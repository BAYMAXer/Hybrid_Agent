from copy import deepcopy
from unittest.mock import Mock

import pytest
from PIL import Image

from hybrid_agent.actions import Action
from hybrid_agent.adapters.windows_env import WindowsEnvironment
from hybrid_agent.desktop import desktop_capture as capture, desktop_state, monitor_windows
from hybrid_agent.desktop.monitor_windows import TaskMonitor, MonitorStop, window_fits


def display(device, x, width, *, primary=False, dpi=96):
    return dict(id=device, name=device, primary=primary, left=x, top=0, width=width, height=1080,
                work_area=[x, 0, x+width, 1040], dpi=[dpi, dpi])


def win(hwnd, title, rect, *, pid=100, process='WINWORD.EXE', owner=None, active=True):
    return dict(hwnd=hwnd, pid=pid, process_name=process, title=title, bounds=rect, foreground=active,
                owner_hwnd=owner, minimized=False, visible=True, enabled=True, responsive=True, cloaked=False)


@pytest.fixture
def screens(monkeypatch):
    pair = [display('P', 0, 1920, primary=True), display('S', -1440, 1440, dpi=144)]
    monkeypatch.setattr(capture, 'list_desktop_monitors', lambda: deepcopy(pair))
    monkeypatch.setattr(capture, 'virtual_geometry', lambda: dict(left=-1440, top=0, width=3360, height=1080))
    from contextlib import nullcontext
    monkeypatch.setattr(capture, 'physical_dpi_context', nullcontext)
    return pair


def test_selected_screen_pixels_origin_bounds_and_stale_metadata(screens, monkeypatch):
    grab = Mock(return_value=Image.new('RGB', (1440, 1080)))
    monkeypatch.setattr('PIL.ImageGrab.grab', grab)
    assert capture.resolve_desktop_monitor()['id'] == 'P'
    image, metadata = capture.capture_monitor('s')
    grab.assert_called_once_with(bbox=(-1440, 0, 0, 1080), all_screens=True)
    assert image.size == (1440, 1080) and metadata['desktop_monitor']['dpi'] == [144, 144]
    action = Action('click', {'x': 14, 'y': 20, 'observation_metadata': metadata})
    assert capture.validate_desktop_action(action, metadata, capture.virtual_geometry())[1] == (-1440, 0)
    with pytest.raises(ValueError, match='超出'):
        capture.validate_desktop_action(Action('drag_and_drop', dict(x1=1, y1=2, x2=1441, y2=2)),
                                        metadata, capture.virtual_geometry())
    newer = dict(metadata, desktop_capture_id='different')
    with pytest.raises(ValueError, match='过期'):
        capture.validate_desktop_action(action, newer, capture.virtual_geometry())
    screens[1]['dpi'] = [120, 120]
    with pytest.raises(ValueError, match='DPI'):
        capture.validate_desktop_action(action, metadata, capture.virtual_geometry())
    screens.pop()
    with pytest.raises(ValueError, match='不可用'):
        capture.resolve_desktop_monitor('S')


def test_target_doc_reused_process_and_late_owned_dialog_only(screens, monkeypatch):
    first = win(1, '预算.docx - Word', [-1000, 0, -100, 700], active=True)
    target = win(2, '报告 - Word', [-600, 20, 160, 560], active=False)
    state = {'applications': [first, target]}
    moved = []
    def move(window, monitor):
        moved.append(window['hwnd'])
        window['bounds'] = [10, 20, 700, 540]
    monkeypatch.setattr(monitor_windows, '_move_to_work_area', move)
    monkeypatch.setattr(desktop_state, 'windows_runtime_state', lambda **_: state)
    monkeypatch.setattr(desktop_state, '_activate_window', lambda hwnd: (True, True))
    scope = TaskMonitor('primary')
    result = desktop_state.switch_windows_application('word', task='请处理报告.docx并保存', bind=scope.bind)
    assert result['window']['hwnd'] == 2 and moved == [2]
    first['foreground'] = False
    dialog = win(3, '另存为', [-1200, 30, -700, 600], owner=2)
    state['applications'].append(dialog)
    scope.prepare_observation(state, '')
    assert moved == [2, 3]
    unrelated = win(4, '用户文档 - Word', [-600, 15, -50, 250], owner=1)
    dialog['foreground'] = False
    state['applications'].append(unrelated)
    with pytest.raises(MonitorStop, match='前台'):
        scope.prepare_observation(state, '')
    assert moved == [2, 3] and not window_fits(first, screens[0])
    assert desktop_state.switch_windows_application('word', bind=scope.bind)['error_code'] == 'ambiguous_application'


