"""Read the committed filesystem location of the visible foreground Explorer view."""
from __future__ import annotations

import ntpath
import sys
import threading

from .desktop_readiness import foreground, matches_target, window_ready


_shell_read = threading.Lock()


def filesystem_path(value):
    """Accept absolute drive/UNC paths, excluding Shell namespaces and device paths."""
    if not isinstance(value, str) or not value:
        return ''
    path = ntpath.normpath(value)
    drive, tail = ntpath.splitdrive(path)
    if path.startswith(('\\\\?\\', '\\\\.\\')):
        return ''
    if len(drive) == 2 and drive[1] == ':' and tail.startswith('\\'):
        return path
    if drive.startswith('\\\\') and len(drive.strip('\\').split('\\')) == 2:
        return path
    return ''


def ready_explorer(obs):
    info = getattr(obs, 'info', None) or {}
    if (info.get('task_mode') != 'desktop' or not info.get('desktop_focus_stable')
            or info.get('screenshot_stale') or info.get('observation_error')
            or info.get('desktop_foreground_outside_task_monitor') or not info.get('desktop_capture_id')):
        return None
    runtime = info.get('runtime_state') or {}
    if runtime.get('source') != 'windows_window_api':
        return None
    window = foreground(runtime)
    if window_ready(window) and matches_target(window, {'process_name': 'explorer.exe'}):
        return window
    return None


def committed_explorer_path(obs):
    window = ready_explorer(obs)
    if window:
        location = obs.info.get('explorer_location') or {}
        if location.get('status') == 'observed' and location.get('hwnd') == window['hwnd']:
            return filesystem_path(location.get('path'))
    return ''


def _visible_view(windows, hwnd, get_view, displayed):
    """A single owning HWND may expose several Shell tabs. Ambiguity is unknown."""
    visible = {}
    for window in windows:
        if int(window.HWND) == hwnd:
            view = get_view(window)
            view_hwnd = view.GetWindow()
            if displayed(view_hwnd):
                visible[view_hwnd] = view
    return next(iter(visible.items())) if len(visible) == 1 else (None, None)


def _read_active_folder(hwnd, pid):
    import pythoncom
    import win32com.client
    import win32gui
    import win32process
    from win32com.shell import shell, shellcon

    def same_foreground():
        return win32gui.GetForegroundWindow() == hwnd and win32process.GetWindowThreadProcessId(hwnd)[1] == pid

    def displayed(view_hwnd):
        if not win32gui.IsChild(hwnd, view_hwnd) or not win32gui.IsWindowVisible(view_hwnd):
            return False
        left, top, right, bottom = win32gui.GetClientRect(view_hwnd)
        if right <= left or bottom <= top:
            return False
        # Win11 can keep multiple tabs' views WS_VISIBLE. Require this view (or
        # its child) to actually receive a hit inside its on-screen client area.
        center = win32gui.ClientToScreen(view_hwnd, ((left + right) // 2, (top + bottom) // 2))
        hit = win32gui.WindowFromPoint(center)
        return hit == view_hwnd or bool(hit and win32gui.IsChild(view_hwnd, hit))

    def get_view(window):
        provider = window._oleobj_.QueryInterface(pythoncom.IID_IServiceProvider)
        browser = provider.QueryService(shell.SID_STopLevelBrowser, shell.IID_IShellBrowser)
        return browser.QueryActiveShellView()

    if not same_foreground():
        return ''
    windows = win32com.client.Dispatch('Shell.Application').Windows()
    view_hwnd, view = _visible_view(windows, hwnd, get_view, displayed)
    if view is None:
        return ''
    folder = view.QueryInterface(shell.IID_IFolderView).GetFolder(shell.IID_IShellFolder)
    pidl = folder.QueryInterface(shell.IID_IPersistFolder2).GetCurFolder()
    path = shell.SHGetNameFromIDList(pidl, shellcon.SIGDN_FILESYSPATH)
    return filesystem_path(path) if same_foreground() and displayed(view_hwnd) else ''


def observe_explorer_location(window):
    """Bound Shell latency and never reuse the result of a timed-out read."""
    if not (window_ready(window) and matches_target(window, {'process_name': 'explorer.exe'})):
        return None
    hwnd = window['hwnd']
    location = {'source': 'windows_shell_active_view', 'status': 'unknown', 'hwnd': hwnd}
    if sys.platform != 'win32' or not _shell_read.acquire(blocking=False):
        return location
    finished = threading.Event()
    paths = []

    def read():
        try:
            import pythoncom
            from .desktop_capture import physical_dpi_context
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            try:
                with physical_dpi_context():
                    paths.append(_read_active_folder(hwnd, window['pid']))
            finally:
                pythoncom.CoUninitialize()
        except Exception:
            # Non-filesystem views, teardown during navigation and unreachable
            # COM servers (including pywintypes.com_error) have no reliable path.
            pass
        finally:
            _shell_read.release()
            finished.set()

    worker = threading.Thread(target=read, name='explorer-location', daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _shell_read.release()
        return location
    if finished.wait(.35) and paths and paths[0]:
        location.update(status='observed', path=paths[0])
    return location
