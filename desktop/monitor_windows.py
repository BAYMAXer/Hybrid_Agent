"""Bind intentional desktop window transitions to one physical Windows display."""
from __future__ import annotations

import ctypes
import re
from ctypes import wintypes

from .desktop_capture import physical_dpi_context, resolve_desktop_monitor
from .desktop_readiness import foreground, window_identity


class MonitorStop(RuntimeError):
    def __init__(self, code, reason):
        self.code = code
        super().__init__(reason)


def window_fits(window, monitor):
    rect = window.get('frame_bounds') or window.get('bounds')
    if not rect or len(rect) != 4:
        return False
    x, y = monitor['left'], monitor['top']
    return (x <= rect[0] < rect[2] <= x + monitor['width']
            and y <= rect[1] < rect[3] <= y + monitor['height'])


def document_title(title):
    """The document component of a normal Word/Excel/Notepad title."""
    return re.split(r'\s+[–—-]\s+', str(title).strip().lstrip('*'), maxsplit=1)[0].strip().casefold()


def task_named_windows(candidates, task):
    # A process name or a generic application caption does not identify a document.
    text = str(task or '').casefold()
    found = []
    for window in candidates:
        name = document_title(window.get('title', ''))
        if not name:
            continue
        pattern = re.escape(name)
        if not re.search(r'\.[a-z0-9]{1,8}$', name):
            # Office frequently hides known extensions in its caption.
            suffix = {'winword.exe': r'docx?', 'excel.exe': r'xlsx?', 'powerpnt.exe': r'pptx?'}.get(
                window.get('process_name', '').casefold())
            if not suffix:
                continue
            pattern += r'\.(?:' + suffix + ')'
        if re.search(r'(?<![\w.\-])' + pattern + r'(?![a-z0-9_.\-])', text, re.ASCII):
            found.append(window)
    return found


def _move_to_work_area(window, monitor):
    """Re-check the real HWND/PID/title before mutating it and verify a physical frame afterwards."""
    from .desktop_state import windows_runtime_state
    hwnd = window['hwnd']
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.IsZoomed.argtypes = user.IsIconic.argtypes = [wintypes.HWND]
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]

    def read_target():
        current = next((w for w in windows_runtime_state(include_layout=True)['applications']
                        if window_identity(w) == window_identity(window) and w['title'] == window['title']), None)
        if not current:
            raise MonitorStop('desktop_window_identity_changed', '移动前后窗口身份发生变化，已停止桌面输入')
        return current

    with physical_dpi_context():
        current = read_target()
        maximized = bool(user.IsZoomed(hwnd))
        if maximized or user.IsIconic(hwnd):
            user.ShowWindow(hwnd, 9)  # SW_RESTORE; move normal placement before maximizing again.
            current = read_target()
        left, top, right, bottom = monitor['work_area']
        for _ in range(2):  # WM_DPICHANGED can alter the rect on the first cross-DPI move.
            raw = current.get('bounds')
            frame = current.get('frame_bounds') or raw
            if not frame or not raw:
                raise MonitorStop('desktop_window_move_failed', '无法核对目标窗口边界')
            width, height = min(frame[2]-frame[0], right-left), min(frame[3]-frame[1], bottom-top)
            x = left + (right-left-width)//2 + raw[0]-frame[0]
            y = top + (bottom-top-height)//2 + raw[1]-frame[1]
            width += raw[2]-raw[0] - (frame[2]-frame[0])
            height += raw[3]-raw[1] - (frame[3]-frame[1])
            if not user.SetWindowPos(hwnd, None, x, y, width, height, 0x14):  # NOZORDER | NOACTIVATE
                raise MonitorStop('desktop_window_move_failed', 'Windows 未能将目标窗口移入任务屏幕')
            current = read_target()
            if window_fits(current, monitor):
                break
        if maximized:
            user.ShowWindow(hwnd, 3)
            current = read_target()
        if not window_fits(current, monitor):
            raise MonitorStop('desktop_window_move_failed', '目标窗口归位后仍超出任务屏幕，已停止输入')


