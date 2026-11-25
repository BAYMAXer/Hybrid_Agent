import json
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from hybrid_agent.env import Observation
from hybrid_agent.pipeline import Pipeline
from hybrid_agent.planning.decision_modes import desktop_cli_step
from hybrid_agent.planning.plan_compiler import compile_plan
from hybrid_agent.planning.planning_service import PlanningService
from hybrid_agent.planning.replanning import validate_repair
from hybrid_agent.verification.verifiers import CompletionVerifier


def step(goal, **extra):
    return {'goal': goal, 'expected_evidence': '能够独立读到目标文件',
            'verification': [{'kind': 'file_exists', 'file_type': 'file', 'path': r'D:\Download\test.docx'}],
            **extra}


def compile_steps(steps, repair=False):
    return compile_plan({'steps': steps}, '创建 test.docx', {'checks': []}, {'kind': 'desktop'}, [], repair=repair)


def test_word_contract_references_bind_the_original_check_without_overrides():
    contract = {'checks': [{'id': 'word-file', 'kind': 'desktop_file', 'path': 'test.docx',
                            'format': 'docx', 'change': 'created'}]}

    def compile_checks(checks):
        return compile_plan({'steps': [step('保存', verification=checks)]}, '创建 test.docx',
                            contract, {'kind': 'desktop'}, [], repair=True)

    reference = {'id': 'word-file', 'kind': 'desktop_file'}
    only = compile_checks([reference, {'id': 'word-file'}])
    assert only['steps'][0]['check_ids'] == ['word-file']
    assert only['steps'][0]['verification'] == []
    assert only['completion_spec'] == contract
    visual = {'kind': 'visual'}
    assert compile_checks([visual, reference])['steps'][0]['verification'] == [visual]
    for invalid in ({'id': 'unknown'}, {**reference, 'path': 'other.docx'},
                    {'id': 'word-file', 'kind': 'fake'}, {'kind': 'file_content', 'path': 'test.docx', 'text': ''}):
        with pytest.raises(ValueError, match='verification'):
            compile_checks([reference, invalid])


def test_actions_are_optional_hints_and_cannot_grant_cli():
    plan = compile_steps([step('创建', actions=['右键点击并选择新建', 'press_key', 'press', {}, 'cli'])])
    assert plan['steps'][0]['actions'] == ['press', 'cli']
    assert plan['planning_warnings'][0]['step'] == 1
    for bad in (None, 'cli', ['通过 CLI 创建文件'], {'cli': True}):
        assert compile_steps([step('创建', actions=bad)])['steps'][0]['actions'] == []


def test_optional_hints_do_not_relax_required_evidence_or_identical_repair():
    incomplete = step('创建')
    del incomplete['verification']
    with pytest.raises(ValueError, match='verification'):
        compile_steps([incomplete], repair=True)
    previous = compile_steps([step('创建')])
    current = compile_steps([step('创建', actions=['描述下一步'])], repair=True)
    with pytest.raises(ValueError, match='重复'):
        validate_repair(current, {'strategy_change': '输入同一个操作的新描述'}, {'remaining_steps': previous['steps']})


def test_recovery_uses_observed_directory_and_missing_output_without_extra_request():
    proposal = step('在当前目录创建并保存文档', actions=['在空白处右击并选择新建'])
    model = SimpleNamespace(last_call={}, chat=Mock(return_value=json.dumps(
        {'strategy_change': '已确认目标目录，改用新建文档并读盘核实', 'steps': [proposal]}, ensure_ascii=False)))
    old = step('探索并找到目录')
    state = SimpleNamespace(task_profile={'kind': 'desktop'}, memory=SimpleNamespace(
        plan={'steps': [old]}, action_history=['单击 Download']),
        queue=SimpleNamespace(cursor=0, current=old), step_review={}, step_progress={}, recovery={},
        obs=Observation(info={'task_mode': 'desktop', 'desktop_focus_stable': True, 'desktop_capture_id': 'cap-4',
                              'runtime_state': {'source': 'windows_window_api', 'applications': [
            dict(hwnd=9, pid=10, process_name='explorer.exe', class_name='CabinetWClass', foreground=True,
                 enabled=True, visible=True, responsive=True, minimized=False, cloaked=False, bounds=[0, 0, 800, 600])]},
                              'explorer_location': {'status': 'observed', 'hwnd': 9, 'path': r'D:\Download'},
                              'cli_checks': [{'predicate': proposal['verification'][0], 'result': {
                                  'status': 'executed', 'file': {'exists': False}}}]}))
    accepted, events = [], []
    service = PlanningService(model, lambda: 60, Mock(), lambda event, **data: events.append((event, data)))
    assert service.plan('创建 test.docx', state, {'checks': []}, [], [], {},
                        lambda plan, call: accepted.append(plan), repair=True)
    assert model.chat.call_count == 1
    assert accepted[0]['steps'][0]['actions'] == []
    diagnostic = dict(events)['replan_context']['brief']['desktop_state']
    assert diagnostic == {'explorer_directory': r'D:\Download', 'missing_files': [r'D:\Download\test.docx']}
    assert 'planning_hints_ignored' in dict(events)


