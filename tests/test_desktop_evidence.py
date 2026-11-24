import json
import os
import secrets
from base64 import b64decode
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZipFile

import httpx
from PIL import Image, ImageDraw, ImageFont
import pytest

from hybrid_agent.adapters.cli_env import CLIEnvironment
from hybrid_agent.env import Observation
from hybrid_agent.model.decision_judge import DecisionJudgeConfig
from hybrid_agent.model.decision_model import ChatModel
from hybrid_agent.model.judge_selection import RuntimeDecisionJudge
from hybrid_agent.pipeline import Pipeline
from hybrid_agent.planning.plan_compiler import compile_plan
from hybrid_agent.planning.task_specs import task_analysis, task_profile
from hybrid_agent.reporting.report_delivery import collect_sources, generate_report, report_fingerprint, report_source_images
from hybrid_agent.reporting.report_review import review_packet
from hybrid_agent.runtime.cli_executor import file_check_samples
from hybrid_agent.verification.desktop_files import durable_file_predicates
from hybrid_agent.verification.judge_evidence import select_frame
from hybrid_agent.verification.step_verifier import code_review
from hybrid_agent.verification.verifiers import CompletionVerifier


def desktop_observation():
    return Observation(screenshot=Image.new('RGB', (64, 32)),
        context=[{'kind': 'desktop_ax', 'window_ref': 'w1', 'snapshot_id': 'snap',
                  'desktop_capture_id': 'cap', 'title': '工作窗口', 'nodes': [
                      {'ref': '1', 'role': 'button', 'name': 'Save', 'direct': True, 'actions': ['click']},
                      {'ref': '2', 'role': 'button', 'name': 'Disabled', 'direct': True, 'states': {'disabled': True}}]}],
        info={'task_mode': 'desktop', 'active_surface': 'desktop', 'desktop_capture_id': 'cap',
              'runtime_state': {'applications': [{'pid': 1, 'hwnd': 2, 'title': '工作窗口', 'foreground': True}]}})


def test_jev_native_channel_and_review_wire():
    calls = []

    def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        answers = {}
        for name, question in payload['questions'].items():
            scores = dict.fromkeys(question['criteria'], 0.0)
            scores[('desktop_ax' if name == 'next_mode' else next(iter(scores)))] = 1.0
            answers[name] = {'type': 'choice', 'probabilities': scores}
        return httpx.Response(200, json={'answers': answers})

    evaluator = RuntimeDecisionJudge(DecisionJudgeConfig(url='https://test.invalid', review_mode='exclusive'),
                                     transport=httpx.MockTransport(transport))
    try:
        obs = desktop_observation()
        step = {'id': 'save', 'goal': 'Save the report'}
        outcome = evaluator.select_mode('Save the report', step, obs)
        assert outcome['answers']['next_mode']['label'] == 'desktop_ax'
        question = calls[-1]['questions']['next_mode']
        assert set(question['criteria']) == {'desktop_ax', 'gui', 'unknown'}
        assert len(json.loads(calls[-1]['state'])['desktop_windows'][0]['controls']) == 1
        evaluator.assess_transition('Save the report', step, obs, obs, {}, {})
        assert set(calls[-1]['questions']) == {'settled', 'correct', 'next_mode', 'replan'}
        assert 'desktop_ax' in calls[-1]['questions']['next_mode']['criteria']
        assert 'dom' not in calls[-1]['questions']['next_mode']['criteria']
        assert evaluator.review('Save the report', step, obs, obs, {}, {})['status'] == 'passed'
        assert set(calls[-1]['questions']['step']['criteria']) >= {'progress', 'pending'}
        assert json.loads(calls[-1]['state'])['after']['nodes'][0]['window_ref'] == 'w1'
        evaluator.review('Save the report', {'id': 'whole_task'}, obs, obs, {}, {})
        assert 'progress' not in calls[-1]['questions']['step']['criteria']
    finally:
        evaluator.close()


def test_stale_native_tree_cannot_be_used():
    obs = desktop_observation()
    obs.info['desktop_capture_id'] = 'different'
    assert not select_frame(obs)['nodes']