class TaskMonitor:
    def __init__(self, selector):
        self.monitor = resolve_desktop_monitor(selector)
        self.root = None
        self.moved = False
        self.shell_transition = False
        self.launch_process = ''
        self.new_app_baseline = None
        self.launch_baseline = None

    def refresh(self):
        try:
            self.monitor = resolve_desktop_monitor(self.monitor['id'])
        except (ValueError, OSError) as exc:
            raise MonitorStop('desktop_monitor_unavailable', str(exc)) from exc
        return self.monitor

    def fit(self, window):
        monitor = self.refresh()
        if not window_fits(window, monitor) or window.get('minimized'):
            if window.get('shell_surface') or window.get('class_name') in (
                    'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd'):
                raise MonitorStop('desktop_window_outside_monitor', 'Windows 系统界面位于任务屏幕之外，已停止输入')
            try:
                _move_to_work_area(window, monitor)
            except OSError as exc:
                raise MonitorStop('desktop_window_move_failed', f'无法归位目标窗口：{exc}') from exc
            self.moved = True

    def bind(self, window):
        self.fit(window)
        self.root = window_identity(window)
        self.shell_transition = False
        self.launch_process = ''
        self.new_app_baseline = None
        self.launch_baseline = None

    def launch_state(self, state, target):
        """Win+E must not adopt an already-open Explorer window during launch."""
        if self.launch_baseline is None or target.get('hwnd'):
            return state
        from .desktop_readiness import matches_target
        candidates = [w for w in state.get('applications', []) if matches_target(w, target)
                      and window_identity(w) not in self.launch_baseline and not w.get('cloaked')]
        if len(candidates) > 1:
            raise MonitorStop('desktop_window_ambiguous', '启动后出现多个符合条件的资源管理器窗口，无法确认任务目标')
        return {**state, 'applications': candidates}

    def expect_shell_action(self, action):
        """A command typed into Run/Search or a named pinned application grants a process hint."""
        text = str(action.args.get('text') or action.args.get('target') or '').strip().casefold()
        aliases = {'notepad': 'notepad', '记事本': 'notepad', 'word': 'winword', 'winword': 'winword',
                   'powerpoint': 'powerpnt', 'excel': 'excel', '计算器': 'calculatorapp',
                   'calculator': 'calculatorapp', 'calc': 'calculatorapp', '画图': 'mspaint',
                   'paint': 'mspaint', '资源管理器': 'explorer'}
        exe = re.fullmatch(r'([a-z0-9_-]+)\.exe', text)
        if exe:
            self.launch_process = exe[1]
        else:
            for label, process in aliases.items():
                if re.search(r'(?<![a-z0-9])' + re.escape(label) + r'(?![a-z0-9])', text):
                    self.launch_process = process
                    break

    def owns(self, window, state):
        if not self.root:
            return False
        windows = {w['hwnd']: w for w in state.get('applications', []) if w.get('hwnd')}
        owner = window
        seen = set()
        while owner and owner.get('hwnd') not in seen:
            if window_identity(owner) == self.root:
                return True
            seen.add(owner.get('hwnd'))
            owner = windows.get(owner.get('owner_hwnd'))
        return False

    def guard_focus(self, state):
        """Check again at native dispatch; the user may switch during observation or inference."""
        current = foreground(state)
        if self.root and not self.owns(current, state) and not self.shell_window(current):
            raise MonitorStop('desktop_window_focus_changed', '前台已切换至未确认的任务窗口，已停止桌面输入')

    def shell_window(self, window):
        return bool(window and self.shell_transition and (window.get('shell_surface')
                    or window.get('class_name') == '#32770' and window.get('process_name', '').casefold() == 'explorer.exe'))

    def prepare_observation(self, state, task):
        """Only an explicit target/owner relationship grants off-screen relocation."""
        monitor = self.refresh()
        win = foreground(state)
        if not win:
            return
        ident = window_identity(win)
        if (self.launch_process and win.get('process_name', '').casefold().removesuffix('.exe') == self.launch_process
                and (self.new_app_baseline is None or ident not in self.new_app_baseline)):
            candidates = [w for w in state.get('applications', []) if w.get('process_name') == win.get('process_name')
                          and not w.get('cloaked') and not w.get('owner_hwnd')
                          and (self.new_app_baseline is None or window_identity(w) not in self.new_app_baseline)]
            named = task_named_windows(candidates, task)
            candidates = named or candidates
            if len(candidates) != 1 or window_identity(candidates[0]) != ident:
                raise MonitorStop('desktop_window_ambiguous', '启动操作匹配多个窗口，无法确认要移动的目标')
            self.bind(win)
        elif self.owns(win, state):
            self.fit(win)
        elif self.shell_window(win):
            if not window_fits(win, monitor):
                raise MonitorStop('desktop_window_outside_monitor', '启动器或系统弹窗位于任务屏外，已停止输入')
        elif self.root:
            self.guard_focus(state)
        elif (matches := task_named_windows(state.get('applications', []), task)) and len(matches) == 1:
            if window_identity(matches[0]) == ident:
                self.bind(win)
