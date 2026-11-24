"""Optional HCU Windows UI Automation channel, bound to one local desktop frame."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid

from .desktop_readiness import window_identity
from .desktop_capture import windows_desktop_key


REVISION = "697d3fbda04cb322915866bab1aad79c726a61e3"
_SDK_IMPORT_LOCK = threading.Lock()


def _runtime_directory():
    override = os.environ.get("HCU_RUNTIME")
    if override:
        path = Path(override).expanduser()
        return path.parent if path.suffix.lower() == ".exe" else path
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HybridAgent" / "hcu" / REVISION[:7]


def _connect(session_id):
    root = _runtime_directory()
    cli = root / "computer-use.exe"
    provider = root / "computer-use-windows-provider.exe"
    sdk = root / "hcu_sdk" / "__init__.py"
    if not all(p.is_file() for p in (cli, provider, sdk, root / "REVISION")):
        raise FileNotFoundError(f"缺少 HCU Windows 组件，请运行 install-hcu-windows.ps1 或配置 HCU_RUNTIME ({root})")
    if (root / "REVISION").read_text(encoding="utf-8-sig").strip() != REVISION:
        raise ValueError("HCU Windows 运行时版本与当前适配器不匹配")
    # Package name includes the installed source directory so relative SDK imports
    # (notably the Windows AF_UNIX transport) always resolve to these pinned files.
    name = "_hybrid_hcu_sdk_" + uuid.uuid5(uuid.NAMESPACE_URL, str(sdk.resolve())).hex
    with _SDK_IMPORT_LOCK:
        module = sys.modules.get(name)
        if module is None:
            spec = importlib.util.spec_from_file_location(name, sdk)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
    # Keep the daemon for the pinned runtime separate from third-party HCU
    # clients. Every task also has an independent daemon session and ref space.
    socket = str(Path(module.default_socket()).with_name(f"h-{REVISION[:7]}-{windows_desktop_key()}.sock"))
    result = subprocess.run([str(cli), "daemon", "start", "--socket", socket],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=12,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[:400] or "HCU daemon 未启动")
    return module.Desktop(module.SocketTransport(path=socket, session=session_id, timeout=6, start=False), retries=0)


def browser_window(window):
    """Native common dialogs and embedded WebViews have different window identities."""
    if not window:
        return False
    process = str(window.get("process_name", "")).casefold().removesuffix(".exe")
    cls = str(window.get("class_name", "")).casefold()
    if process in {"chrome", "msedge", "brave", "opera", "vivaldi", "chromium", "arc"}:
        return cls.startswith("chrome_widgetwin_")
    return ((process == "firefox" and cls == "mozillawindowclass")
            or (process == "iexplore" and cls == "ieframe"))


def _reject(code, reason):
    return {"channel": "reobserve", "source": "desktop_ax", "error_code": code, "reason": reason}


def _node_actions(row):
    if not row.get("ref"):
        return []
    states = row.get("states") or {}
    if str(states.get("disabled", "")).lower() == "true" or str(states.get("enabled", "")).lower() == "false":
        return []
    explicit = row.get("actions")
    if explicit:
        return [a for a in explicit if a in {"click", "type", "set_value", "select"}]
    # The pinned Windows provider mints actionable refs even when UIA omits
    # the optional action list. ResolveAndAct checks the live control pattern.
    role = row.get("role")
    if role in {"textbox", "searchbox", "spinbutton"}:
        return ["click"] if str(states.get("readonly", "")).lower() == "true" else ["click", "type", "set_value"]
    if role in {"option", "listitem", "tab", "treeitem", "radio"}:
        return ["click", "select"]
    if role in {"button", "link", "menuitem", "menuitemcheckbox", "menuitemradio", "checkbox",
                "switch", "combobox", "slider", "cell", "gridcell"}:
        return ["click"]
    return []


class DesktopAX:
    def __init__(self, enabled=True, connect=_connect):
        self.enabled = enabled
        self._connect = connect
        self.client = None
        self.session_id = "agent-" + uuid.uuid4().hex
        self.unavailable = ""
        self.window = None
        self.block = None
        self.raw_refs = {}
        self.failed_intents = {}

    def invalidate(self):
        self.window = self.block = None
        self.raw_refs.clear()

    def observe(self, window, capture_id):
        self.invalidate()
        if not self.enabled:
            return None, "disabled"
        if not window or not capture_id or browser_window(window):
            return None, "no_eligible_foreground_window"
        if self.unavailable:
            return None, self.unavailable
        if self.client is None:
            try:
                self.client = self._connect(self.session_id)
            except Exception as err:  # An incomplete optional installation must leave the visual channel available.
                self.unavailable = str(err)[:400]
                return None, self.unavailable
        try:
            inventory = self.client.apps()
            name = str(window["process_name"]).casefold().removesuffix(".exe")
            matches = [w for w in inventory.get("windows", []) if w.get("ref")
                       and w.get("title") == window["title"]
                       and str(w.get("app", "")).casefold().removesuffix(".exe") == name]
            if len(matches) != 1:
                return None, "window_not_unique_in_hcu_inventory"
            record = self.client.observe(window_ref=matches[0]["ref"])
        except Exception as err:  # Optional provider can reject a single inaccessible application.
            return None, str(err)[:400]
        app, native_window = record.get("app") or {}, record.get("window") or {}
        if (app.get("pid") != window.get("pid") or native_window.get("ref") != matches[0]["ref"]
                or native_window.get("title") != window["title"] or not record.get("snapshot_id")):
            return None, "window_changed_during_ax_observe"
        # Preserve ancestors for selection while exposing only bounded records.
        rows = record.get("elements") or []
        nodes = []
        prefix = f"desktop:{self.session_id}:{capture_id}:"
        interactive = [i for i, row in enumerate(rows) if _node_actions(row)]
        selected = set(interactive[:120])
        # Keep native tree order, reserving room for title/status/value evidence.
        for i, row in enumerate(rows):
            if len(selected) >= 160:
                break
            if row.get("name") or row.get("value") or row.get("role") in {"window", "dialog"}:
                selected.add(i)
        for i in sorted(selected):
            row = rows[i]
            node = {k: str(row[k])[:400] for k in ("role", "name", "value", "description") if k in row}
            raw = row.get("ref")
            actions = _node_actions(row)
            if raw:
                rejected = self.failed_intents.get((window_identity(window), window["title"], raw), set())
                actions = [intent for intent in actions if intent not in rejected]
            node.update(states=dict(row.get("states") or {}), actions=actions, direct=bool(raw and actions))
            if raw:
                node["ref"] = prefix + raw
                self.raw_refs[node["ref"]] = raw
            if row.get("parent"):
                node["parent"] = prefix + row["parent"]
            nodes.append(node)
        self.window = dict(window)
        self.block = {"kind": "desktop_ax", "window_ref": native_window["ref"], "title": native_window["title"],
                      "app": app.get("name", name), "pid": app["pid"], "snapshot_id": record["snapshot_id"],
                      "desktop_capture_id": capture_id, "nodes": nodes, "truncated": len(rows) > len(nodes)}
        return self.block, ""

    def route(self, action, observation):
        ref = action.args.get("target_ref")
        if not ref:
            return None
        if (not self.block or ref not in self.raw_refs or observation is None
                or self.block["desktop_capture_id"] != observation.info.get("desktop_capture_id")):
            return _reject("stale_desktop_ref", "控件引用已失效，请重新观察桌面")
        exposed = (observation.info.get("exposed_refs") or ()) if "exposed_refs" in observation.info else None
        if exposed is not None and ref not in exposed:
            return _reject("unexposed_desktop_ref", "此控件引用未出现在本轮决策输入，请重新观察")
        by_ref = {node["ref"]: node for node in self.block["nodes"] if node.get("ref")}
        node = by_ref[ref]
        intent = action.action
        if intent == "type" and action.args.get("overwrite") is True:
            intent = "set_value"
        if intent == "select":
            option = action.args.get("option")
            def belongs_to_target(candidate):
                visited = set()
                parent = candidate.get("parent")
                while parent in by_ref and parent not in visited:
                    if parent == ref:
                        return True
                    visited.add(parent)
                    parent = by_ref[parent].get("parent")
                return False
            choices = [candidate for candidate in by_ref.values() if candidate.get("name") == option
                       and "select" in candidate["actions"]
                       and (candidate["ref"] == ref or belongs_to_target(candidate))]
            if len(choices) != 1:
                return _reject("desktop_option_not_unique", "请展开选项并观察唯一可选控件")
            node = choices[0]
            if exposed is not None and node["ref"] not in exposed:
                return _reject("unexposed_desktop_ref", "本轮未展示该选项，请展开并重新观察")
        if intent not in node["actions"] or intent not in {"click", "type", "set_value", "select"}:
            return _reject("unsupported_desktop_ax_action", "当前控件不支持此动作，请重新观察并选择通道")
        return {"channel": "desktop_ax", "target_ref": node["ref"], "intent": intent,
                "window_ref": self.block["window_ref"], "snapshot_id": self.block["snapshot_id"],
                "desktop_capture_id": self.block["desktop_capture_id"]}

    def execute(self, action, route, current_window):
        result = {"channel": "desktop_ax", "target_ref": route.get("target_ref"),
                  "window_ref": route.get("window_ref"), "snapshot_id": route.get("snapshot_id")}
        ref = self.raw_refs.get(route.get("target_ref"))
        foreground = current_window()
        if (not ref or not self.block or not foreground or window_identity(foreground) != window_identity(self.window)
                or foreground.get("title") != self.window.get("title")
                or any(route.get(k) != self.block[k] for k in ("window_ref", "snapshot_id", "desktop_capture_id"))):
            return {**result, "status": "rejected", "dispatched": False, "error_code": "stale_desktop_ref"}
        intent = route["intent"]
        target = next((n for n in self.block["nodes"] if n.get("ref") == route["target_ref"]), None)
        try:
            if intent == "select":
                matches = self.client.find(window_ref=route["window_ref"], name=action.args["option"], limit=40)
                if not any(n.get("ref") == ref for n in matches.get("matches", [])):
                    raise ValueError("当前选项已改变，请展开并重新观察")
            read = self.client.read([ref])
            live = next((r.get("element") for r in read.get("read", []) if r.get("ok") and r.get("ref") == ref), None)
            if (not target or not live or live.get("role") != target.get("role") or
                    str(live.get("name", ""))[:400] != target.get("name", "") or intent not in _node_actions(live)):
                raise ValueError("控件已变化，请重新观察")
        except Exception as err:
            return {**result, "status": "rejected", "dispatched": False, "error_code": "desktop_ax_readback_changed",
                    "reason": str(err)[:400]}
        foreground = current_window()
        if (not foreground or window_identity(foreground) != window_identity(self.window)
                or foreground.get("title") != self.window["title"]):
            return {**result, "status": "rejected", "dispatched": False, "error_code": "desktop_focus_changed"}
        step = {"type": intent, "ref": ref}
        if intent in {"type", "set_value"}:
            step["text" if intent == "type" else "value"] = action.args["text"]
        request = uuid.uuid4().hex
        result["request_id"] = request
        cache_key = window_identity(self.window), self.window["title"], ref
        self.invalidate()  # An observed frame can dispatch precisely once.
        try:
            record = self.client.act([step], window_ref=route["window_ref"], request_id=request, observe="none")
        except Exception as err:
            record = getattr(err, "record", None)
            if not isinstance(record, dict):
                return {**result, "status": "uncertain", "dispatched": None,
                        "error_code": "desktop_ax_transport_unknown", "reason": str(err)[:400],
                        "continuation": {"state": "stop", "retry_same_request": False}}
        evidence = record.get("evidence") or {}
        continuation = evidence.get("continuation") or record.get("continuation") or {}
        sent = bool(record.get("input_emitted"))
        status = "executed" if record.get("ok") else ("uncertain" if sent or continuation.get("state") == "stop" else "rejected")
        reason = str(record.get("message", ""))[:400]
        if status == "rejected" and (continuation.get("retry_same_request") is False
                                     or record.get("code") in {"unsupported", "unsupported_action"}):
            # A Windows role can advertise a possible method even when the
            # application's live UIA pattern rejects it. Retire that method
            # for this logical control; other controls and GUI remain usable.
            self.failed_intents.setdefault(cache_key, set()).add(intent)
            reason += " 当前窗口的此控件已拒绝该操作，请选择其他控件、GUI 或键盘路径。"
        return {**result, "status": status, "dispatched": sent, "continuation": continuation,
                "error_code": record.get("code", ""), "reason": reason,
                "completed": record.get("completed"), "replayed": bool(record.get("replayed")),
                "readback": {key: evidence[key] for key in ("readback_verified", "effect", "runtime_action_evidence")
                             if key in evidence}}

    def close(self):
        self.invalidate()
        if self.client is not None:
            self.client.close()
            self.client = None
