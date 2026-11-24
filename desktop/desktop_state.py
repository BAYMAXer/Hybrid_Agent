"""OS window evidence and targeted window activation, never file operations."""
import os
import sys
import re
import time


def windows_runtime_state(*, include_layout=False):
    if sys.platform != "win32":
        return {"applications": [], "complete": False}
    try:
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        for name in ('IsIconic', 'IsWindowEnabled', 'IsHungAppWindow'):
            getattr(user, name).argtypes = [wintypes.HWND]
        dwm = ctypes.WinDLL('dwmapi', use_last_error=True)
        dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        foreground = user.GetForegroundWindow()
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        apps, errors, layout = [], [], []

        @callback_type
        def visit(hwnd, _):
            handle = None
            try:
                if not user.IsWindowVisible(hwnd):
                    return True
                title = ctypes.create_unicode_buffer(1024)
                user.GetWindowTextW(hwnd, title, len(title))
                class_name = ctypes.create_unicode_buffer(256)
                user.GetClassNameW(hwnd, class_name, len(class_name))
                rect, cloaked = wintypes.RECT(), wintypes.DWORD()
                bounds = ([rect.left, rect.top, rect.right, rect.bottom]
                          if user.GetWindowRect(hwnd, ctypes.byref(rect)) else None)
                cloak_ok = dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0
                if include_layout and cloak_ok and cloaked.value:
                    return True
                if include_layout and bounds and not user.IsIconic(hwnd) and not cloaked.value:
                    layout.append((int(hwnd), tuple(bounds)))
                if not title.value and not (hwnd == foreground and class_name.value in
                                            {'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd'}):
                    return True
                pid = wintypes.DWORD()
                user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                handle = kernel.OpenProcess(0x1000, False, pid.value)
                path = ctypes.create_unicode_buffer(32768)
                length = wintypes.DWORD(len(path))
                if not handle or not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                    errors.append(True)
                    return True
                apps.append({"process_name": os.path.basename(path.value), "pid": pid.value, "title": title.value,
                             "hwnd": int(hwnd), "class_name": class_name.value, "foreground": hwnd == foreground,
                             "bounds": bounds, "visible": True, "minimized": bool(user.IsIconic(hwnd)),
                             "enabled": bool(user.IsWindowEnabled(hwnd)), "responsive": not bool(user.IsHungAppWindow(hwnd)),
                             "cloaked": bool(cloaked.value) if cloak_ok else None})
            except Exception:
                errors.append(True)
            finally:
                if handle:
                    kernel.CloseHandle(handle)
            return True

        if include_layout:
            from .desktop_capture import physical_dpi_context
            with physical_dpi_context():
                complete = bool(user.EnumWindows(visit, 0)) and not errors
        else:
            complete = bool(user.EnumWindows(visit, 0)) and not errors
        return {"applications": apps, "complete": complete, "source": "windows_window_api",
                **({"_window_layout": layout} if include_layout else {})}
    except Exception:
        return {"applications": [], "complete": False}


# Aliases identify applications; they are never interpreted as commands/paths.
_APP_ALIASES = {
    'file_explorer': 'explorer', 'file explorer': 'explorer', 'windows_explorer': 'explorer',
    'windows explorer': 'explorer', 'explorer': 'explorer', 'explorer.exe': 'explorer',
    'file_manager': 'explorer', 'file manager': 'explorer', '资源管理器': 'explorer', '文件管理器': 'explorer',
    '文件资源管理器': 'explorer', 'windows_file_explorer': 'explorer',
    'chrome': 'chrome', 'google_chrome': 'chrome', 'google chrome': 'chrome',
    'edge': 'msedge', 'microsoft_edge': 'msedge', 'microsoft edge': 'msedge',
    'firefox': 'firefox', 'notepad': 'notepad',
}


def windows_application_process(app_code):
    code = str(app_code or '').strip().casefold()
    if code in _APP_ALIASES:
        return _APP_ALIASES[code]
    if re.fullmatch(r'[a-z0-9_-]+(?:\.exe)?', code):
        return code.removesuffix('.exe')
    return None


def pointer_guard(action, origin, browser_handles, layouts, *, leaving_browser=False):
    """Recheck local window geometry at the gesture points, including background windows."""
    if sys.platform != 'win32':
        return ""
    import ctypes
    import math
    from ctypes import wintypes
    from .desktop_capture import physical_dpi_context
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.WindowFromPoint.argtypes, user.WindowFromPoint.restype = [wintypes.POINT], wintypes.HWND
    user.GetAncestor.argtypes, user.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    fields = [('x1', 'y1'), ('x2', 'y2')] if action.action == 'drag_and_drop' else [('x', 'y')]
    with physical_dpi_context():
        points = []
        if action.action == 'scroll':
            point = wintypes.POINT()
            if not user.GetCursorPos(ctypes.byref(point)):
                raise OSError('无法确认当前滚动落点')
            points.append(point)
        else:
            for x, y in fields:
                values = [action.args.get(x), action.args.get(y)]
                if all(type(v) in (int, float) and math.isfinite(v) and 0 <= v < 2**30 for v in values):
                    points.append(wintypes.POINT(int(values[0]) + origin[0], int(values[1]) + origin[1]))
        for p in points:
            owner = user.GetAncestor(user.WindowFromPoint(p), 2)
            if owner in browser_handles or (leaving_browser and not owner):
                return "browser_task_out_of_scope"
            if layouts and not stable_point_layout((p.x, p.y), layouts, owner=owner):
                return "desktop_window_geometry_changed"
    return "browser_task_out_of_scope" if leaving_browser and not points else ""