def test_desktop_report_can_cite_window_native_controls_file_and_screenshot():
    obs = desktop_observation()
    obs.info['cli_result'] = {'status': 'executed', 'operation': 'read',
                            'file': {'path': 'C:/workspace/result.txt', 'is_file': True}, 'stdout': 'saved text'}
    state = SimpleNamespace(obs=obs, task='总结目前的应用状态', report_sources=[], step_id=2)
    collect_sources(state)
    sources = {s['kind']: s for s in state.report_sources}
    assert {'desktop_window', 'desktop_ax', 'target_file', 'desktop_screen'} == set(sources)
    assert 'saved text' in sources['target_file']['text']
    assert 'Save' in sources['desktop_ax']['text']
    screen = sources['desktop_screen']
    images = report_source_images(state, state.report_sources)
    assert images and images[0][0] == screen['id'] and images[0][1].startswith('data:image/')
    packet, error = review_packet(state.task, {'status': 'complete', 'sources': state.report_sources,
                                             'text': f"已观察到操作界面 [{screen['id']}]."})
    assert not error and packet['visual_source_ids'] == [screen['id']]
    assert all('data:image/' not in s['text'] for s in packet['observed_sources'])

    class Writer:
        vision = True
        build_vision_message = ChatModel.build_vision_message

        def chat(self, messages, retries):
            assert any(item['type'] == 'image_url' for item in messages[-1]['content'])
            return f"已观察到操作界面 [{screen['id']}]."

    state.report_delivery = {}
    report = generate_report(state, Writer(), Mock(), Mock(), lambda: 20)
    assert report['status'] == 'complete'


def test_desktop_final_rechecks_plan_files_after_model_pass(tmp_path):
    target = tmp_path / 'report.txt'
    target.write_text('expected content', encoding='utf-8')
    env = local_env(tmp_path)
    env.task_mode = 'desktop'
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.env = env
    pipeline.completion = CompletionVerifier('Edit the report')
    pipeline._model_final_report = {'status': 'passed', 'review_scope': 'text_report'}
    pipeline._task_time_remaining = lambda: 15
    pipeline._emit = Mock()
    state = SimpleNamespace(obs=Observation(), memory=SimpleNamespace(plan={'steps': [
        {'phase': 'final', 'verification': [{'kind': 'file_content', 'path': target.name, 'text': 'expected content'}]}]}))
    assert pipeline._final_code_evidence(state, pipeline.completion.evaluate())['file_checks'][0]['status'] == 'passed'
    target.unlink()
    result = SimpleNamespace(success=True, termination_reason='verified')
    assert Pipeline._evaluate.__wrapped__(pipeline, result, state) == 0.0
    assert not result.success and result.termination_reason == 'verification_changed'
    assert result.verification['file_checks'][0]['status'] in {'failed', 'unknown'}


def evaluate_desktop_control_verdict(before, current):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.env = SimpleNamespace(task_mode='desktop', evaluation_available=False)
    pipeline.completion = CompletionVerifier('Verify the application setting', task_mode='desktop')
    pipeline._reviewed_download_facts = []
    pipeline._reviewed_observation = pipeline._review_fingerprint(before)
    pipeline._model_final_report = {'method': 'decision_model', 'status': 'passed', 'review_scope': 'whole_task'}
    state = SimpleNamespace(obs=before, memory=SimpleNamespace(plan={'steps': []}))
    pipeline._reviewed_state = state
    pipeline._parallel_final_snapshot = lambda: (pipeline.completion.evaluate(), [], current)
    pipeline._verify_finish = Mock(return_value=(False, 'Current setting differs'))
    pipeline._emit = Mock()
    result = SimpleNamespace(success=True, termination_reason='verified')
    pipeline._evaluate(result, state)
    return pipeline._verify_finish, result


def test_final_review_reuses_unchanged_desktop_control_across_ref_refresh():
    before, current = desktop_observation(), desktop_observation()
    current.info['desktop_capture_id'] = 'new-capture'
    current.context[0].update(window_ref='w2', snapshot_id='snap2', desktop_capture_id='new-capture')
    current.context[0]['nodes'][0]['ref'] = 'new-lease'
    current.context[0]['nodes'][0]['parent'] = 'new-parent-lease'
    review, result = evaluate_desktop_control_verdict(before, current)
    review.assert_not_called()
    assert result.success and result.reward_source == 'independent_model_review'


