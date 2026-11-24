"""Physical virtual-desktop screenshots and image-to-desktop coordinates.

Browser screenshots never use this module. Pillow's Windows all_screens path
and Win32 metrics run under the same per-monitor DPI context.
"""
from contextlib import contextmanager
import copy
import hashlib
import math
import os
import sys
import time
import uuid

from ..actions import Action


def _user32_desktop():
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetThreadDesktop.argtypes, user.GetThreadDesktop.restype = [wintypes.DWORD], wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    return user


def _desktop_name(user, handle):
    import ctypes
    from ctypes import wintypes
    name, length = ctypes.create_unicode_buffer(256), wintypes.DWORD()
    if not user.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    return name.value


def windows_desktop_key():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    user = _user32_desktop()
    name = _desktop_name(user, user.GetThreadDesktop(kernel.GetCurrentThreadId()))
    return hashlib.sha256(f"{session.value}/{name}".encode()).hexdigest()[:12]


def input_desktop_active():
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    user = _user32_desktop()
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    active = user.OpenInputDesktop(0, False, 1)
    if not active:
        return False
    try:
        current = user.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId())
        return _desktop_name(user, current) == _desktop_name(user, active)
    except OSError:
        return False
    finally:
        user.CloseDesktop(active)


def acquire_desktop_lease():
    """The lifetime of the named kernel object grants this process exclusive input."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, "Local\\HybridAgentDesktop-" + windows_desktop_key())
    last_error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(last_error)
    if last_error == 183:  # ERROR_ALREADY_EXISTS
        kernel.CloseHandle(handle)
        raise RuntimeError("此 Windows 桌面已有活动任务使用鼠标、键盘和 AX，请等待该任务结束")
    return lambda: kernel.CloseHandle(handle)


@contextmanager
def physical_dpi_context():
    if sys.platform != 'win32':
        yield
        return
    import ctypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    setter = user.SetThreadDpiAwarenessContext
    setter.argtypes = [ctypes.c_void_p]
    setter.restype = ctypes.c_void_p
    previous = setter(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    if not previous:
        raise OSError('无法建立物理像素 DPI 上下文，拒绝猜测桌面坐标')
    try:
        yield
    finally:
        setter(previous)


def virtual_geometry():
    import ctypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    with physical_dpi_context():
        left, top, width, height = [int(user.GetSystemMetrics(i)) for i in (76, 77, 78, 79)]
    if width <= 0 or height <= 0:
        raise OSError('虚拟桌面尺寸不可用')
    return {'left': left, 'top': top, 'width': width, 'height': height}


def capture_virtual_desktop():
    from PIL import ImageGrab
    with physical_dpi_context():
        before = virtual_geometry()
        shot = ImageGrab.grab(all_screens=True)
        after = virtual_geometry()
    if before != after or shot.size != (before['width'], before['height']):
        raise OSError('显示器布局在截图时发生变化，请重新观察桌面')
    return shot, {'screen_origin': [before['left'], before['top']],
                  'virtual_desktop': before, 'desktop_capture_id': uuid.uuid4().hex,
                  'captured_at': time.time(), 'desktop_capture_source': 'win32_virtual_desktop',
                  'screenshot_size': list(shot.size)}


def validate_desktop_action(action, captured, current_geometry):
    """Validate image-local points. Only the adapter may supply the origin."""
    geometry = captured.get('virtual_desktop')
    if not geometry or geometry != current_geometry:
        raise ValueError('显示器布局已改变或缺少桌面截图，请重新 observe_desktop')
    attached = action.args.get('observation_metadata') or {}
    if attached and attached.get('desktop_capture_id') != captured.get('desktop_capture_id'):
        raise ValueError('动作来自过期桌面截图，请重新定位')
    pairs = [('x1', 'y1'), ('x2', 'y2')] if action.action == 'drag_and_drop' else [('x', 'y')]
    required = action.action in {'click', 'double_click', 'right_click', 'select', 'drag_and_drop'}
    for x, y in pairs:
        if not required and x not in action.args and y not in action.args:
            continue
        for field, limit in ((x, geometry['width']), (y, geometry['height'])):
            value = action.args.get(field)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < limit:
                raise ValueError('定位点超出当前虚拟桌面截图范围')
    return Action(action.action, copy.deepcopy(action.args)), (geometry['left'], geometry['top'])
