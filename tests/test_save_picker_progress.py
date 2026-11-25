from types import SimpleNamespace

import pytest

from hybrid_agent.actions import Action
from hybrid_agent.runtime.step_progress import ProgressConfig, StepProgressMonitor


PATH = r'D:\out\test.docx'
STEP = {'id':'save', 'goal':'使用 Word 保存为 ' + PATH,
        'verification':[{'kind':'file_exists', 'path':PATH, 'file_type':'file'}]}
TASK = '通过 Word 在 ' + PATH + ' 新建空白文档'
F12 = Action('press', {'key':'F12'})
OBSERVE = Action('observe_desktop', {})
REVIEW = {'plan_step_id':'save', 'status':'failed'}


def frame(picker=False, *, dialog=None, owner=None, extras=None):
    main = dict(hwnd=721, pid=193, process_name='WINWORD.EXE', class_name='OpusApp', title='Document1 - Word',
                foreground=not picker, bounds=[0,0,900,650], minimized=False, cloaked=False, enabled=not picker,
                visible=True, responsive=True)
    main.update(owner or {})
    popup = dict(main, hwnd=800, class_name='#32770', owner_hwnd=721, title='另存为', foreground=True, enabled=True)
    popup.update(dialog or {})
    info = dict(desktop_capture_source='win32_task_monitor', desktop_capture_id='frame', desktop_focus_stable=True,
                desktop_file_dialog=picker, runtime_state={'source':'windows_window_api',
                           'applications':[main, popup] if picker else [main]})
    return SimpleNamespace(info={**info, **(extras or {})}, context=[])


def update(monitor, before, after, action=OBSERVE, step=STEP, dispatched=None):
    sent = action.action != 'observe_desktop' if dispatched is None else dispatched
    return monitor.observe(step, REVIEW, before, after, action, {'status':'executed', 'dispatched':sent}, task=TASK)


@pytest.mark.parametrize('title', ['另存为', 'Save As'])
def test_save_picker_delayed_frame_recovers_idle_and_never_completes_file(title):
    m = StepProgressMonitor()
    active, blocked = frame(), frame(owner={'enabled': False})
    picker = frame(True,dialog={'title':title})
    assert update(m,active,blocked,F12)['idle_decisions'] == 1
    progress = update(m,blocked,picker)
    assert progress['idle_decisions'] == 0 and progress['status'] == 'watching'
    assert len(progress['evidence']) == 1 and 'overwrite=true' in progress['navigation_feedback']
    assert '点击保存' in progress['navigation_feedback']
    for idle in range(1,5):
        unchanged = update(m,picker,picker,Action('type',{'text':PATH}))
        assert unchanged['idle_decisions'] == idle and unchanged['evidence'] == []
    assert unchanged['status'] == 'stalled' and m.explorations == 1


@pytest.mark.parametrize('kwargs', [
    {'dialog':{'title':'打开'}}, {'dialog':{'title':'Open'}}, {'dialog':{'class_name':'Dialog'}},
    {'dialog':{'owner_hwnd':999}}, {'dialog':{'pid':194}}, {'owner':{'process_name':'other.exe'}},
    {'owner':{'enabled':True}},
    {'dialog':{'enabled':False}}, {'extras':{'desktop_window_moved':True}},
    {'extras':{'desktop_file_dialog':False}}, {'extras':{'desktop_focus_stable':False}},
    {'extras':{'desktop_foreground_outside_task_monitor':True}},
])
def test_untrusted_window_or_wrong_picker_never_earns_credit(kwargs):
    assert not update(StepProgressMonitor(), frame(), frame(True, **kwargs), F12)['evidence']


def test_save_picker_requires_target_dispatched_intent_and_budget():
    active, picker = frame(), frame(True)
    assert not update(StepProgressMonitor(),active,picker)['evidence']
    assert not update(StepProgressMonitor(),active,picker,F12,dispatched=False)['evidence']
    assert not update(StepProgressMonitor(),active,picker,F12,step=dict(STEP,goal='核对 Word 窗口标题'))['evidence']
    assert not update(StepProgressMonitor(),active,picker,F12,
                      step=dict(STEP,verification=[{'kind':'file_exists','path':r'C:\wrong\stranger.docx'}]))['evidence']
    assert not update(StepProgressMonitor(ProgressConfig(exploration_allowance=0)),active,picker,F12)['evidence']
    m = StepProgressMonitor()
    assert update(m,active,picker,F12)['evidence']
    update(m,picker,active,Action('press',{'key':'esc'}))
    assert not update(m,active,frame(True,dialog={'hwnd':801}),F12)['evidence']
    assert m.explorations == 1