def test_final_review_rechecks_changed_desktop_control_on_identical_pixels():
    before, current = desktop_observation(), desktop_observation()
    target = before.context[0]['nodes'][0]
    target.update(role='checkbox', name='AutoSave', states={'checked': True})
    current.context[0]['nodes'][0].update(role='checkbox', name='AutoSave', states={'checked': False})
    review, result = evaluate_desktop_control_verdict(before, current)
    review.assert_called_once()
    reviewed_state, _ = review.call_args.args
    assert reviewed_state.obs is current
    assert not result.success and result.termination_reason == 'verification_changed'


def test_unchanged_desktop_observation_does_not_create_new_sources():
    obs = desktop_observation()
    obs.info['desktop_focus_stable'] = True
    state = SimpleNamespace(obs=obs, task='告诉我应用中的信息', report_sources=[], step_id=1)
    collect_sources(state)
    before = report_fingerprint(state)
    state.step_id = 20
    obs.info.update(captured_at=50, desktop_capture_id='new-cap')
    obs.context[0].update(desktop_capture_id='new-cap', window_ref='new-window-ref', snapshot_id='new-snap')
    collect_sources(state)
    assert len(state.report_sources) == 3
    assert report_fingerprint(state) == before


def test_report_crops_only_the_confirmed_foreground_window():
    obs = desktop_observation()
    obs.screenshot.paste('red', (0, 0, 40, 32))
    obs.screenshot.paste('blue', (40, 0, 64, 32))
    obs.info.update(screenshot_space='screen_pixels', screen_origin=[-100, 10], desktop_focus_stable=True)
    obs.info['runtime_state']['applications'][0]['bounds'] = [-90, 10, -60, 40]
    obs.info['runtime_state']['applications'].append({'title': 'PRIVATE_BACKGROUND', 'foreground': False})
    obs.context[0]['nodes'].extend([
        {'ref': 'h1', 'role': 'text', 'name': 'PRIVATE_HIDDEN', 'states': {'hidden': True}},
        {'ref': 'p1', 'role': 'password', 'name': 'Password', 'value': 'PRIVATE_PASSWORD'},
    ])
    state = SimpleNamespace(obs=obs, task='告诉我应用中的信息', report_sources=[], step_id=1)
    collect_sources(state)
    screen = next(s for s in state.report_sources if s['kind'] == 'desktop_screen')
    assert screen['image_scope'] == 'foreground_window'
    url = report_source_images(state, state.report_sources)[0][1]
    result = Image.open(BytesIO(b64decode(url.split(',', 1)[1])))
    assert result.size == (30, 30)
    assert result.getextrema()[0][0] > 200 and result.getextrema()[2][1] < 30
    assert 'PRIVATE_' not in json.dumps(state.report_sources, ensure_ascii=False)
    assert 'PRIVATE_' not in json.dumps(select_frame(obs), ensure_ascii=False)
    obs.info['desktop_focus_stable'] = False
    state.report_sources.clear()
    collect_sources(state)
    assert not report_source_images(state, state.report_sources)


@pytest.mark.skipif(os.environ.get('HYBRID_AGENT_REAL_TERRA_REPORT') != '1',
                    reason='requires a signed-in Codex app-server with Terra access')