def test_explorer_launch_excludes_previous_or_ambiguous_foreground(screens):
    scope = TaskMonitor('primary')
    old = win(10, 'Other', [-650, 20, -1, 500], process='explorer.exe')
    old['class_name'] = 'CabinetWClass'
    scope.launch_baseline = {(10, 100, 'explorer.exe')}
    target = {'process_name':'explorer.exe'}
    assert not scope.launch_state({'applications':[old]}, target)['applications']
    fresh = dict(old, hwnd=11, bounds=[0, 0, 900, 500])
    assert scope.launch_state({'applications':[old, fresh]}, target)['applications'] == [fresh]
    with pytest.raises(MonitorStop, match='多个'):
        scope.launch_state({'applications':[old, fresh, dict(fresh, hwnd=12)]}, target)
    scope.bind(fresh)
    assert scope.root[0] == 11
    screens.clear()
    with pytest.raises(MonitorStop, match='不可用'):
        scope.refresh()


def test_changed_dpi_invalidates_native_dispatch(screens, monkeypatch):
    window = win(50, 'active', [20, 20, 400, 500])
    state = {'applications':[window]}
    env = WindowsEnvironment(task_mode='desktop')
    env._task_monitor = TaskMonitor('primary')
    env._desktop_capture = {'desktop_monitor': deepcopy(screens[0])}
    env._last_observation_info = {'runtime_state': state}
    monkeypatch.setattr(env, '_ensure_desktop_lease', lambda: None)
    monkeypatch.setattr(env, '_desktop_is_input', lambda: True)
    monkeypatch.setattr(env, '_runtime_state', lambda: state)
    monkeypatch.setattr(env, 'observe', Mock())
    checks = []
    class Provider:
        def execute(self, action, route, current_window):
            checks.append(current_window())
            return {'status':'rejected', 'dispatched':False}
    env._desktop_ax = Provider()
    screens[0]['dpi'] = [120, 120]
    env.execute_routed(Action('click', {}), {'channel':'desktop_ax'})
    assert checks == [None]


def test_unrelated_focus_ends_run_before_screenshot_on_next_observation(screens, monkeypatch):
    root = win(50, '任务', [20, 20, 400, 500])
    unrelated = win(51, '其他', [-600, 20, -100, 500])
    env = WindowsEnvironment(task_mode='desktop')
    env._task_monitor = TaskMonitor('primary')
    env._task_monitor.bind(root)
    root['foreground'] = False
    monkeypatch.setattr(env, '_desktop_is_input', lambda: True)
    monkeypatch.setattr(env, '_runtime_state', lambda: {'applications':[root, unrelated]})
    monkeypatch.setattr(env, '_take_screenshot', lambda: pytest.fail('captured unrelated foreground'))
    obs = env.observe()
    assert env.desktop_stop['error_code'] == 'desktop_window_focus_changed'
    assert obs.info['desktop_stop']['continuation']['state'] == 'stop' and obs.screenshot is None
    monkeypatch.setattr(env, '_ensure_desktop_lease', lambda: None)
    result = env.step(Action('press', {'key': 'enter'}))
    assert result.done and not result.info['dispatched']


def test_stale_global_shortcut_rejected_and_no_cross_monitor_blind_switch(screens, monkeypatch):
    env = WindowsEnvironment(task_mode='desktop')
    env._task_monitor = TaskMonitor('primary')
    window = win(50, 'active', [20, 20, 400, 500])
    state = {'applications':[window]}
    env._last_observation_info = {'runtime_state':deepcopy(state)}
    env._desktop_capture = {'desktop_monitor':deepcopy(screens[0])}
    monkeypatch.setattr(env, '_runtime_state', lambda: state)
    assert '盲切' in env._prepare_monitor_input(Action('hotkey', {'keys':['alt','tab']}), (0,0))
    screens[0]['dpi'] = [144,144]
    assert 'DPI' in env._prepare_monitor_input(Action('hotkey', {'keys':['win','e']}), (0,0))