@pytest.mark.parametrize('remaining, expected', [(3, 'proposal_rejected'), (0, 'exhausted')])
def test_recovery_termination_explains_why_no_plan_was_installed(remaining, expected):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.max_steps, pipeline._replans, pipeline.max_replans = 40, 0, remaining
    pipeline._task_time_remaining = lambda: 60
    pipeline.planning_calls = []
    def reject(*args, **kwargs):
        pipeline.planning_calls.append({'validation_error': '缺少 verification'})
        return False
    pipeline._plan = reject
    pipeline._publish_diagnostic = Mock()
    result = SimpleNamespace(trajectory=[])
    state = SimpleNamespace(task='创建', queue=None, memory=Mock(), step_review={}, execution_feedback={},
                            failure_key='', failure_class='step_no_progress', step_progress={})
    assert pipeline._recover_plan(state, result, 4)
    assert result.trajectory[-1]['recovery_status'] == expected
    assert pipeline._publish_diagnostic.call_args.kwargs['status'] == expected
    if remaining:
        assert 'verification' in result.final_answer


def test_original_word_creation_requires_real_new_docx_in_the_requested_directory(tmp_path):
    job = rf'在{tmp_path}创建一个test的文档'
    if not tmp_path.drive:
        job = '新建 test.docx'
    verifier = CompletionVerifier(job, task_mode='desktop')
    env = SimpleNamespace(cli_target='local', initial_state={'workspace_dir': str(tmp_path)})
    verifier.bind_desktop_files(env)
    target = tmp_path / 'test.docx'
    assert verifier.evaluate()['status'] == 'failed'
    target.write_bytes(b'')
    assert verifier.evaluate()['status'] == 'failed'
    target.write_text('plain text', encoding='utf-8')
    assert verifier.evaluate()['status'] == 'failed'
    with ZipFile(target, 'w') as doc:
        doc.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        doc.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        doc.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>')
    assert verifier.evaluate()['status'] == 'passed'
    preexisting = CompletionVerifier(job, task_mode='cli')
    preexisting.bind_desktop_files(env)
    assert preexisting.evaluate()['status'] == 'failed'
    gui = CompletionVerifier('用 Word 新建 test.docx', task_mode='desktop')
    assert [c['kind'] for c in gui.public_spec['checks']] == ['desktop_file', 'unresolved_requirement']
    assert CompletionVerifier('打开 test.docx', task_mode='desktop').public_spec['checks'] == []


@pytest.mark.parametrize('instruction', [
    '通过Word创建文档', '通过 GUI 创建文档', '必须走GUI创建文件', '禁止使用 CLI 创建文档'])
def test_planner_cli_hint_cannot_override_a_gui_requirement(instruction):
    assert not desktop_cli_step(instruction, {'goal': '创建文件', 'actions': ['cli']})
    assert desktop_cli_step('创建 test.docx', {'goal': '创建文件', 'actions': ['cli']})


def test_compound_creation_retains_format_and_remaining_requirements():
    checks = CompletionVerifier(r'使用Word新建文档，输入结果并另存为 D:\Download\test.docx，关闭窗口',
                                task_mode='desktop').public_spec['checks']
    assert checks[0]['path'] == r'D:\Download\test.docx'
    assert checks[0]['format'] == 'docx' and checks[0]['change'] == 'created'
    assert checks[1]['kind'] == 'unresolved_requirement'
    assert '关闭窗口' in checks[1]['text']
