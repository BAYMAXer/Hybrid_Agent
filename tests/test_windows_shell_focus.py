"""Observe and use Start without sending input to the real Windows desktop."""
import ctypes
import os
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from PIL import Image

from hybrid_agent.actions import Action
from hybrid_agent.adapters import windows_env
from hybrid_agent.desktop import desktop_capture, desktop_state
from hybrid_agent.desktop.desktop_readiness import foreground


PINNED_APP = {"target": "pinned app", "coordinate_space": "screen_pixels", "x": 60, "y": 40}


@pytest.fixture
def shell(monkeypatch, tmp_path):
    # EnumWindows sees the underlying desktop app. GetForegroundWindow and
    # WindowFromPoint can independently identify an unlisted shell surface.
    menu, search, app, run = 72, 81, 19, 91
    system_root = str(tmp_path / "Windows")
    monkeypatch.setenv("SystemRoot", system_root)
    monkeypatch.setenv("WINDIR", system_root)
    sample = SimpleNamespace(menu=menu, search=search, app=app, run=run, enumerated=[app], foreground=menu,
                             hit=menu, menu_title="", process="StartMenuExperienceHost.exe",
                             menu_bounds=(20, 10, 160, 100), visible=True, menu_root=system_root)
    user, kernel, dwm = Mock(), Mock(), Mock()
    user.GetForegroundWindow.side_effect = lambda: sample.foreground
    user.EnumWindows.side_effect = lambda visit, arg: all(visit(hwnd, arg) for hwnd in sample.enumerated)
    user.IsWindow.side_effect = lambda hwnd: hwnd in (app, run, search, menu)
    user.IsWindowVisible.side_effect = lambda hwnd: hwnd in (app, run, search) or hwnd == menu and sample.visible
    user.IsWindowEnabled.return_value = True
    user.IsIconic.return_value = user.IsHungAppWindow.return_value = False
    user.GetAncestor.side_effect = lambda hwnd, flag: hwnd
    user.WindowFromPoint.side_effect = lambda point: sample.hit

    def find_window(parent, previous, window_class, title):
        if window_class == "Windows.UI.Core.CoreWindow":
            return {0: menu, None: menu, menu: search, search: 0}.get(previous, 0)
        return 0

    user.FindWindowExW.side_effect = find_window

    def window_text(hwnd, buf, size):
        buf.value = {app: "Document", search: "搜索", menu: sample.menu_title, run: "运行"}[hwnd]
        return len(buf.value)

    def window_class(hwnd, buf, size):
        buf.value = {app: "Notepad", run: "#32770"}.get(hwnd, "Windows.UI.Core.CoreWindow")
        return len(buf.value)

    def thread_process_id(hwnd, pid):
        pid._obj.value = hwnd + 100
        return hwnd + 200

    user.GetWindowTextW.side_effect = window_text
    user.GetClassNameW.side_effect = window_class
    user.GetWindowThreadProcessId.side_effect = thread_process_id
    kernel.OpenProcess.side_effect = lambda access, inherit, pid: pid

    def process_name(handle, flags, path, length):
        if handle == menu + 100:
            path.value = os.path.join(sample.menu_root, "SystemApps", "Microsoft.Windows.StartMenuExperienceHost_cw5n1h2txyewy", sample.process)
        elif handle == search + 100:
            path.value = os.path.join(system_root, "SystemApps", "MicrosoftWindows.Client.CBS_cw5n1h2txyewy", "SearchHost.exe")
        elif handle == run + 100:
            path.value = os.path.join(system_root, "explorer.exe")
        else:
            path.value = os.path.join(system_root, "System32", "notepad.exe")
        return True

    def window_rect(hwnd, rect):
        area = sample.menu_bounds if hwnd == menu else (0, 0, 180, 120)
        rect._obj.left, rect._obj.top, rect._obj.right, rect._obj.bottom = area
        return True

    kernel.QueryFullProcessImageNameW.side_effect = process_name
    user.GetWindowRect.side_effect = window_rect
    dwm.DwmGetWindowAttribute.return_value = 0
    libraries = {"user32": user, "kernel32": kernel, "dwmapi": dwm}
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: libraries[name], raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    monkeypatch.setattr(desktop_state, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows_env, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(desktop_capture, "physical_dpi_context", nullcontext)
    geometry = {"left": 0, "top": 0, "width": 180, "height": 120}
    monkeypatch.setattr(desktop_capture, "virtual_geometry", lambda: geometry)
    sample.input = Mock()
    monkeypatch.setitem(sys.modules, "pyautogui", sample.input)
    env = windows_env.WindowsEnvironment(task_mode="desktop", workspace_dir=str(tmp_path), enable_desktop_ax=False)
    monkeypatch.setattr(env, "_ensure_desktop_lease", lambda: None)
    monkeypatch.setattr(env, "_desktop_is_input", lambda: True)

    def capture():
        env._desktop_capture = {"desktop_capture_id": uuid4().hex, "screen_origin": [0, 0],
                                "virtual_desktop": geometry}
        return Image.new("RGB", (180, 120))

    monkeypatch.setattr(env, "_take_screenshot", capture)
    sample.env = env
    yield sample
    env.close()


@pytest.mark.parametrize("enumerated", [False, True], ids=["missing-from-enumeration", "untitled-in-enumeration"])
def test_start_menu_accepts_visual_click_and_escape(shell, enumerated):
    if enumerated:
        shell.enumerated.insert(0, shell.menu)
    observation = shell.env.observe()
    info = observation.info
    assert info["desktop_focus_stable"]
    assert foreground(info["runtime_state"])["hwnd"] == shell.menu
    assert len([w for w in info["runtime_state"]["applications"] if w["hwnd"] == shell.menu]) == 1
    assert shell.env.step(Action("click", PINNED_APP)).info["dispatched"]
    assert shell.env.step(Action("press", {"key": "esc"})).info["dispatched"]
    shell.input.click.assert_called_once_with(60, 40)
    shell.input.press.assert_called_once_with("esc")


def test_search_focus_and_start_menu_pointer_belong_to_distinct_shell_windows(shell):
    shell.menu_title = "开始"
    shell.foreground = shell.search
    observation = shell.env.observe()
    assert foreground(observation.info["runtime_state"])["hwnd"] == shell.search
    assert shell.hit == shell.menu
    assert shell.env.step(Action("click", PINNED_APP)).info["dispatched"]
    assert shell.env.step(Action("press", {"key": "esc"})).info["dispatched"]
    shell.input.click.assert_called_once_with(60, 40)
    shell.input.press.assert_called_once_with("esc")


@pytest.mark.parametrize("condition", ["other-window", "no-owner", "menu-moved", "menu-disappeared", "untrusted-menu"])
def test_start_menu_pointer_requires_same_observed_shell_target(shell, condition, tmp_path):
    shell.menu_title = "开始"
    shell.foreground = shell.search
    shell.env.observe()
    if condition == "other-window":
        shell.hit = shell.app
    elif condition == "no-owner":
        shell.hit = 0
    elif condition == "menu-moved":
        shell.menu_bounds = (35, 20, 175, 110)
    elif condition == "menu-disappeared":
        shell.visible = False
    else:
        shell.menu_root = str(tmp_path / "Untrusted")
    result = shell.env.step(Action("click", PINNED_APP))
    assert result.info["status"] == "rejected" and not result.info["dispatched"]
    shell.input.click.assert_not_called()


def test_start_menu_keyboard_requires_same_live_foreground(shell):
    shell.env.observe()
    shell.foreground = shell.app
    result = shell.env.step(Action("press", {"key": "esc"}))
    assert result.info["status"] == "rejected" and not result.info["dispatched"]
    shell.input.press.assert_not_called()


@pytest.mark.parametrize(("host", "key"), [("menu", "win"), ("search", "winleft"), ("search", "winright")])
def test_win_r_from_start_reports_dispatch_without_visible_run_dialog(shell, host, key):
    shell.foreground = getattr(shell, host)
    shell.env.observe()
    result = shell.env.step(Action("hotkey", {"keys": [key, "r"]}))
    shell.input.hotkey.assert_called_once_with(key, "r")
    assert result.info["dispatched"] and result.info["status"] == "uncertain"
    assert result.info["error_code"] == "run_dialog_not_confirmed"
    assert "菜单" in result.info["reason"] and "Esc" in result.info["reason"]


def test_win_r_can_open_the_run_dialog_from_search(shell):
    shell.foreground = shell.search
    shell.env.observe()

    def open_dialog(*keys):
        shell.enumerated.insert(0, shell.run)
        shell.foreground = shell.run

    shell.input.hotkey.side_effect = open_dialog
    result = shell.env.step(Action("hotkey", {"keys": ["win", "r"]}))
    shell.input.hotkey.assert_called_once_with("win", "r")
    assert result.info["dispatched"] and result.info["status"] == "executed"
    assert foreground(result.observation.info["runtime_state"])["title"] == "运行"


def test_unrelated_shortcut_from_start_keeps_its_own_receipt(shell):
    shell.env.observe()
    result = shell.env.step(Action("hotkey", {"keys": ["win", "d"]}))
    shell.input.hotkey.assert_called_once_with("win", "d")
    assert result.info["dispatched"] and result.info["status"] == "executed"


def test_shell_target_appearing_during_capture_requires_new_observation(shell, monkeypatch):
    shell.foreground = shell.search
    shell.visible = False
    capture = shell.env._take_screenshot

    def show_menu_during_capture():
        image = capture()
        shell.visible = True
        return image

    monkeypatch.setattr(shell.env, "_take_screenshot", show_menu_during_capture)
    shell.env.observe()
    assert not shell.env.step(Action("click", PINNED_APP)).info["dispatched"]
    shell.input.click.assert_not_called()
    shell.env.observe()
    assert shell.env.step(Action("click", PINNED_APP)).info["dispatched"]
    shell.input.click.assert_called_once_with(60, 40)


@pytest.mark.parametrize("condition", ["hidden", "different-process", "untrusted-path", "no-foreground"])
def test_no_input_on_unconfirmed_start_surface(shell, condition, tmp_path):
    if condition == "hidden":
        shell.visible = False
    elif condition == "different-process":
        shell.process = "unrelated.exe"
    elif condition == "untrusted-path":
        shell.menu_root = str(tmp_path / "Untrusted")
    else:
        shell.foreground = 0
    shell.env.observe()
    result = shell.env.step(Action("press", {"key": "esc"}))
    assert result.info["status"] == "rejected" and not result.info["dispatched"]
    shell.input.press.assert_not_called()