def test_real_terra_desktop_visual_report_without_web_sources(monkeypatch):
    """Exercise generation and final Jev review with a real model and synthetic pixels."""
    from hybrid_agent import pipeline as pipeline_module
    from hybrid_agent.model.terra_verification import TerraAPI, TerraChat, TerraJudge

    monkeypatch.setattr(pipeline_module, '_viz_log_status', None)
    code = ''.join(secrets.choice('ACEFHLPRTUVXY3479') for _ in range(4))
    frame = Image.new('RGB', (560, 300), 'white')
    draw = ImageDraw.Draw(frame)
    draw.rectangle((10, 10, 550, 66), fill='#28334a')
    draw.text((25, 26), 'Synthetic status panel', fill='white', font=ImageFont.load_default(size=22))
    draw.text((280, 165), code, fill='#0c1330', anchor='mm', font=ImageFont.load_default(size=110))
    task = '请告诉我测试应用画面中央的四字符代码。只给出代码和引用编号。'
    obs = Observation(screenshot=frame, info={'task_mode': 'desktop', 'active_surface': 'desktop'})
    state = SimpleNamespace(obs=obs, task=task, report_sources=[], report_delivery={}, step_id=1,
                            final_review_state={}, memory=SimpleNamespace(plan={'steps': []}),
                            judge_checkpoints=[], routing_hint={})
    collect_sources(state)
    assert len(state.report_sources) == 1 and state.report_sources[0]['kind'] == 'desktop_screen'
    assert code not in json.dumps(state.report_sources, ensure_ascii=False)
    api = TerraAPI()
    env = SimpleNamespace(task_mode='desktop', evaluation_available=False)
    model = TerraChat(api, 'decision')
    pipeline = Pipeline(model=model, grounding=None, env=env, system_prompt='',
        decision_judge=lambda: TerraJudge(api, DecisionJudgeConfig(review_mode='exclusive', url='https://unused.invalid')))
    pipeline.completion = CompletionVerifier(task)
    try:
        pipeline._ensure_report(state)
        assert state.report_delivery['status'] == 'complete', state.report_delivery.get('error')
        assert code in state.report_delivery['text'] and '[S1]' in state.report_delivery['text']
        confirmed, reason = pipeline._verify_finish(state, '')
        assert confirmed, reason
        call = pipeline.decision_judge.last_call
        assert call['backend'] == 'codex_app_server' and call['purpose'] == 'report_review'
        assert len(call['request']['images']) == 1
        packet = json.loads(call['request']['state'])
        assert packet['image_order'] == ['S1'] and packet['observed_sources'][0]['kind'] == 'desktop_screen'
        assert not packet['observed_sources'][0].get('url')
        assert 'data:image/' not in call['request']['state']
        result = SimpleNamespace(success=True, termination_reason='verified')
        assert pipeline._evaluate(result, state) == 0.0
        assert result.success and result.reward_source == 'independent_model_review'
    finally:
        pipeline.decision_judge.close()


def local_env(tmp_path):
    return SimpleNamespace(cli_target='local', initial_state={'workspace_dir': str(tmp_path)})


def test_browser_contract_inference_is_scoped_to_browser_capable_modes():
    tasks = [('请下载一篇叫做 Attention is All You Need 的论文', 'paper_download'),
             ('下载 https://example.invalid/file.pdf', 'download'),
             ('打开 https://example.invalid/', 'url_equals')]
    for task, expected in tasks:
        for mode in (None, 'browser'):
            assert CompletionVerifier(task, task_mode=mode).public_spec['checks'][0]['kind'] == expected
        for mode in ('desktop', 'cli'):
            explicit = {'checks': [{'kind': 'desktop_file', 'path': 'paper.pdf'}]}
            private = {'checks': [{'kind': 'desktop_file', 'path': 'private.pdf'}]}
            assert CompletionVerifier(task, task_mode=mode).public_spec == {'checks': []}
            verifier = CompletionVerifier(task, explicit, private, task_mode=mode)
            assert [c['path'] for c in verifier.public_spec['checks']] == ['paper.pdf']
            assert [c['path'] for c in verifier.spec['checks']] == ['paper.pdf', 'private.pdf']
            assert task_profile(task, verifier.public_spec, task_mode=mode)['kind'] == mode
            assert task_analysis(task, verifier.public_spec, task_mode=mode)['kind'] == mode


def test_cli_completion_binds_explicit_file_before_first_action(tmp_path):
    task = '请下载一篇叫做 Existing File 的论文'
    spec = {'checks': [{'kind': 'desktop_file', 'path': 'report.txt', 'change': 'created'}]}
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.completion = CompletionVerifier(task, spec, task_mode='cli')
    pipeline.env = CLIEnvironment(workspace_dir=tmp_path)
    pipeline.run_id = 'test-cli'
    pipeline._publish_diagnostic = Mock()
    state = SimpleNamespace()
    pipeline._prepare_initial_environment(task, state, SimpleNamespace(run_manifest={}))
    output = tmp_path.joinpath(os.path.basename(pipeline.env.workspace_dir), 'report.txt')
    output.write_text('generated file', encoding='utf-8')
    assert state.task_profile['kind'] == 'cli'
    assert pipeline.completion.evaluate()['status'] == 'passed'


