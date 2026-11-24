"""Routing invariants shared by the desktop policy, native executor and Studio trace."""
from types import SimpleNamespace

import pytest
from PIL import Image

from hybrid_agent.actions import Action
from hybrid_agent.decision_context import action_scope, system_sections
from hybrid_agent.env import Observation
from hybrid_agent.pipeline import Pipeline
from hybrid_agent.planning.context import PromptCompiler
from hybrid_agent.planning.decision_modes import decision_mode, desktop_cli_step, mode_capability
from hybrid_agent.runtime.pipeline_format import _operation_mode


def observation():
    return Observation(screenshot=Image.new('RGB', (180, 100)), info={
        'task_mode':'desktop', 'active_surface':'desktop', 'screenshot_space':'screen_pixels'},
        context=[{'kind':'desktop_ax', 'window_ref':'w1', 'snapshot_id':'s2',
                  'nodes':[{'ref':'desk:s2:r1', 'direct':True, 'role':'button', 'name':'保存', 'actions':['click']}]}])


def test_modes_filter_browser_skills_and_render_scoped_references():
    obs = observation()
    assert mode_capability(obs) is None
    profile = decision_mode(obs, judgment={'label':'desktop_ax'})
    menu = action_scope({}, obs, profile=profile)
    assert {'click', 'type', 'select'} <= set(menu)
    assert not set(menu) & {'goto', 'observe_browser', 'run_browser_js', 'list_tabs'}
    assert not any(k == 'skills' for k, _ in system_sections(menu, obs, profile=profile))
    text, refs, _ = PromptCompiler().structure(obs.context, '点击保存', 512)
    assert '保存' in text and refs == ['desk:s2:r1']
    assert _operation_mode(Action('click'), {'channel':'desktop_ax'}) == 'desktop_ax'
    assert decision_mode(obs, judgment={'label':'unknown'}).mode == 'gui'
    assert mode_capability(obs, {'goal':'拖拽图形'}).mode == 'gui'
    obs.info['desktop_file_dialog'] = True
    assert mode_capability(obs).reason == 'native_file_dialog_keyboard_input'
    obs.info.pop('desktop_file_dialog')
    obs.context.clear()
    assert mode_capability(obs).mode == 'gui'
    obs.screenshot = None
    assert mode_capability(obs).mode == 'observe'


def test_small_ax_budget_keeps_matching_edit_value_visible():
    obs = observation()
    nodes = obs.context[0]['nodes']
    nodes.extend(dict(nodes[0], ref=f'r{i}', name=f'Command {i}') for i in range(20))
    nodes.append({'role':'text', 'name':'编辑栏', 'value':'TERRA-AX-42'})
    text, refs, _ = PromptCompiler().structure(obs.context, '在 A1 输入 TERRA-AX-42', 320)
    assert 'TERRA-AX-42' in text
    assert len(refs) < 20


def test_desktop_cli_requires_a_planned_and_allowed_operation():
    step = {'goal':'写入工作簿', 'actions':['cli']}
    assert not desktop_cli_step('用 Excel 写入工作簿', step)
    assert not desktop_cli_step('使用 Blender 渲染这个模型', step)
    assert not desktop_cli_step('不要使用终端保存文件', step)
    assert desktop_cli_step('创建一个文本文件', step)
    assert not desktop_cli_step('创建一个文本文件', {'goal':'点击新建'})
    assert desktop_cli_step('用 Excel 编辑，之后用终端复制成品', {'goal':'用终端复制成品','actions':['cli']})
    obs = observation()
    obs.info['cli_capability'] = {'available':True}
    choice = {'goal':'复制已生成的文件', 'actions':['click','cli']}
    assert mode_capability(obs, choice) is None
    assert decision_mode(obs, choice, judgment={'label':'cli'}).mode == 'cli'
    obs.context.clear()
    assert decision_mode(obs, choice, judgment={'label':'desktop_ax'}).mode == 'gui'


