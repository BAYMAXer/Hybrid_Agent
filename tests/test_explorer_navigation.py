"""Replay the Windows file-task navigation failure using observable Shell facts."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from hybrid_agent.actions import Action
from hybrid_agent.desktop.explorer_location import _visible_view, committed_explorer_path, filesystem_path
from hybrid_agent.env import Observation
from hybrid_agent.runtime.execution_queue import normalize_verification
from hybrid_agent.runtime.step_progress import StepProgressMonitor
from hybrid_agent.verification.judge_evidence import select_frame
from hybrid_agent.verification.step_verifier import code_review, unresolved_desktop_checks


def explorer(path=None, *, hwnd=12, title='Download - 文件资源管理器', **extra):
    window = dict(hwnd=hwnd, pid=44, process_name='explorer.exe', class_name='CabinetWClass',
                  visible=True, foreground=True, enabled=True, responsive=True, minimized=False,
                  cloaked=False, bounds=[0, 0, 1024, 768], title=title)
    location = {'status': 'unknown', 'hwnd': hwnd}
    if path:
        location.update(status='observed', path=path)
    return Observation(info={'task_mode': 'desktop', 'active_surface': 'desktop', 'desktop_focus_stable': True,
                            'desktop_capture_id': 'fixture',
                            'runtime_state': {'applications': [window], 'source': 'windows_window_api'},
                            'explorer_location': location, **extra})


TARGET = r'D:\Download'
TASK = r'在D:\Download创建一个test的文档'
STEP = {'id': 'create', 'goal': '创建 Word 文档',
        'verification': [{'kind': 'file_exists', 'file_type': 'file', 'path': r'D:\Download\test.docx'}]}
MISSING = {'status': 'failed', 'checks': [{'kind': 'file_exists', 'status': 'failed'}]}
EXECUTED = {'status': 'executed', 'dispatched': True}


def observe(monitor, before, after, action, task=TASK, review=MISSING):
    return monitor.observe(STEP, review, before, after, action, EXECUTED, task=task)


def test_incident_open_two_single_clicks_then_enter_directory():
    home = explorer(title='主文件夹 - 文件资源管理器')
    destination = explorer(TARGET)
    monitor = StepProgressMonitor()
    miss = Observation()
    launch = observe(monitor, miss, home, Action('hotkey', {'keys': ['win', 'e']}))
    first = observe(monitor, home, home, Action('click', {'target': 'Data (D:)'}))
    second = observe(monitor, home, home, Action('click', {'target': 'Download'}))
    arrived = observe(monitor, home, destination, Action('double_click', {'target': 'Download'}))
    assert [s['idle_decisions'] for s in (launch, first, second, arrived)] == [0, 1, 2, 0]
    assert arrived['status'] == 'watching' and '任务指定目录' in arrived['evidence'][0]
    assert '单击后尚未核实' in second['navigation_feedback'] and MISSING['status'] == 'failed'
    assert monitor.explorations == 1


def test_visiting_other_download_folder_repeatedly_or_moving_window_does_not_count():
    monitor = StepProgressMonitor()
    wrong = explorer(r'C:\Download')
    right = explorer(TARGET)
    click = Action('double_click', {'target': 'Download'})
    moved = explorer(TARGET, desktop_window_moved=True)
    assert observe(monitor, Observation(), wrong, click)['idle_decisions'] == 1
    assert observe(monitor, wrong, moved, click)['idle_decisions'] == 2
    assert observe(monitor, wrong, right, click)['idle_decisions'] == 0
    assert observe(monitor, right, wrong, click)['idle_decisions'] == 1
    assert observe(monitor, wrong, right, click)['idle_decisions'] == 2
    observe(monitor, right, right, Action('click', {'target': 'test'}))
    stalled = observe(monitor, right, right, Action('click', {'target': 'test'}))
    assert stalled['status'] == 'stalled' and stalled['idle_decisions'] == 4
    assert 'navigation_feedback' not in stalled
    stuck = StepProgressMonitor()
    for _ in range(4):
        stalled = observe(stuck, wrong, wrong, Action('click', {'target': 'Download'}))
    assert stalled['status'] == 'stalled' and '目录未切换' in stalled['navigation_feedback']


def test_only_explicit_destination_or_public_deliverable_directory_can_confirm_progress():
    old = explorer(r'C:\Download')
    new = explorer(TARGET)
    enter = Action('press', {'key': 'enter'})
    for different in (r'进入D:\Download42', r'进入D:\Download\Pictures'):
        assert not observe(StepProgressMonitor(), old, new, enter, task=different)['evidence']
    public = [{'kind': 'desktop_file', 'path': r'd:\download\test.docx'}]
    result = StepProgressMonitor().observe(STEP, MISSING, old, new, enter, EXECUTED,
                                           task='创建目标文件', public_checks=public)
    assert result['evidence']


def test_directory_predicate_uses_current_observed_path_and_never_title_or_address_entry():
    pred = {'kind': 'explorer_folder_equals', 'path': r'd:\Download'}
    step = {'verification': normalize_verification([pred])}
    with pytest.raises(ValueError):
        normalize_verification([{**pred, 'path': 'Download'}])
    for obs, expected in ((explorer(), 'unknown'), (explorer(TARGET, desktop_focus_stable=False), 'unknown'),
                          (explorer(r'X:\Download'), 'failed'), (explorer(TARGET), 'passed')):
        # The edit control may already contain the target text before Enter.
        obs.context = [{'kind': 'desktop_ax', 'nodes': [{'name': '地址', 'value': TARGET}]}]
        result = code_review(step, obs, EXECUTED, {})
        assert result['status'] == expected
        assert unresolved_desktop_checks(result) == (expected != 'passed')
    wrong_window = explorer(TARGET)
    wrong_window.info['explorer_location']['hwnd'] = 42
    assert not committed_explorer_path(wrong_window)
    assert select_frame(wrong_window)['explorer_location'] == {'status': 'unknown'}
    assert select_frame(explorer(TARGET))['explorer_location']['path'] == TARGET


def test_visible_active_shell_view_is_bound_to_hwnd_and_ambiguous_tabs_yield_unknown():
    other_tab = SimpleNamespace(HWND=11, view=9)
    tab1 = SimpleNamespace(HWND=12, view=1)
    tab2 = SimpleNamespace(HWND=12, view=2)
    tabs = [other_tab, tab1, tab2]
    def get_view(item):
        return SimpleNamespace(GetWindow=lambda: item.view)
    assert _visible_view(tabs, 12, get_view, {2, 9}.__contains__)[0] == 2
    assert _visible_view(tabs, 12, get_view, {1, 2}.__contains__) == (None, None)
    assert _visible_view(tabs, 12, get_view, {9}.__contains__) == (None, None)
    assert not filesystem_path('shell:Downloads')
    assert not filesystem_path('C:Download')
    assert filesystem_path(r'\\server\share\Folder') == r'\\server\share\Folder'


def test_desktop_observation_without_ax_only_reads_directory_for_stable_window(monkeypatch, tmp_path):
    from PIL import Image
    from hybrid_agent.adapters.windows_env import WindowsEnvironment
    env = WindowsEnvironment(task_mode='desktop', enable_desktop_ax=False, workspace_dir=str(tmp_path))
    monkeypatch.setattr(env, '_desktop_is_input', lambda: True)
    def capture():
        env._desktop_capture = {'desktop_capture_id': 'fixture'}
        return Image.new('RGB', (40, 20))
    monkeypatch.setattr(env, '_take_screenshot', capture)
    frames = [explorer(TARGET), explorer(TARGET), explorer(TARGET), explorer(TARGET, hwnd=99)]
    states = iter([deepcopy(obs.info['runtime_state']) for obs in frames])
    monkeypatch.setattr(env, '_runtime_state', lambda: next(states))
    reads = []
    def shell_location(window):
        reads.append(window['hwnd'])
        return {'status': 'observed', 'path': TARGET, 'hwnd': window['hwnd']}
    monkeypatch.setattr('hybrid_agent.desktop.explorer_location.observe_explorer_location', shell_location)
    assert committed_explorer_path(env.observe()) == TARGET
    assert not committed_explorer_path(env.observe())
    assert reads == [12]
