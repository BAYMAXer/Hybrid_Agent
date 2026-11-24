import sys
from uuid import uuid4
from unittest.mock import Mock

import pytest

from hybrid_agent.adapters.windows_env import WindowsEnvironment
from hybrid_agent.desktop.desktop_state import stable_point_layout


def test_desktop_single_click_on_spreadsheet_emits_one_input():
    from hybrid_agent.actions import Action
    from hybrid_agent.adapters.vmware_env import to_pyautogui
    action = Action("click", {"target":"Sheet1 的 A1 单元格","x":5,"y":8})
    assert to_pyautogui(action, desktop_origin=(-2, 0), literal_click=True).count("pyautogui.click(3, 8)") == 1
    assert to_pyautogui(action).count("pyautogui.click(5, 8)") == 2


def test_point_uses_live_geometry_and_stacking_of_the_window_at_that_point():
    app = (1, (0, 0, 300, 300))
    other_app = (2, (310, 0, 610, 300))
    taskbar = (3, (0, 300, 610, 330))
    desktop = (4, (0, 0, 700, 400))
    before = [app, other_app, taskbar, desktop]
    moved = [(1, (10, 0, 310, 300)), other_app, taskbar, desktop]
    assert not stable_point_layout((100, 50), [before, before, moved])
    assert not stable_point_layout((100, 50), [before, [(8, (0, 0, 140, 80)), *before]])
    assert stable_point_layout((400, 100), [before, moved])
    assert stable_point_layout((20, 315), [before, moved])
    assert stable_point_layout((650, 350), [before, moved])
    overlay = (6, (0, 0, 245, 220))
    assert not stable_point_layout((100, 50), [[overlay, *before], [overlay, *moved]], owner=app[0])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named kernel object")
def test_exclusive_input_on_one_desktop_is_released_on_close(tmp_path, monkeypatch):
    # Exercise a real named mutex without competing with a live desktop task.
    lease_key = 'test-' + uuid4().hex
    monkeypatch.setattr('hybrid_agent.desktop.desktop_capture.windows_desktop_key', lambda: lease_key)
    first, second = [WindowsEnvironment(task_mode="desktop", workspace_dir=tmp_path, enable_desktop_ax=False)
                     for _ in range(2)]
    try:
        first.begin_episode("one")
        first.begin_episode("two")
        with pytest.raises(RuntimeError, match="已有活动任务"):
            second.begin_episode("another")
        first.close()
        second.begin_episode("another")
    finally:
        first.close()
        second.close()


def test_switched_window_readiness_uses_the_environment_observation(tmp_path, monkeypatch):
    env = WindowsEnvironment(task_mode='desktop', workspace_dir=tmp_path, enable_desktop_ax=False)
    target = {'hwnd': 42, 'pid': 128, 'process_name': 'Notepad.exe'}
    current = dict(target, foreground=True, visible=True, minimized=False, cloaked=False,
                   enabled=True, responsive=True, bounds=[30, 40, 850, 700])
    sample = Mock(return_value={'applications': [current]})
    monkeypatch.setattr(env, '_runtime_state', sample)
    env.window_timeout_s = 2
    result = env._wait_for_desktop_window(target, dispatched=True)
    assert result['framework_wait']['status'] == 'completed' and sample.call_count == 2