def stable_point_layout(point, layouts, *, owner=None):
    """Compare overlapping layers through the live owner, including click-through overlays."""
    x, y = point
    samples = []
    for layout in layouts:
        at_point = []
        for hwnd, bounds in layout:
            if bounds[0] <= x < bounds[2] and bounds[1] <= y < bounds[3]:
                at_point.append((hwnd, tuple(bounds)))
                if not owner or hwnd == owner:
                    break
        samples.append(at_point)
    return all(sample == samples[0] for sample in samples)


def common_file_dialog(window, block):
    """Identify the native shell file picker and its separate overwrite prompt."""
    if not window or window.get("class_name") != "#32770":
        return "", ""
    title = str(window.get("title", "")).strip().casefold()
    nodes = (block or {}).get("nodes", [])
    text = " ".join(str(n.get("name", "")) for n in nodes if n.get("role") == "text")
    if (title in {"确认另存为", "confirm save as", "confirm file replace"}
            or (title == "确认替换" and any(term in text for term in ("文件", "已存在")))):
        return "overwrite", text
    if title not in {"另存为", "保存为", "保存", "打开", "选择文件", "save as", "save", "open", "select file"}:
        return "", ""
    if any(any(tag in str(n.get("name", "")).casefold() for tag in ("文件名", "file name", "filename"))
           for n in nodes):
        return "picker", ""
    if sys.platform != "win32" or not window.get("hwnd"):
        return "", ""
    # Accessibility can be unavailable. FileNameControlHost / cmb13 or edt1
    # plus the shell view distinguish the Explorer-style picker from another
    # application's own dialog with the same title.
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumChildWindows.argtypes = [wintypes.HWND, callback, wintypes.LPARAM]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetDlgCtrlID.argtypes = [wintypes.HWND]
    classes, ids = set(), set()
    @callback
    def visit(child, _):
        buf = ctypes.create_unicode_buffer(128)
        user.GetClassNameW(child, buf, len(buf))
        classes.add(buf.value)
        ids.add(user.GetDlgCtrlID(child))
        return True
    user.EnumChildWindows(window["hwnd"], visit, 0)
    shell = bool(classes & {"SHELLDLL_DefView", "DUIViewWndClassName", "NamespaceTreeControl"})
    return ("picker", "") if shell and (ids & {0x47C, 0x480} or "ComboBox" in classes) else ("", "")


def _activate_window(hwnd):
    """Return dispatch and observed foreground separately; Windows can deny focus.

    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow
    """
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    for name in ('IsWindow', 'IsIconic', 'SetForegroundWindow'):
        fn = getattr(user, name)
        fn.argtypes, fn.restype = [wintypes.HWND], wintypes.BOOL
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.ShowWindow.restype = wintypes.BOOL
    user.GetForegroundWindow.restype = wintypes.HWND
    if not user.IsWindow(hwnd):
        return False, False
    if user.IsIconic(hwnd):
        user.ShowWindow(hwnd, 9)  # SW_RESTORE; keep already-visible window sizing.
    user.SetForegroundWindow(hwnd)
    deadline = time.monotonic() + .5
    while True:
        if user.GetForegroundWindow() == hwnd:
            return True, True
        if time.monotonic() >= deadline:
            return True, False
        time.sleep(.05)


def switch_windows_application(app_code):
    """Activate one matching existing window, or report why no switch occurred."""
    process = windows_application_process(app_code)
    result = {'action': 'switch_applications', 'app_code': app_code, 'channel': 'desktop', 'dispatched': False}
    if not process:
        return {**result, 'status': 'rejected', 'error_code': 'unknown_application',
                'reason': 'app_code 必须是应用标识或进程名，不能是命令；也可通过任务栏或系统启动器操作。'}
    runtime = windows_runtime_state()
    matches = [a for a in runtime.get('applications', [])
               if str(a.get('process_name', '')).casefold().removesuffix('.exe') == process and a.get('hwnd')
               and a.get('cloaked') is not True]
    if process == 'explorer':
        # explorer.exe also owns the desktop and taskbar; those are not folders.
        matches = [a for a in matches if a.get('class_name') in {'CabinetWClass', 'ExploreWClass'}]
    current = next((a for a in matches if a.get('foreground')), None)
    if current:
        return {**result, 'status': 'observed', 'reason': '目标应用已在前台，请继续操作窗口内容，不要反复切换。',
                'window': current}
    if not matches:
        reason = ('未找到已打开的资源管理器窗口；请使用 hotkey keys=["win","e"] 打开后，根据新截图继续。'
                  if process == 'explorer' else '未找到目标应用窗口；请通过系统启动器打开，或根据桌面截图选择应用。')
        return {**result, 'status': 'rejected', 'error_code': 'application_not_found', 'reason': reason}
    if len(matches) != 1:
        return {**result, 'status': 'rejected', 'error_code': 'ambiguous_application',
                'reason': '有多个目标应用窗口，请通过任务栏或任务切换界面按标题选择，不会盲按 Alt+Tab。',
                'candidates': [a.get('title', '') for a in matches[:8]]}
    target = matches[0]
    try:
        dispatched, focused = _activate_window(target['hwnd'])
    except Exception as exc:
        return {**result, 'status': 'uncertain', 'dispatched': True, 'error_code': 'activation_error',
                'reason': f'窗口激活结果不确定（{type(exc).__name__}）；请检查桌面截图。'}
    if not focused:
        return {**result, 'status': 'uncertain' if dispatched else 'rejected', 'dispatched': dispatched,
                'error_code': 'foreground_not_confirmed',
                'window': target,
                'reason': '目标窗口未确认进入前台；请用任务栏或系统快捷键选择窗口，不能当成切换成功。'}
    return {**result, 'status': 'executed', 'dispatched': dispatched,
            'reason': '已确认目标应用窗口进入前台。', 'window': target}
