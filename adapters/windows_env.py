"""Windows 本机任务环境。

desktop 从物理桌面、当前窗口和可选的 HCU 原生 AX 开始。browser 使用任务浏览器
的 CDP 会话，支持既有的桌面接管操作。两种入口共享框架的观察和动作接口。
"""
from __future__ import annotations

import re
import subprocess
import sys
import math
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Optional

from ..env import Environment, Observation, StepResult
from ..actions import Action, get_action_spec, BASIC_ACTIONS
from .vmware_env import to_pyautogui


class WindowsEnvironment(Environment):
    cli_target = "local"

    evaluation_available = False

    def __init__(self, browser_port: int = 9222, width: int = 1920, height: int = 1080,
                 downloads_dir: Optional[str] = None, workspace_dir: Optional[str] = None,
                 window_timeout_s: float = 15.0, browser_start_url: Optional[str] = None,
                 browser_session_mode: str = "isolated", task_mode: str = "browser",
                 enable_desktop_ax: bool = True, desktop_monitor: str = 'primary'):
        if task_mode not in {"browser", "desktop"}:
            raise ValueError('WindowsEnvironment task_mode must be browser or desktop')
        self.task_mode = task_mode
        self._desktop_lock = threading.RLock()
        self._desktop_ax = None
        self._desktop_stop = None
        self._monitor_selector = desktop_monitor
        self._task_monitor = None
        self._desktop_lease = None
        self._captured_layouts = ()
        self._captured_shell_windows = set()
        self._desktop_intent_task = None
        self._protect_new_file = False
        self._allowed_existing_names = set()
        self._enable_desktop_ax = enable_desktop_ax
        if not isinstance(browser_session_mode, str) or browser_session_mode not in {"isolated", "persistent"}:
            raise ValueError('browser_session_mode must be isolated or persistent')
        self.browser_session_mode = browser_session_mode
        if type(window_timeout_s) not in (int, float) or not math.isfinite(window_timeout_s) or window_timeout_s <= 0:
            raise ValueError('window_timeout_s must be finite and positive')
        self.window_timeout_s = window_timeout_s
        self._window_wait_emit = None
        self._window_wait_remaining = None
        self._window_wait_cancel = threading.Event()
        self._pending_window = None
        self._window_launch_inflight = False
        self._last_window_wait = None
        self.browser_port = browser_port
        self.width = width
        self.height = height
        self.os_name = "windows"
        self.browser_only = task_mode == "browser"
        self.downloads_dir = downloads_dir
        self.workspace_dir = workspace_dir
        self.browser_start_url = browser_start_url
        if downloads_dir is not None and workspace_dir is not None:
            raise ValueError('workspace_dir 与 downloads_dir 不能同时指定')
        self._browser_session = None
        self._last_observation_info = {}
        self._observation_surface = task_mode
        self._episode_prepared = False
        self.initial_state = {}
        self._desktop_capture = {}

    def prepare_workspace(self, task_id=None):
        if self._episode_prepared:
            return dict(self.initial_state)
        if self.task_mode == "desktop":
            from ..runtime.workspace_config import default_workspace_root
            from ..runtime.cli_executor import cli_capability
            root = Path(self.workspace_dir or self.downloads_dir or default_workspace_root(prefer_env=True)).expanduser().resolve()
            directory = root / time.strftime("%Y-%m-%d")
            directory.mkdir(exist_ok=True, parents=True)
            self.initial_state = {"episode_id": task_id or uuid.uuid4().hex, "task_mode": "desktop",
                                  "workspace_dir": str(directory), "workspace_root": str(root),
                                  "workspace_location": "target_desktop", "workspace_scope": "day"}
            self.initial_state["cli_capability"] = cli_capability(self)
            return dict(self.initial_state)
        self.initial_state = self._get_browser_session().prepare_workspace(task_id)
        self.initial_state['workspace_location'] = 'target_desktop'
        return dict(self.initial_state)

    def begin_episode(self, task_id=None, isolated=True):
        if self.task_mode == "desktop":
            with self._desktop_lock:
                from ..desktop.hcu_ax import DesktopAX
                self._episode_prepared = False
                self.prepare_workspace(task_id or self.initial_state.get("episode_id"))
                if self._desktop_ax:
                    self._desktop_ax.close()
                self._desktop_ax = DesktopAX(self._enable_desktop_ax)
                self._desktop_stop = None
                self._observation_surface = "desktop"
                self._desktop_capture = {}
                self._last_observation_info = {}
                self._captured_layouts = ()
                self._captured_shell_windows.clear()
                self._pending_window = self._last_window_wait = None
                self._window_launch_inflight = False
                self._window_wait_cancel.clear()
                self._ensure_desktop_lease()
                if sys.platform == 'win32':
                    from ..desktop.monitor_windows import TaskMonitor
                    self._task_monitor = TaskMonitor(self._monitor_selector)
                    self.initial_state['desktop_monitor'] = self._task_monitor.monitor.copy()
                self._episode_prepared = True
                return dict(self.initial_state)
        session = self._get_browser_session()
        self.initial_state = session.begin_episode(task_id=task_id, isolated=isolated,
            viewport={"width": self.width, "height": self.height}, start_url=self.browser_start_url)
        if (not self.initial_state.get("isolated") and not
                (self.browser_session_mode == "persistent"
                 and self.initial_state.get("isolation_scope") == "task_tabs"
                 and self.initial_state.get("session_mode") == "persistent")):
            raise RuntimeError("Browser episode isolation was not established")
        self.initial_state['workspace_location'] = 'target_desktop'
        session.initial_state = dict(self.initial_state)
        self._episode_prepared = True
        self._observation_surface = "browser"
        self._desktop_capture = {}
        self._last_observation_info = {}
        self._pending_window = None
        self._window_launch_inflight = False
        self._last_window_wait = None
        self._window_wait_cancel.clear()
        return dict(self.initial_state)

    def _ensure_desktop_lease(self):
        if self._desktop_lease is None and sys.platform == "win32" and self.task_mode == "desktop":
            from ..desktop.desktop_capture import acquire_desktop_lease
            self._desktop_lease = weakref.finalize(self, acquire_desktop_lease())

    def set_desktop_file_intent(self, task, checks=()):
        self._desktop_intent_task = task
        files = [c for c in checks or () if isinstance(c, dict) and c.get("kind") == "desktop_file"]
        created = {PureWindowsPath(c["path"]).name.casefold() for c in files if c.get("change") == "created" and c.get("path")}
        modified = {PureWindowsPath(c["path"]).name.casefold() for c in files if c.get("change") == "modified" and c.get("path")}
        text = str(task or "")
        asks_new = (bool(re.search(r"新建|新的.{0,10}文件|创建.{0,15}文件|\bnew\b.{0,18}\bfile\b", text, re.I))
                    and not re.search(r"(?:不要|无需|不得|勿)(?:新建|创建)|\b(?:not|never|don't) create\b", text, re.I))
        forbids_overwrite = bool(re.search(r"(?:不(?:要|得|允许)?|禁止|勿)覆盖|\b(?:never|not|don't)\s+overwrite\b", text, re.I))
        allows_overwrite = bool(re.search(r"允许覆盖|覆盖(?:已有|原文件|现有)|替换现有文件|\boverwrite\b", text, re.I))
        self._protect_new_file = bool(created or forbids_overwrite or (asks_new and not allows_overwrite))
        self._allowed_existing_names = set() if forbids_overwrite else modified - created

    def _overwrite_protected(self, text):
        return self._protect_new_file and not any(
            re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])", text.casefold())
            for name in self._allowed_existing_names)

    @contextmanager
    def framework_wait_context(self, emit, remaining):
        old = self._window_wait_emit, self._window_wait_remaining
        self._window_wait_emit, self._window_wait_remaining = emit, remaining
        try:
            yield
        finally:
            self._window_wait_emit, self._window_wait_remaining = old

    def cancel_pending_wait(self):
        self._window_wait_cancel.set()

    def _wait_for_desktop_window(self, target, *, dispatched):
        from ..desktop.desktop_readiness import wait_for_window
        self._pending_window = target
        read_state = self._runtime_state if self.task_mode == 'desktop' else None
        if self._task_monitor:
            read_state = lambda: self._task_monitor.launch_state(self._runtime_state(), target)
        report = wait_for_window(target, timeout_s=self.window_timeout_s,
            emit=self._window_wait_emit, remaining=self._window_wait_remaining,
            cancelled=self._window_wait_cancel.is_set,
            read_state=read_state)
        self._last_window_wait = report
        if report['status'] == 'completed':
            if self._task_monitor and report.get('window'):
                self._task_monitor.bind(report['window'])
            self._pending_window = None
            self._window_launch_inflight = False
        return {'status': 'executed' if report['status'] == 'completed' else 'uncertain',
                'dispatched': dispatched, 'channel': 'desktop', 'framework_wait': report,
                'reason': report['detail'],
                **({'window': report['window']} if report.get('window') else {}),
                **({'error_code': 'desktop_window_' + report['status']} if report['status'] != 'completed' else {})}

    # ---- 观察 ----
    def _take_screenshot(self):
        """Desktop fallback; browser screenshots are collected atomically with AX."""
        from ..desktop.desktop_capture import capture_monitor, capture_virtual_desktop
        self._desktop_capture = {}
        if self.task_mode == 'desktop' and sys.platform == 'win32' and self._task_monitor is None:
            from ..desktop.monitor_windows import TaskMonitor
            self._ensure_desktop_lease()
            self._task_monitor = TaskMonitor(self._monitor_selector)
        if self._task_monitor:
            shot, self._desktop_capture = capture_monitor(self._task_monitor.monitor['id'])
        else:
            shot, self._desktop_capture = capture_virtual_desktop()
        return shot

    def _runtime_state(self):
        from ..desktop.desktop_state import windows_runtime_state
        return windows_runtime_state(include_layout=self.task_mode == "desktop")

    def _desktop_is_input(self):
        from ..desktop.desktop_capture import input_desktop_active
        return input_desktop_active()

    def _inactive_observation(self):
        self._desktop_capture = {}
        self._captured_layouts = ()
        self._captured_shell_windows.clear()
        if self._desktop_ax:
            self._desktop_ax.invalidate()
        self._last_observation_info = {"task_mode": "desktop", "active_surface":"desktop",
            "screenshot_stale":True, "observation_error":"任务桌面当前不可输入，请等待桌面恢复后再观察",
            "desktop_ax_status":"input_desktop_inactive", "initial_state":dict(self.initial_state)}
        return Observation(info=self._last_observation_info)

    def _focus_is_browser(self) -> bool:
        """本机浏览器模式：以「CDP 有可用页面」为准，不依赖前台焦点。

        用户可同时用电脑（切到别的窗口），智能体照常通过 CDP 虚拟控制浏览器。
        """
        if self.task_mode == "desktop":
            return False
        from ..browser.utils import list_cdp_pages
        try:
            pages = list_cdp_pages("localhost", self.browser_port)
            return any(p.get("url") and not p.get("url", "").startswith(
                ("devtools://", "chrome-extension://")) for p in pages)
        except Exception:  # noqa: BLE001
            return False

    def _get_browser_session(self):
        """懒加载 BrowserSession（保持快照，跨步复用）。连不上返回 None。

        require_focus=False：本机模式用 CDP 虚拟输入，不要求浏览器前台聚焦——
        这样智能体操作浏览器时，你可以照常用自己的鼠标键盘。
        """
        if self.task_mode == "desktop":
            return None
        if self._browser_session is None:
            from ..browser.browser_session import BrowserSession, BrowserOptions
            self._browser_session = BrowserSession(
                options=BrowserOptions(endpoint=f"http://localhost:{self.browser_port}",
                                       require_focus=False, session_mode=self.browser_session_mode),
                foreground_check=None,
                downloads_dir=self.downloads_dir,
                workspace_dir=self.workspace_dir,
            )
        return self._browser_session

    def _collect_browser_snapshot(self):
        """抓当前浏览器页面的结构化 AX/DOM 快照（browser_ax）。非浏览器场景返回 None。"""
        if not self._focus_is_browser():
            return None
        try:
            session = self._get_browser_session()
            if session is None:
                return None
            return session.collect()
        except Exception:  # noqa: BLE001
            return None

    def _build_observation(self) -> Observation:
        """Collect screenshot and AX on one page, with an explicit coordinate space.

        Windows 通用桌面使用本轮固定的任务屏。后台浏览器观察通过 CDP 截取页面，
        不受浏览器窗口遮挡影响；只有显式 observe_desktop 才观察整个虚拟桌面。
        """
        from ..model.grounding import screenshot_size
        from ..runtime.scene import Scene, browser_scene
        if self.task_mode == "desktop" and not self._desktop_is_input():
            return self._inactive_observation()

        from ..desktop.desktop_readiness import foreground, window_identity
        desktop_before = self._runtime_state() if self._observation_surface == 'desktop' else None
        window_moved = False
        if self._task_monitor:
            from ..desktop.monitor_windows import MonitorStop
            try:
                if self._pending_window and self._window_launch_inflight:
                    from ..desktop.desktop_readiness import matches_target, window_ready
                    candidate = foreground(self._task_monitor.launch_state(desktop_before, self._pending_window))
                    if candidate and matches_target(candidate, self._pending_window) and window_ready(candidate):
                        self._task_monitor.bind(candidate)
                        self._pending_window = dict(candidate)
                self._task_monitor.prepare_observation(desktop_before, self._desktop_intent_task)
                window_moved = self._task_monitor.moved
                if window_moved:
                    self._task_monitor.moved = False
                    if self._desktop_ax:
                        self._desktop_ax.invalidate()
                    self._desktop_capture = {}
                    desktop_before = self._runtime_state()
            except MonitorStop as exc:
                self._stop_monitor(exc)
                return self._stopped_observation()
        screenshot, snapshot, metadata = None, None, {}
        error = ""
        if self._observation_surface == "browser":
            # 页面仍在导航/渲染时截图会瞬时失败，重试几次再放弃。
            import time as _time
            for _ in range(4):
                try:
                    session = self._get_browser_session()
                    if session is not None:
                        screenshot, snapshot, metadata = session.collect_observation()
                        metadata = dict(metadata or {})
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
                if screenshot is not None and metadata.get("screenshot_space") == "css_viewport":
                    break
                _time.sleep(0.25)
        elif self.task_mode != "desktop":
            try:
                session = self._get_browser_session()
                if session is not None and hasattr(session, "event_metadata"):
                    metadata = session.event_metadata()
            except Exception as exc:  # noqa: BLE001
                error = str(exc)

        if screenshot is not None and metadata.get("screenshot_space") == "css_viewport":
            info = metadata
            info["scene"] = browser_scene(
                snapshot, screenshot, page_id=metadata.get("page_id", ""),
                snapshot_id=metadata.get("snapshot_id", ""),
            ).to_dict()
            context = [snapshot] if snapshot else []
        elif self._observation_surface == "browser":
            # 浏览器模式：CDP 截图失败 → 返回错误观测（无截图），不截取用户桌面。
            info = {"screenshot_space": "css_viewport",
                    "scene": Scene(browser_use=1, structure_available=bool(snapshot),
                                   mode="browser_dom" if snapshot else "visual",
                                   reason="browser_observation_failed").to_dict(),
                    "observation_error": error or str(metadata.get("error") or "浏览器截图/快照采集失败")}
            for key in ("artifacts", "download_events", "pending_downloads", "dialogs", "dialog",
                        "initial_state", "tabs", "artifact_events"):
                if key in metadata:
                    info[key] = metadata[key]
            context = [snapshot] if snapshot else []
        else:
            # 本机桌面截任务屏，浏览器任务显式切到桌面时沿用虚拟桌面截图。
            try:
                screenshot = self._take_screenshot()
            except (ValueError, OSError, RuntimeError) as exc:
                error = str(exc)
                self._desktop_capture = {}
                if self._task_monitor:
                    from ..desktop.monitor_windows import MonitorStop
                    try:
                        self._task_monitor.refresh()
                    except MonitorStop as stopped:
                        self._stop_monitor(stopped)
                        return self._stopped_observation()
            size = screenshot_size(screenshot)
            info = {"screenshot_space": "screen_pixels",
                    "screenshot_size": list(size) if size else None,
                    **self._desktop_capture,
                    "scene": Scene(browser_use=0, structure_available=False,
                                   mode="visual", reason="desktop_fallback").to_dict()}
            for key in ("artifacts", "download_events", "pending_downloads", "dialogs", "dialog",
                        "initial_state", "tabs", "artifact_events"):
                if key in metadata:
                    info[key] = metadata[key]
            if error or metadata.get("error") or metadata.get("observation_errors"):
                info["observation_error"] = error or metadata.get("error") or "; ".join(metadata["observation_errors"])
            context = []
        if self.initial_state:
            info.setdefault('initial_state', dict(self.initial_state))
        info["active_surface"] = "browser" if info.get("screenshot_space") == "css_viewport" else "desktop"
        info["requested_surface"] = self._observation_surface
        info['control_mode'] = 'background_browser' if info['active_surface'] == 'browser' else 'desktop_takeover'
        info["task_mode"] = self.task_mode
        ax_block, ax_reason = None, ""
        ax_window = foreground(desktop_before or {})
        if self._task_monitor:
            from ..desktop.monitor_windows import window_fits
            info['desktop_window_moved'] = window_moved
            if not ax_window or not window_fits(ax_window, self._task_monitor.monitor):
                ax_window = None
                info['desktop_foreground_outside_task_monitor'] = True
        if self.task_mode == "desktop" and self._desktop_ax:
            ax_block, ax_reason = self._desktop_ax.observe(ax_window,
                                                           info.get("desktop_capture_id"))
        info["runtime_state"] = self._runtime_state()
        if self.task_mode == "desktop" and not self._desktop_is_input():
            return self._inactive_observation()
        if desktop_before is not None:
            before, after = foreground(desktop_before), foreground(info['runtime_state'])
            info['desktop_focus_stable'] = bool(before and after and window_identity(before) == window_identity(after))
            if self.task_mode == "desktop":
                from ..desktop.hcu_ax import browser_window
                from ..desktop.desktop_state import common_file_dialog, shell_pointer_windows
                self._captured_layouts = tuple(layout for layout in (
                    desktop_before.pop("_window_layout", None), info["runtime_state"].pop("_window_layout", None))
                    if layout is not None)
                self._captured_shell_windows = shell_pointer_windows(desktop_before) & shell_pointer_windows(info['runtime_state'])
                info['control_mode'] = 'desktop'
                info['desktop_browser_foreground'] = browser_window(after)
                info['desktop_focus_stable'] = bool(info['desktop_focus_stable'] and before.get("title") == after.get("title"))
                if self._task_monitor:
                    info['desktop_focus_stable'] = bool(info['desktop_focus_stable'] and before.get('bounds') == after.get('bounds'))
                if (info['desktop_focus_stable'] and info.get('desktop_capture_id')
                        and not info.get('desktop_foreground_outside_task_monitor') and not info.get('observation_error')):
                    from ..desktop.explorer_location import observe_explorer_location
                    location = observe_explorer_location(after)
                    if location is not None:
                        info['explorer_location'] = location
                kind, confirmation_text = common_file_dialog(after, ax_block if info['desktop_focus_stable'] else None)
                info["desktop_file_dialog"] = kind == "picker"
                info["desktop_overwrite_protected"] = kind == "overwrite" and self._overwrite_protected(confirmation_text)
                if ax_block and (info["desktop_file_dialog"] or info["desktop_overwrite_protected"]):
                    # Win32 common-dialog UIA SetValue can update the display
                    # without updating the shell's selected file. Use real keyboard
                    # and pointer messages for this dialog, and retain read evidence.
                    ax_block["read_only"] = True
                    for node in ax_block["nodes"]:
                        node.update(direct=False, actions=[])
                if ax_block and info['desktop_focus_stable']:
                    context.append(ax_block)
                    info["scene"] = Scene(browser_use=0, structure_available=True, mode="desktop_ax",
                                          reason="native_window_observed").to_dict()
                elif self._desktop_ax:
                    self._desktop_ax.invalidate()
                info['desktop_ax_status'] = ('available' if context else ax_reason or 'foreground_changed')
                if self._pending_window and (self._last_window_wait or {}).get('status') == 'timeout':
                    from ..desktop.desktop_readiness import matches_target, window_ready
                    eligible = (not self._task_monitor or not self._window_launch_inflight or self._pending_window.get('hwnd'))
                    if (eligible and info['desktop_focus_stable'] and window_ready(before) and window_ready(after)
                            and before['bounds'] == after['bounds'] and matches_target(after, self._pending_window)):
                        self._last_window_wait = {**self._last_window_wait, 'status': 'completed', 'window': dict(after),
                            'detail': '先前等待超时，新的桌面截图前后已确认目标窗口在前台且可接收输入'}
                        self._pending_window = None
                        self._window_launch_inflight = False
            if self._last_window_wait:
                info['desktop_window_wait'] = dict(self._last_window_wait)
        # The compiler attaches the actually exposed native refs to this exact
        # observation after truncating the decision prompt.
        self._last_observation_info = info if self.task_mode == "desktop" else dict(info)
        return Observation(screenshot=screenshot, text="", context=context, info=info)

    def _derive_scene(self, snapshot, screenshot=None):
        """Derive browser mode from whether page content is actionable in AX."""
        from ..runtime.scene import Scene, browser_scene
        if snapshot:
            return browser_scene(snapshot, screenshot).to_dict()
        if self._focus_is_browser():
            return Scene(browser_use=1, structure_available=False, mode="visual",
                         reason="no_structured_page").to_dict()
        return Scene(browser_use=0, structure_available=False, mode="visual",
                     reason="not_browser").to_dict()

    def _fresh_obs_after_action(self) -> Observation:
        """动作执行后重新观察：新截图 + 新 browser 快照。"""
        return self.observe()

    # ---- 路由 + 执行（browser_dom）----
    # 浏览器场景里全部走 CDP 的动作（虚拟鼠标键盘，不碰真实外设）
    _BROWSER_CDP_ACTIONS = ("goto", "download", "click", "double_click", "right_click",
                            "type", "select", "scroll", "press", "hotkey")

    def route_action(self, action, obs):
        """浏览器场景的动作 → browser_dom（CDP 虚拟输入）；结构解析失败 → None（视觉定位）。

        流程：DOM（element_id / target_ref / 语义匹配）→ 命中走 CDP；都没命中返回 None，
        交给 pipeline 用 grounding 模型定坐标，再由 step() 用 CDP 虚拟坐标点击（仍不动真实鼠标）。
        """
        if self.task_mode == "desktop":
            with self._desktop_lock:
                if action.args.get("target_ref") and ((obs and obs.info.get("desktop_file_dialog"))
                                                       or self._last_observation_info.get("desktop_overwrite_protected")):
                    return {"channel":"reobserve","source":"desktop_ax","error_code":"desktop_dialog_requires_gui",
                            "reason":"文件对话框使用可见按钮和真实键盘；新建文件遇到覆盖确认请按 Esc 返回"}
                if action.args.get("target_ref") and not self._desktop_ax:
                    return {"channel": "reobserve", "source": "desktop_ax", "error_code": "stale_desktop_ref",
                            "reason": "原生 AX 未就绪，请重新观察"}
                return self._desktop_ax.route(action, obs) if self._desktop_ax else None
        if obs is None or action.action not in self._BROWSER_CDP_ACTIONS:
            return None
        # Route against the actual observation, not a fresh CDP page guess.
        from ..runtime.scene import observation_scene
        if not observation_scene(obs).get("browser_use"):
            return None
        session = self._get_browser_session()
        if session is None:
            return None
        # 地址栏输入 URL → 转 goto（地址栏是浏览器 chrome UI，不在 DOM）
        url = self._address_bar_url(action)
        if url:
            return {"channel": "browser_dom", "goto_url": url, "coordinate_space": "css_viewport"}
        # A scroll with an explicit node refers to that container, not the page.
        if action.action == "scroll" and (action.args.get("target_ref") or action.args.get("element_id")):
            return session.route(action, obs)
        # goto / scroll / press / hotkey：无 target，直接 browser_dom
        if action.action in ("goto", "download", "scroll", "press", "hotkey"):
            return {"channel": "browser_dom", "target": action.args.get("target"),
                    "coordinate_space": "css_viewport", **self._route_identity(obs)}
        # click / type / select / double / right：优先 DOM 定位
        try:
            route = session.route(action, obs)  # 处理 target_ref / element_id
        except Exception:  # noqa: BLE001
            route = None
        if route is not None:
            return route  # browser_dom 或 reobserve
        # A content-heavy AX snapshot may expose only navigation/filter controls.
        # Do not resolve a card description against those unrelated controls;
        # let screenshot grounding choose the card while explicit refs still work.
        selected_mode = ((obs.info or {}).get('decision_mode') or {}).get('mode')
        if ((selected_mode == 'gui' or (selected_mode is None and observation_scene(obs).get("mode") == "browser_gui"))
                and action.action in {"click", "double_click", "right_click"}):
            return None
        # 语义兜底：target 描述 → ref（仍在结构层）
        target = str(action.args.get("target") or "")
        if target:
            exposed = obs.info.get("exposed_refs") if isinstance(obs.info, dict) else None
            if exposed is not None and not exposed:
                return None
            allowed = set(exposed) if exposed else None
            try:
                ref = session.resolve_target(target, allowed=allowed)
            except Exception:  # noqa: BLE001
                ref = None
            if not ref and getattr(session, 'last_resolution_error', None) == 'ambiguous':
                return {'channel': 'reobserve', 'error_code': 'ambiguous',
                        'reason': '多个 AX 元素匹配此描述，请结合标题和链接选择当前 target_ref，不能通过视觉猜选。'}
            if ref:
                action.args["target_ref"] = ref
                return session.route(action, obs)
        # 结构解析失败：返回 None → 视觉定位（grounding 定坐标 → CDP 虚拟点击）
        return None

    @staticmethod
    def _route_identity(obs):
        return {key: obs.info[key] for key in ("page_id", "document_id", "snapshot_id")
                if key in obs.info}

    @staticmethod
    def _address_bar_url(action):
        """模型往地址栏 type 网址（常见误操作）→ 提取 URL 用于 goto；否则返回 None。

        地址栏是浏览器 chrome UI、不在页面 DOM 里，type/click 定位不到；识别后直接转 goto。
        """
        if action.action != "type":
            return None
        text = str(action.args.get("text", "")).strip()
        target = str(action.args.get("target", "")).lower()
        if not re.search(r"^(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?$", text):
            return None
        if any(k in target for k in ("address bar", "omnibox", "url bar", "地址栏", "导航栏", "网址栏")):
            return text
        return None

    def prevent_duplicate_download(self, action, observation, route=None):
        if self.task_mode == "desktop":
            return None
        session = self._get_browser_session()
        if session is None:
            return None
        return session.prevent_duplicate_download(action, observation, route)

    def execute_routed(self, action, route):
        """执行已路由的 browser_dom 动作：goto / DOM(ref) / scroll/press/hotkey 全部走 CDP。"""
        if self.task_mode == "desktop":
            from ..desktop.desktop_readiness import foreground
            with self._desktop_lock:
                self._ensure_desktop_lease()
                if self._desktop_stop:
                    return StepResult(self.observe(), done=True, info=dict(self._desktop_stop))
                if (self._last_observation_info.get("desktop_file_dialog")
                        or self._last_observation_info.get("desktop_overwrite_protected")):
                    return StepResult(self.observe(), info={"channel":"desktop_ax","status":"rejected",
                                      "dispatched":False,"error_code":"desktop_dialog_requires_gui"})
                exposed = ((self._last_observation_info.get("exposed_refs") or ())
                           if "exposed_refs" in self._last_observation_info else None)
                if exposed is not None and route.get("target_ref") not in exposed:
                    return StepResult(self.observe(), info={"channel":"desktop_ax","status":"rejected",
                                      "dispatched":False,"error_code":"unexposed_desktop_ref"})
                if route.get("channel") != "desktop_ax" or not self._desktop_ax:
                    return StepResult(self.observe(), info={"channel": "desktop_ax", "status": "rejected",
                                      "dispatched": False, "error_code": "unavailable_desktop_route"})
                if self._window_wait_cancel.is_set() or (self._window_wait_remaining and self._window_wait_remaining() <= 0):
                    return StepResult(self.observe(), info={"channel": "desktop_ax", "status": "rejected",
                                      "dispatched": False, "error_code": "desktop_operation_cancelled"})
                def ax_window():
                    live_state = self._runtime_state() if self._desktop_is_input() else {}
                    current = foreground(live_state)
                    if current and self._task_monitor:
                        from ..desktop.monitor_windows import MonitorStop, window_fits
                        try:
                            monitor = self._task_monitor.refresh()
                            self._task_monitor.guard_focus(live_state)
                        except MonitorStop as exc:
                            self._stop_monitor(exc)
                            return None
                        before = foreground(self._last_observation_info.get('runtime_state', {})) or {}
                        if (monitor != self._desktop_capture.get('desktop_monitor', monitor)
                                or current.get('bounds') != before.get('bounds') or not window_fits(current, monitor)):
                            return None
                    return current

                result = self._desktop_ax.execute(action, route, ax_window)
                if (result.get("continuation") or {}).get("state") == "stop":
                    self._desktop_stop = result
                    return StepResult(self._stopped_observation(), done=True, info=result)
                if self._task_monitor and result.get('dispatched') and not self._task_monitor.root:
                    before = foreground(self._last_observation_info.get('runtime_state', {}))
                    if before:
                        from ..desktop.desktop_readiness import window_identity
                        self._task_monitor.root = window_identity(before)
                return self._monitor_result(StepResult(self.observe(), info=result))
        if action.action in {"download", "rename_artifact", "run_in_terminal", "call_code_agent", "set_cell_values", "save"}:
            return StepResult(self.observe(), info={"status": "rejected", "dispatched": False,
                "error_code": "non_basic_action", "action": action.action})
        session = self._get_browser_session()
        if session is None:
            return StepResult(observation=self.observe(), info={
                "status": "rejected", "dispatched": False, "channel": "browser_dom",
                "error": "browser session unavailable"})
        if action.action == "goto" or route.get("goto_url"):
            result = session.goto(str(action.args.get("url") or route.get("goto_url")))
        elif route.get("target_ref"):
            result = session.execute(action, route)
        elif action.action in ("scroll", "press", "hotkey"):
            result = session.execute_viewport_action(
                action, metadata=action.args.get("observation_metadata") or self._last_observation_info)
        else:
            result = {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                      "error": "no routable target"}
        obs = self._fresh_obs_after_action()
        info = dict(result)
        info["artifacts"] = obs.info.get("artifacts", info.get("artifacts", []))
        return StepResult(observation=obs, info=info)

    # ---- Environment 接口 ----
    def reset(self, task: str) -> Observation:
        if self.task_mode == "desktop" and self._desktop_intent_task != task:
            self.set_desktop_file_intent(task)
        if not self._episode_prepared:
            self.begin_episode()
        self._episode_prepared = False
        return self.observe()

    def observe(self) -> Observation:
        if self.task_mode == "desktop":
            with self._desktop_lock:
                return self._stopped_observation() if self._desktop_stop else self._build_observation()
        return self._build_observation()

    def _stopped_observation(self):
        return Observation(info={"task_mode": "desktop", "active_surface": "desktop", "screenshot_stale": True,
                                 "observation_error": (self._desktop_stop or {}).get('reason') or "HCU 已指示停止，本次任务无法继续观察",
                                 "desktop_stop": self.desktop_stop,
                                 "initial_state": self.initial_state})

    @property
    def desktop_stop(self):
        with self._desktop_lock:
            return dict(self._desktop_stop or {})

    def _stop_monitor(self, exc):
        self._desktop_capture = {}
        if self._desktop_ax:
            self._desktop_ax.invalidate()
        self._desktop_stop = dict(status='rejected', dispatched=False, channel='desktop',
                                  error_code=exc.code, reason=str(exc), continuation={'state': 'stop'})

    def _monitor_result(self, result):
        if self._desktop_stop and not result.done:
            if result.info.get('dispatched'):
                self._desktop_stop['dispatched'] = True
            return StepResult(self._stopped_observation(), done=True, info=dict(self._desktop_stop))
        return result

    def step(self, action: Action) -> StepResult:
        if self.task_mode == "desktop":
            with self._desktop_lock:
                self._ensure_desktop_lease()
                if self._desktop_stop:
                    return StepResult(self._stopped_observation(), done=True, info=dict(self._desktop_stop))
                from ..desktop.monitor_windows import MonitorStop
                try:
                    return self._monitor_result(self._step(action))
                except MonitorStop as exc:
                    self._stop_monitor(exc)
                    return StepResult(self._stopped_observation(), done=True, info=dict(self._desktop_stop))
        return self._step(action)

    def _step(self, action: Action) -> StepResult:
        name = action.action
        if name not in BASIC_ACTIONS:
            return StepResult(self.observe(), info={"status": "rejected", "dispatched": False,
                "error_code": "non_basic_action", "action": name})
        spec = get_action_spec(name)
        kind = spec.kind if spec else "env"
        if self.task_mode == "desktop":
            from ..desktop.desktop_readiness import foreground, global_shortcut
            from ..desktop.hcu_ax import browser_window
            if name not in {"observe_desktop", "wait"} and not self._desktop_is_input():
                return StepResult(self._inactive_observation(), info={"status":"rejected", "dispatched":False,
                                  "error_code":"input_desktop_inactive", "channel":"desktop"})
            allowed = {"observe_desktop", "click", "double_click", "right_click", "type", "select", "scroll",
                       "press", "hotkey", "drag_and_drop", "switch_applications", "wait"}
            if (self._last_observation_info.get("desktop_overwrite_protected") and name not in {"observe_desktop","wait"}
                    and not (name == "press" and str(action.args.get("key", "")).casefold() in {"esc","escape"})):
                return StepResult(self.observe(), info={"status":"rejected","dispatched":False,"channel":"desktop",
                                  "error_code":"unexpected_file_overwrite","reason":"新建任务出现覆盖确认，请按 Esc 并核对实际文件名"})
            if name not in allowed or action.args.get("element_id") or action.args.get("target_ref") or action.args.get("coordinate_space") == "css_viewport":
                return StepResult(self.observe(), info={"status": "rejected", "dispatched": False,
                                  "error_code": "desktop_scope", "channel": "desktop"})
            if name == "switch_applications":
                from ..desktop.desktop_state import windows_application_process
                if windows_application_process(action.args.get("app_code")) in {"chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "chromium", "iexplore", "arc"}:
                    return StepResult(self.observe(), info={"status": "rejected", "dispatched": False,
                                      "error_code": "browser_task_out_of_scope", "channel": "desktop"})
            if name not in {"observe_desktop", "wait", "switch_applications"} and not global_shortcut(action):
                current_state = self._runtime_state()
                current, previous = foreground(current_state), foreground(self._last_observation_info.get("runtime_state", {}))
                from ..desktop.desktop_readiness import window_identity
                leaving_browser = browser_window(current)
                if leaving_browser and name not in {"click", "double_click"}:
                    return StepResult(self.observe(), info={"status": "rejected", "dispatched": False,
                                      "error_code": "browser_task_out_of_scope", "channel": "desktop"})
                if (not self._last_observation_info.get("desktop_focus_stable") or not current or
                        window_identity(current) != window_identity(previous) or current.get("title") != previous.get("title")):
                    return StepResult(self.observe(), info={"status": "rejected", "dispatched": False,
                                      "error_code": "desktop_focus_changed", "channel": "desktop",
                                      "reason": "未能确认截图与当前输入属于同一前台窗口，请重新观察桌面"})
                if name in {"click", "double_click", "right_click", "drag_and_drop", "type", "select", "scroll"}:
                    from ..desktop.desktop_state import pointer_guard, shell_pointer_windows
                    browser_handles = {w["hwnd"] for w in current_state.get("applications", []) if w.get("hwnd") and browser_window(w)}
                    shell_handles = None
                    if current.get('shell_surface'):
                        stable_shell = self._captured_shell_windows & shell_pointer_windows(current_state)
                        shell_handles = {identity[0] for identity in stable_shell}
                    try:
                        if self._captured_layouts and "_window_layout" not in current_state:
                            raise OSError("无法核实目标窗口位置")
                        layouts = (*self._captured_layouts, current_state["_window_layout"]) if "_window_layout" in current_state else ()
                        guard_error = pointer_guard(action, self._desktop_capture.get("screen_origin", [0, 0]), browser_handles,
                                                    layouts, leaving_browser=leaving_browser, shell_handles=shell_handles,
                                                    monitor=self._desktop_capture.get('desktop_monitor'))
                    except OSError:
                        return StepResult(self.observe(), info={"status": "rejected", "dispatched": False,
                                          "error_code": "desktop_pointer_unconfirmed", "channel": "desktop"})
                    if guard_error:
                        return StepResult(self.observe(), info={"status": "rejected", "dispatched": False,
                                          "error_code": guard_error, "channel": "desktop",
                                          **({"reason": "鼠标落点不属于截图中已确认的开始菜单或搜索窗口，请重新观察"}
                                             if guard_error == "desktop_shell_target_changed" else {})})
        if name in {"observe_desktop", "observe_browser"}:
            self._observation_surface = "desktop" if name == "observe_desktop" else "browser"
            obs = self.observe()
            return StepResult(observation=obs, info={"status": "executed", "dispatched": False,
                "action": name, "artifacts": obs.info.get("artifacts", [])})
        if name in {"handle_dialog", "list_tabs", "switch_tab", "close_tab"}:
            session = self._get_browser_session()
            kwargs = {k: v for k, v in action.args.items() if k in {
                "accept", "prompt_text", "dialog_id", "tab_id", "artifact_id", "filename", "url"}}
            result = getattr(session, name)(**kwargs)
            obs = self.observe()
            result["artifacts"] = obs.info.get("artifacts", [])
            return StepResult(observation=obs, info=result)

        coordinate_space = action.args.get("coordinate_space")
        coordinate_actions = ("click", "double_click", "right_click", "type", "select", "drag_and_drop")
        if action.args.get("grounding_failed"):
            return StepResult(observation=self.observe(), info={
                "status": "rejected", "dispatched": False, "error": "grounding failed"})

        if coordinate_space == "css_viewport" and kind == "env" and name != "wait":
            session = self._get_browser_session()
            if session is None:
                result = {"status": "rejected", "dispatched": False,
                          "channel": "browser_cdp", "error": "browser session unavailable"}
            else:
                result = session.execute_viewport_action(
                    action, metadata=action.args.get("observation_metadata") or self._last_observation_info)
            obs = self.observe()
            result = dict(result)
            result["artifacts"] = obs.info.get("artifacts", [])
            return StepResult(observation=obs, info=result)

        if name in coordinate_actions and coordinate_space != "screen_pixels":
            return StepResult(observation=self.observe(), info={
                "status": "rejected", "dispatched": False,
                "error": "coordinate action requires css_viewport or screen_pixels"})

        if kind == "browser":
            result = self.browser_action(name, action.args)
            return StepResult(observation=self._build_observation(), info={"browser_result": result})

        if kind == "shell":
            output = self.run_command(str(action.args.get("command", "")))
            return StepResult(observation=self._build_observation(), info={"terminal_result": output})

        # 浏览器表面使用 CDP；显式切到桌面并重新观察后才允许桌面输入。
        if self.browser_only and self._observation_surface != "desktop" and name != "wait":
            return StepResult(observation=self.observe(), info={
                "status": "rejected", "dispatched": False, "channel": "desktop",
                "error": "请先 observe_desktop 获取桌面截图后再执行 GUI 操作；网页操作使用 DOM 或 observe_browser"})
        if self._window_wait_cancel.is_set() or (self._window_wait_remaining and self._window_wait_remaining() <= 0):
            return StepResult(observation=self.observe(), info={'status': 'rejected', 'dispatched': False,
                'channel': 'desktop', 'error_code': 'desktop_operation_cancelled', 'reason': '操作已取消或任务时间预算已耗尽'})
        return self._step_desktop(action)

    def _step_desktop(self, action: Action) -> StepResult:
        name = action.action
        if name == 'switch_applications':
            from ..desktop.desktop_state import switch_windows_application, windows_application_process
            if self._task_monitor:
                from ..desktop.desktop_readiness import foreground, window_identity
                from ..desktop.monitor_windows import MonitorStop
                self._task_monitor.refresh()
                if self._task_monitor.root:
                    observed = foreground(self._last_observation_info.get('runtime_state', {}))
                    state = self._runtime_state()
                    self._task_monitor.guard_focus(state)
                    current = foreground(state)
                    if not current or window_identity(observed) != window_identity(current):
                        raise MonitorStop('desktop_window_focus_changed', '切换目标应用前检测到前台变化，已停止桌面输入')
            process = windows_application_process(action.args.get('app_code'))
            if sys.platform == 'win32' and process and not self._task_monitor:
                self._pending_window = {'process_name': process + '.exe'}
                self._window_launch_inflight = False
            if self._task_monitor:
                result = switch_windows_application(action.args.get('app_code'),
                    task=self._desktop_intent_task, window_title=str(action.args.get('window_title') or ''),
                    bind=self._task_monitor.bind)
                if result.get('error_code') == 'ambiguous_application':
                    from ..desktop.monitor_windows import MonitorStop
                    raise MonitorStop('desktop_window_ambiguous', '多个窗口符合任务目标，无法确认要操作的窗口：'
                                      + '、'.join(result.get('candidates') or ()))
            else:
                result = switch_windows_application(action.args.get('app_code'))
            if sys.platform == 'win32' and result.get('window') and result.get('status') in {'executed', 'observed', 'uncertain'}:
                result.update(self._wait_for_desktop_window(result['window'], dispatched=result['dispatched']))
                if result['framework_wait']['status'] == 'completed':
                    result.pop('error_code', None)
            return StepResult(observation=self.observe(), info=result)
        from ..desktop.desktop_readiness import launch_target, global_shortcut, check_keyboard_focus, DesktopFocusError
        target = launch_target(action) if sys.platform == 'win32' else None
        if target and self._window_launch_inflight and self._pending_window and self._pending_window.get('process_name') == target['process_name']:
            # A previous launch may still be completing. Never create more windows.
            result = self._wait_for_desktop_window(target, dispatched=False)
            return StepResult(observation=self.observe(), info=result)
        keyboard_guard = (sys.platform == 'win32' and name in {'type', 'select', 'press', 'hotkey', 'goto'}
                          and not global_shortcut(action))
        observed = self._last_observation_info
        def guard_input():
            live_state = self._runtime_state()
            current = check_keyboard_focus(observed.get('runtime_state', {}), live_state,
                pending_target=self._pending_window, capture_stable=observed.get('desktop_focus_stable') is True)
            if self._task_monitor:
                from ..desktop.desktop_readiness import foreground
                from ..desktop.monitor_windows import window_fits
                self._task_monitor.guard_focus(live_state)
                monitor = self._task_monitor.refresh()
                previous = foreground(observed.get('runtime_state', {})) or {}
                if (current.get('bounds') != previous.get('bounds') or not window_fits(current, monitor)
                        or monitor != self._desktop_capture.get('desktop_monitor', monitor)):
                    raise DesktopFocusError('目标窗口位置或显示器已变化，请重新观察任务屏')
        if keyboard_guard:
            try:
                guard_input()
            except DesktopFocusError as exc:
                return StepResult(observation=self.observe(), info={'status': 'rejected', 'dispatched': False,
                    'channel': 'desktop', 'error_code': 'desktop_focus_unconfirmed', 'reason': str(exc)})
        # 坐标化后执行 pyautogui 命令
        try:
            if sys.platform == 'win32' and not self._desktop_capture and name != 'wait':
                raise ValueError('缺少有效虚拟桌面截图，请重新 observe_desktop')
            if self._desktop_capture:
                from ..desktop.desktop_capture import validate_desktop_action, virtual_geometry
                mapped, origin = validate_desktop_action(action, self._desktop_capture, virtual_geometry())
                if self._task_monitor and name != 'wait':
                    monitor_error = self._prepare_monitor_input(action, origin)
                    if monitor_error:
                        return StepResult(self.observe(), info=dict(status='rejected', dispatched=False,
                            channel='desktop', error_code='desktop_monitor_reobserve', reason=monitor_error))
                cmd = to_pyautogui(mapped, desktop_origin=origin, keyboard_guard=keyboard_guard,
                                   literal_click=self.task_mode == "desktop")
            else:
                cmd = to_pyautogui(action, keyboard_guard=keyboard_guard)
        except (ValueError, OSError) as exc:
            return StepResult(observation=self.observe(), info={'status': 'rejected', 'dispatched': False,
                'channel': 'desktop', 'error_code': 'desktop_geometry_changed', 'error': str(exc)})
        if cmd:
            try:
                from ..desktop.desktop_capture import physical_dpi_context
                with physical_dpi_context():
                    if self.task_mode == "desktop" and name != "wait" and not self._desktop_is_input():
                        return StepResult(self._inactive_observation(), info={"status":"rejected", "dispatched":False,
                                          "error_code":"input_desktop_inactive", "channel":"desktop"})
                    exec(cmd, {"__name__": "__main__", "desktop_input_guard": guard_input})  # noqa: S102
            except DesktopFocusError as exc:
                return StepResult(observation=self.observe(), info={'status': 'uncertain', 'dispatched': True,
                    'channel': 'desktop', 'error_code': 'desktop_focus_changed_during_action', 'reason': str(exc)})
            except Exception as exc:  # noqa: BLE001
                return StepResult(observation=self._build_observation(),
                                  info={"status": "failed", "dispatched": True,
                                        "channel": "desktop", "error": f"执行异常: {exc}"})
            if target:
                self._window_launch_inflight = True
                result = self._wait_for_desktop_window(target, dispatched=True)
                return StepResult(observation=self.observe(), info=result)
            after = self.observe()
            receipt = {"status": "executed", "dispatched": True, "channel": "desktop"}
            if self.task_mode == 'desktop' and name == 'hotkey':
                keys = [str(key).casefold() for key in action.args.get('keys', [])]
                if len(keys) == 2 and keys[0] in {'win', 'winleft', 'winright'} and keys[1] == 'r':
                    from ..desktop.desktop_readiness import foreground
                    before_window = foreground(observed.get('runtime_state', {})) or {}
                    after_window = foreground(after.info.get('runtime_state', {})) or {}
                    if (observed.get('desktop_focus_stable') and before_window.get('shell_surface')
                            and after.info.get('desktop_focus_stable') and after_window.get('shell_surface')):
                        receipt.update(status='uncertain', error_code='run_dialog_not_confirmed',
                                       reason='Win+R 已派发，尚未确认“运行”对话框，前台仍是开始菜单或搜索。请重新观察，若菜单仍在，按 Esc 关闭后再按 Win+R。')
            return StepResult(observation=after, info=receipt)
        return StepResult(observation=self.observe(), info={
            "status": "rejected", "dispatched": False, "channel": "desktop",
            "error": "unsupported action or invalid coordinates"})

    def _prepare_monitor_input(self, action, origin):
        from ..desktop.desktop_readiness import foreground, window_identity, global_shortcut, launch_target
        from ..desktop.desktop_state import point_window
        from ..desktop.monitor_windows import window_fits
        scope = self._task_monitor
        monitor = scope.refresh()
        if self._desktop_capture.get('desktop_monitor', monitor) != monitor:
            return '任务屏幕的尺寸或 DPI 已变化，请重新观察'
        current_state = self._runtime_state()
        scope.guard_focus(current_state)
        current = foreground(current_state)
        previous = foreground(self._last_observation_info.get('runtime_state', {}))
        if (not current or window_identity(current) != window_identity(previous)
                or current.get('bounds') != previous.get('bounds') or current.get('title') != previous.get('title')):
            return '前台窗口已变化，请重新观察后再操作'
        if global_shortcut(action):
            keys = {str(k).lower() for k in action.args.get('keys') or [action.args.get('key')]}
            keys = {'win' if k in {'winleft', 'winright'} else k for k in keys}
            if keys in ({'alt','tab'}, {'alt','shift','tab'}) or 'win' in keys and keys & {'left','right'}:
                return '固定屏幕任务请按名称切换目标窗口，不能使用跨屏或盲切窗口的快捷键'
            if not launch_target(action) and not window_fits(current, monitor):
                return '当前前台在任务屏外，请先通过 switch_applications 确认目标窗口'
            if launch_target(action):
                scope.launch_baseline = {window_identity(w) for w in current_state.get('applications', [])}
            scope.shell_transition = keys in ({'win'}, {'win','r'}, {'win','s'})
            return ''
        target = current
        point = ('x1','y1') if action.action == 'drag_and_drop' else ('x','y')
        if all(k in action.args for k in point):
            hit = point_window(action.args[point[0]] + origin[0], action.args[point[1]] + origin[1])
            match = next((w for w in current_state['applications'] if w.get('hwnd') == hit), None)
            if match:
                target = match
        in_shell = (target.get('shell_surface') or target.get('class_name') in
                    {'Shell_TrayWnd','Shell_SecondaryTrayWnd','Progman','WorkerW'}
                    or target.get('class_name') == '#32770' and target.get('process_name', '').lower() == 'explorer.exe')
        if in_shell or scope.shell_transition:
            scope.expect_shell_action(action)
        if not window_fits(target, monitor):
            if not in_shell and all(k in action.args for k in point):
                scope.bind(target)
                return '已将点选目标移入任务屏，请按新截图定位'
            return '目标窗口位于任务屏外，请用 switch_applications 明确选择窗口'
        if in_shell:
            scope.shell_transition = True
        elif not scope.owns(target, current_state):
            scope.bind(target)
        keys = {str(k).casefold() for k in action.args.get('keys', [])}
        if not in_shell and action.action == 'hotkey' and keys == {'ctrl', 'n'}:
            scope.launch_process = target.get('process_name', '').casefold().removesuffix('.exe')
            scope.new_app_baseline = {window_identity(w) for w in current_state.get('applications', [])}
        return ''

    # ---- 浏览器操作 ----
    def browser_action(self, action_name: str, args: Dict[str, Any]) -> str:
        """执行 browser kind 动作。"""
        if action_name == "get_page_source":
            return self.get_page_source(target_ref=args.get("target_ref"), max_chars=args.get("max_chars", 20000))
        if action_name == "run_browser_js":
            return self.evaluate_js(args.get("js", ""), max_chars=args.get("max_chars", 12000))
        return f"未知浏览器动作 {action_name}"

    def get_page_source(self, target_ref=None, max_chars=20000) -> str:
        """Read the same task page used by observations and actions."""
        session = self._get_browser_session()
        if session is None:
            return "浏览器会话不可用，请重新观察。"
        return session.get_page_source(target_ref=target_ref, max_chars=max_chars)

    def evaluate_js(self, js: str, max_chars=12000) -> str:
        """在任务浏览器里执行一段 JavaScript（run_browser_js 动作）。"""
        if not js or not js.strip():
            return "run_browser_js 需要提供 js 代码"
        session = self._get_browser_session()
        if session is None:
            return "浏览器会话不可用，请重新观察。"
        return session.evaluate_js(js, max_chars=max_chars)

    def run_command(self, command: str) -> str:
        stripped = (command or "").strip()
        # `start` 用于启动 GUI 程序/打开文件：分离进程、不捕获输出、立即返回。
        # 否则 subprocess.run(capture_output=True) 会等被启动的 GUI 进程关闭其
        # 继承的 stdout/stderr 管道才结束，导致 run_in_terminal 卡到 120s 超时。
        if re.match(r'^start\b', stripped, re.IGNORECASE):
            try:
                subprocess.Popen(["cmd", "/c", command],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                return "已启动（后台进程，不等待退出）：" + command[:120]
            except Exception as exc:  # noqa: BLE001
                return f"命令执行失败: {exc}"
        try:
            r = subprocess.run(["cmd", "/c", command], capture_output=True, text=True,
                               timeout=120, encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return f"命令执行失败: {exc}"
        return (r.stdout or "").strip() or (r.stderr or "").strip() or "(无输出)"

    def run_python(self, code: str) -> str:
        try:
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               timeout=120, encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return f"python 执行失败: {exc}"
        return (r.stdout or "").strip() or (r.stderr or "").strip() or "(无输出)"

    def evaluate(self) -> float:
        # 本地手动任务没有独立评测器；框架通过任务契约和执行证据验证。
        return 0.0

    def close(self) -> None:
        with self._desktop_lock:
            try:
                if self._desktop_ax is not None:
                    self._desktop_ax.close()
            finally:
                self._desktop_ax = None
                self._desktop_capture = {}
                self._captured_layouts = ()
                self._captured_shell_windows.clear()
                self._last_observation_info = {}
                self._episode_prepared = False
                if self._desktop_lease is not None:
                    self._desktop_lease()
                    self._desktop_lease = None
        if self._browser_session is not None:
            try:
                self._browser_session.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser_session = None