def test_cli_report_sources_require_a_real_file_readback(tmp_path):
    task = '总结工作区中指定文件的内容'
    env = CLIEnvironment(workspace_dir=tmp_path)
    env.prepare_workspace('test-report')
    output = tmp_path.joinpath(os.path.basename(env.workspace_dir), 'report.txt')
    output.write_text('recorded evidence', encoding='utf-8')
    from hybrid_agent.runtime.cli_executor import execute_cli
    obs = env.observe()
    state = SimpleNamespace(obs=obs, task=task, report_sources=[], step_id=1)
    obs.info['cli_result'] = {'status': 'executed', 'operation': 'shell', 'stdout': 'UNVERIFIED CLAIM'}
    collect_sources(state)
    assert not state.report_sources
    obs.info['cli_result'] = execute_cli(env, {'operation': 'read', 'path': output.name})
    collect_sources(state)
    source, = state.report_sources
    assert source['kind'] == 'target_file' and not source['url']
    assert 'recorded evidence' in source['text']
    packet, error = review_packet(task, {'status': 'complete', 'sources': state.report_sources,
                                         'text': '内容为 recorded evidence [S1]。'})
    assert not error and not packet['visual_source_ids']


def test_native_pdf_save_plan_keeps_file_check_instead_of_download_receipt():
    task = '保存导出的 PDF 文件到工作区'
    predicate = {'kind': 'file_exists', 'path': 'result.pdf', 'file_type': 'file'}
    proposal = {'steps': [{'phase': 'final', 'goal': '保存导出的 PDF 文件',
                            'expected_evidence': 'result.pdf 已保存', 'verification': [predicate]}]}
    public_spec = {'checks': []}
    for mode in ('desktop', 'cli'):
        profile = task_profile(task, public_spec, task_mode=mode)
        assert compile_plan(proposal, task, public_spec, profile, [])['steps'][0]['verification'] == [predicate]
    browser = compile_plan(proposal, task, public_spec, task_profile(task, public_spec, task_mode='browser'), [])
    assert {'kind': 'artifact_download', 'format': 'pdf'} in browser['steps'][0]['verification']


def test_final_desktop_file_checks_creation_content_and_replacement(tmp_path):
    target = tmp_path / 'result.txt'
    verifier = CompletionVerifier('Create the report', {'checks': [
        {'kind': 'desktop_file', 'path': target.name, 'change': 'created', 'format': 'txt', 'text_equals': '结果\n'}]})
    verifier.bind_desktop_files(local_env(tmp_path))
    target.write_bytes('结果\r\n'.encode('utf-8-sig'))
    assert verifier.evaluate()['status'] == 'passed'
    target.write_text('different', encoding='utf-8')
    assert verifier.evaluate()['status'] == 'failed'
    target.unlink()
    target.mkdir()
    assert verifier.evaluate()['status'] == 'failed'
    target.rmdir()
    assert verifier.evaluate()['status'] == 'failed'


def test_final_desktop_file_keeps_baseline_and_private_expectations(tmp_path):
    target = tmp_path / 'old.txt'
    target.write_text('already there', encoding='utf-8')
    verifier = CompletionVerifier('Create the report', private_spec={'checks': [
        {'kind': 'desktop_file', 'path': target.name, 'change': 'created'}]})
    verifier.bind_desktop_files(local_env(tmp_path))
    target.write_text('new contents', encoding='utf-8')
    report = verifier.public_report()
    assert report['status'] == 'failed'
    assert 'old.txt' not in json.dumps(report)