def test_desktop_executor_checks_scope_before_skills_or_cli_dispatch():
    pipe = object.__new__(Pipeline)
    pipe.env = SimpleNamespace(task_mode='desktop')
    pipe._active_actions = ['click', 'wait']
    state = SimpleNamespace(obs=observation(), task='用记事本保存', queue=None)
    for name in ('ilearning_answer_exam', 'run_browser_js', 'cli'):
        response = pipe.execute(Action(name), state)
        assert response.info['dispatched'] is False
        assert response.info['error_code'] == 'desktop_scope'
    for target in ('desktop:session:frame:r4', {'target_ref':'r4'}):
        assert 'target_ref' in pipe._validate_action(Action('type', {'target':target, 'text':'test'}))


def test_keyboard_actions_bind_the_capture_and_drag_requires_both_points():
    obs = observation()
    obs.info['desktop_capture_id'] = 'frame-1'
    key = Action('hotkey', {'keys':['ctrl','s']})
    Pipeline._attach_observation_space(key, obs)
    assert key.args['observation_metadata']['desktop_capture_id'] == 'frame-1'
    pipe = object.__new__(Pipeline)
    from hybrid_agent.model.grounding import GroundingResult
    responses = iter([GroundingResult(20, 30, 1.0), GroundingResult(-1, -1, 0)])
    seen = []
    def locate(image, target):
        seen.append((image, target))
        return next(responses)
    pipe.grounding = SimpleNamespace(locate=locate, allow_ocr_fallback=False)
    result = pipe._resolve(Action('drag_and_drop', {'from_target':'方块','to_target':'终点'}), obs)
    assert result.args['grounding_failed'] and 'x1' not in result.args
    assert seen == [(obs.screenshot,'方块'), (obs.screenshot,'终点')]


def test_hidden_or_visual_only_refs_cannot_reach_native_execution():
    from hybrid_agent.runtime.pipeline_types import StepState
    pipe = object.__new__(Pipeline)
    pipe.env = SimpleNamespace(task_mode='desktop', route_action=lambda a,o: {'channel':'desktop_ax'})
    obs = observation()
    action = Action('click', {'target':'保存','target_ref':'desk:s2:r1'})
    state = StepState(task='保存文档', obs=obs)
    for mode, exposed in [('desktop_ax', []), ('gui', ['desk:s2:r1'])]:
        obs.info.update(decision_mode={'mode':mode}, exposed_refs=exposed)
        pipe.ground(action, state)
        assert state.route['channel'] == 'reobserve' and state.route['source'] == 'desktop_ax'
    obs.info.update(decision_mode={'mode':'desktop_ax'}, exposed_refs=['desk:s2:r1'])
    pipe.ground(action, state)
    assert state.route['channel'] == 'desktop_ax'


@pytest.mark.parametrize("origin,size", [((0, 0), (1366, 768)), ((-1920, -120), (4480, 1560))])
def test_only_the_executor_translates_image_pixels_to_virtual_desktop(origin, size):
    from hybrid_agent.desktop.desktop_capture import validate_desktop_action
    geometry = dict(zip(('left','top','width','height'), (*origin, *size)))
    captured = {'desktop_capture_id':'frame', 'virtual_desktop':geometry}
    point = Action('click', {'x':size[0]-1, 'y':0, 'observation_metadata':{'desktop_capture_id':'frame'}})
    valid, offset = validate_desktop_action(point, captured, geometry)
    assert (valid.args['x'] + offset[0], valid.args['y'] + offset[1]) == (origin[0]+size[0]-1, origin[1])
    with pytest.raises(ValueError):
        validate_desktop_action(point, captured, {**geometry, 'left':origin[0]+1})
    for bad in [Action('click', {'x':size[0], 'y':1}),
                Action('drag_and_drop', {'x1':1,'y1':1,'x2':3,'y2':size[1]}),
                Action('click', {'x':1,'y':1,'observation_metadata':{'desktop_capture_id':'old'}})]:
        with pytest.raises(ValueError):
            validate_desktop_action(bad, captured, geometry)