def test_trusted_absolute_file_and_relative_scope(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    target = tmp_path / 'explicit.txt'
    target.write_text('DOCUMENT-CONTENTS', encoding='utf-8')
    verifier = CompletionVerifier('Read exact explicit output', {'checks': [
        {'kind': 'desktop_file', 'path': str(target), 'text_equals': 'DOCUMENT-CONTENTS'}]})
    verifier.bind_desktop_files(local_env(workspace))
    report = verifier.public_report()
    assert report['status'] == 'passed' and report['checks'][0]['file']['path'] == str(target)
    assert 'DOCUMENT-CONTENTS' not in json.dumps(report)
    verifier = CompletionVerifier('Verify relative output', {'checks': [
        {'kind': 'desktop_file', 'path': '../explicit.txt'}]})
    verifier.bind_desktop_files(local_env(workspace))
    assert verifier.evaluate()['status'] == 'unknown'


@pytest.mark.skipif(os.name != 'nt', reason='Windows may expose path and descriptor identities differently')
def test_windows_file_probe_compares_descriptor_fields_to_descriptor_fields(tmp_path, monkeypatch):
    target = tmp_path / 'result.txt'
    target.write_text('saved text', encoding='utf-8')
    verifier = CompletionVerifier('Read the result', {'checks': [
        {'kind': 'desktop_file', 'path': target.name, 'text_equals': 'saved text'}]})
    verifier.bind_desktop_files(local_env(tmp_path))
    original_fstat = os.fstat

    def descriptor_stat(fd):
        actual = original_fstat(fd)
        return SimpleNamespace(st_dev=actual.st_dev + 1, st_ino=actual.st_ino + 1, st_mode=actual.st_mode,
            st_size=actual.st_size, st_mtime_ns=actual.st_mtime_ns, st_ctime_ns=actual.st_ctime_ns)

    with monkeypatch.context() as isolated:
        isolated.setattr(os, 'fstat', descriptor_stat)
        assert verifier.evaluate()['status'] == 'passed'


def test_xlsx_independent_cell_and_formula_checks(tmp_path):
    path = tmp_path / 'table.xlsx'
    main = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    with ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets><sheet name="Data" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml',
            f'<worksheet xmlns="{main}"><sheetData><row><c r="A1"><v>25</v></c><c r="B1"><f>A1*2</f><v>50</v></c></row></sheetData></worksheet>')
    spec = {'checks': [{'kind': 'desktop_file', 'path': path.name, 'format': 'xlsx', 'cells': [
        {'sheet': 'Data', 'cell': 'A1', 'value': '25'}, {'sheet': 'Data', 'cell': 'B1', 'formula': '=A1*2'}]}]}
    verifier = CompletionVerifier('Save the spreadsheet', spec)
    verifier.bind_desktop_files(local_env(tmp_path))
    assert verifier.evaluate()['status'] == 'passed'
    spec['checks'][0]['cells'][1]['formula'] = '=A1*3'
    verifier = CompletionVerifier('Save the spreadsheet', spec)
    verifier.bind_desktop_files(local_env(tmp_path))
    assert verifier.evaluate()['status'] == 'failed'


def test_planned_file_content_reads_only_logical_docx_text(tmp_path):
    with ZipFile(tmp_path / 'document.docx', 'w') as document:
        document.writestr('[Content_Types].xml', '<Types/>')
        document.writestr('word/document.xml', '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
            <w:body><w:p><w:r><w:t>TERRA-</w:t></w:r><w:r><w:t>DOCX</w:t></w:r></w:p></w:body></w:document>''')
        document.writestr('customXml/item1.xml', '<metadata>UNREFERENCED-XML</metadata>')
    for marker, expected in [('TERRA-DOCX', 'passed'), ('UNREFERENCED-XML', 'failed')]:
        predicate = {'kind': 'file_content', 'path': 'document.docx', 'text': marker}
        samples = file_check_samples(local_env(tmp_path), [predicate], lambda: 10)
        report = code_review({'verification': [predicate]}, Observation(info={'cli_checks': samples}), {}, {})
        assert report['status'] == expected
        assert samples[0]['result']['stdout'] == ''  # Only match evidence leaves the target reader.


def test_planned_file_content_resolves_xlsx_cells_on_the_guest(tmp_path):
    with ZipFile(tmp_path / 'cells.xlsx', 'w') as workbook:
        workbook.writestr('[Content_Types].xml', '<Types/>')
        workbook.writestr('xl/workbook.xml', '''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" r:id="rId1"/></sheets></workbook>''')
        workbook.writestr('xl/_rels/workbook.xml.rels', '''<Relationships><Relationship Id="rId1"
            Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>''')
        workbook.writestr('xl/sharedStrings.xml', '''<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
            <si><t>TERRA-AX-42</t></si><si><t>UNUSED-SHARED-STRING</t></si></sst>''')
        workbook.writestr('xl/worksheets/sheet1.xml', '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
            <sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><r><t>INLINE-</t></r><r><t>CELL</t></r></is></c>
            <c r="C1" t="str"><v>FORMULA-CACHE</v></c></row></sheetData></worksheet>''')

    def run_guest_source(source):
        output = StringIO()
        with redirect_stdout(output):
            exec(source, {})
        return {'output': output.getvalue()}

    guest = SimpleNamespace(cli_target='guest', guest_workspace_dir=str(tmp_path),
                            controller=SimpleNamespace(run_python_script=run_guest_source))
    for marker, expected in [('TERRA-AX-42', 'passed'), ('INLINE-CELL', 'passed'),
                             ('FORMULA-CACHE', 'passed'), ('UNUSED-SHARED-STRING', 'failed')]:
        predicate = {'kind': 'file_content', 'path': 'cells.xlsx', 'text': marker}
        samples = file_check_samples(guest, [predicate], lambda: 10)
        report = code_review({'verification': [predicate]}, Observation(info={'cli_checks': samples}), {}, {})
        assert report['status'] == expected
        assert samples[0]['result']['content_scope'] == 'office_document_text'


def test_durable_checks_follow_moves_and_drop_discarded_files(tmp_path):
    steps = [{'verification': [{'kind': 'file_content', 'path': 'draft.txt', 'text': 'content'},
                               {'kind': 'file_exists', 'path': 'scratch.txt'}]},
             {'phase': 'final', 'verification': [{'kind': 'file_exists', 'path': 'done.txt'},
                                                  {'kind': 'file_absent', 'path': 'scratch.txt'}]}]
    checks = durable_file_predicates(steps, {'draft.txt': 'done.txt'}, str(tmp_path))
    assert {'kind': 'file_content', 'path': str(tmp_path / 'done.txt'), 'text': 'content'} in checks
    assert {'kind': 'file_absent', 'path': 'scratch.txt'} in checks
    assert {'kind': 'file_exists', 'path': 'scratch.txt'} not in checks


def test_required_file_type_rejects_directory():
    predicate = {'kind': 'file_exists', 'path': 'saved.txt', 'file_type': 'file'}
    obs = Observation(info={'cli_checks': [{'predicate': predicate, 'result': {
        'status': 'executed', 'file': {'exists': True, 'is_dir': True, 'is_file': False}}}]})
    assert code_review({'verification': [predicate]}, obs, {}, {})['status'] == 'failed'
def test_application_display_and_saved_file_do_not_require_separate_report():
    from hybrid_agent.reporting.report_delivery import wants_report
    task = '让计算器显示结果，然后切换到记事本，通过 GUI 把算得的数字写入 result.txt 并保存。'
    assert not wants_report(task, desktop=True)
    assert not wants_report('在工作表中显示结果，然后保存工作簿', desktop=True)
    assert wants_report(task + '另外请告诉我计算结果。', desktop=True)
    assert wants_report('让浏览器显示结果')
    assert wants_report('请给出计算结果和对比表格。')


def test_final_window_history_separates_dispatch_from_observed_disappearance():
    from types import SimpleNamespace
    from hybrid_agent.verification.judge_evidence import desktop_window_transition

    window = {'hwnd': 12, 'pid': 32, 'title': 'Settings', 'foreground': True}
    def frame(capture, windows, complete=True):
        return SimpleNamespace(info={'task_mode': 'desktop', 'desktop_capture_id': capture,
            'runtime_state': {'source': 'windows_window_api', 'complete': complete, 'applications': windows}})

    before, empty = frame('one', [window]), frame('two', [])
    sent = {'status': 'executed', 'dispatched': True}
    closed = desktop_window_transition(before, empty, 'hotkey', {'keys': ['alt', 'f4']}, sent, 1)
    assert closed['keys'] == ['alt', 'f4'] and closed['previous_window_listed_after'] is False
    background = frame('two', [dict(window, foreground=False)])
    assert desktop_window_transition(before, background, 'hotkey', {}, sent, 1)['previous_window_listed_after']
    assert 'previous_window_listed_after' not in desktop_window_transition(before, frame('two', [], False), 'hotkey', {}, sent, 1)
    assert desktop_window_transition(empty, empty, 'hotkey', {'keys': ['win', 'i']}, sent, 2)['keys'] == ['win', 'i']
    reopened = desktop_window_transition(empty, frame('three', [dict(window, hwnd=16)]), 'observe_desktop', {}, {}, 3)
    assert reopened['after']['hwnd'] == 16 and 'dispatched_action' not in reopened
    assert desktop_window_transition(empty, empty, 'hotkey', {'keys': ['win', 'i']}, {'dispatched': False}, 2) is None
    empty.info['screenshot_stale'] = True
    assert desktop_window_transition(before, empty, 'hotkey', {'keys': ['alt', 'f4']}, sent, 1) is None
