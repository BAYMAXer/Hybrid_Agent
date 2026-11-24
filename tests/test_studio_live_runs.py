import asyncio
import json
import tempfile
import unittest
from collections import OrderedDict
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from hybrid_agent.viz import server
from hybrid_agent.viz.logging import _LiveLogQueue


class StudioLiveRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.tmp = stack.enter_context(tempfile.TemporaryDirectory())
        for name, value in (('_workbench_cache', OrderedDict()), ('_workbench_run', None),
                            ('_workbench_sequence', 0), ('logs', []), ('clients', [])):
            stack.enter_context(patch.object(server, name, value))
        stack.enter_context(patch.dict(server._agent_state, status='idle', proc=None, run_id=None))
        stack.enter_context(patch.dict(server._batch_state, running=False))
        stack.enter_context(patch.object(server, '_local_runner_command', return_value=['runner']))
        stack.enter_context(patch.object(server, '_reader_thread'))
        self.spawn = stack.enter_context(patch.object(server.subprocess, 'Popen'))
        self.spawn.side_effect = lambda *a, **kw: Mock(poll=Mock(return_value=None), pid=1234)

    async def launch(self, task):
        return await server.start_run(SimpleNamespace(url=SimpleNamespace(port=8197), json=AsyncMock(
            return_value={'mode': 'local', 'task': task, 'result_dir': self.tmp})))

    async def post(self, run, event, **fields):
        payload = {'kind': 'status', 'event': event, 'studio_run_id': run, 'run_id': run, **fields}
        return await server.receive_log(SimpleNamespace(json=AsyncMock(return_value=payload)))

    async def test_live_delivery_replay_and_late_events(self):
        pending = asyncio.Queue()
        server.clients.append(pending)
        first, competing = await asyncio.gather(self.launch('read desktop'), self.launch('duplicate'))
        run = first['run_id']
        self.assertFalse(competing['ok'])
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(self.spawn.call_args.kwargs['env']['AGENTS_VIZ_RUN_ID'], run)
        self.assertEqual(self.spawn.call_args.kwargs['env']['AGENTS_VIZ_SERVER_URL'], 'http://127.0.0.1:8197')
        self.assertEqual(pending.get_nowait()['event'], 'workbench_start')
        await self.post(run, 'phase_timing', phase='startup')
        await self.post(run, 'workbench_start', instruction='read desktop')
        record = {'event_type': 'action_context', 'event_id': run + ':e1', 'step': 1,
                  'observation': {'screenshot': 'data:image/png;base64,AQID'}}
        await self.post(run, 'workbench_record', record=record)
        events = [pending.get_nowait() for _ in range(pending.qsize())]
        self.assertEqual(events[-1]['record'], record)
        status = await server.run_status()
        self.assertEqual((status['run_id'], status['current_step'], status['status']), (run, 1, 'running'))
        replay = await server.history(run_id=run)
        self.assertEqual([e['event'] for e in replay['logs']], ['workbench_start', 'phase_timing', 'workbench_record'])
        self.assertEqual((await server.history(replay['cursor'], run))['logs'], [])
        self.assertEqual((await server.history(replay['cursor'] + 1, run))['logs'], replay['logs'])
        old_proc = server._agent_state['proc']
        server._agent_state['output'] = ['RuntimeError: verification unavailable']
        server._finish_run(old_proc, 1, 1.5, None)
        self.assertEqual((await server.run_status())['status'], 'failed')
        terminal = (await server.history(replay['cursor'], run))['logs']
        self.assertEqual(terminal[0]['record']['error_type'], 'RuntimeError: verification unavailable')
        new = (await self.launch('inspect another window'))['run_id']
        self.assertNotEqual(run, new)
        self.assertTrue((await self.post(run, 'workbench_start', instruction='stale'))['ignored'])
        self.assertEqual((await server.history(run_id=run))['logs'], [])
        current = await server.history()
        self.assertEqual([e['run_id'] for e in current['logs']], [new])
        server._finish_run(old_proc, 1, 2, None)
        self.assertEqual((await server.run_status())['status'], 'running')

    async def test_success_recovers_child_outcome_and_spawn_error_is_visible(self):
        run = (await self.launch('read a title'))['run_id']
        timing = Path(self.tmp, 'run_timing.json')
        outcome = {'success': True, 'final_answer': 'Calculator'}
        timing.with_name('trajectory.json').write_text(json.dumps({'run_manifest': {'run_id': run},
            'outcome': outcome}), encoding='utf-8')
        self.assertIsNone(server._saved_outcome(timing, 'previous-run'))
        server._finish_run(server._agent_state['proc'], 0, 3, server._saved_outcome(timing, run))
        self.assertEqual((await server.history(run_id=run))['logs'][-1]['record']['outcome'], outcome)
        self.spawn.side_effect = FileNotFoundError('missing runner')
        failed = await self.launch('next task')
        self.assertFalse(failed['ok'])
        replay = await server.history(run_id=failed['run_id'])
        self.assertEqual([p['event'] for p in replay['logs']], ['workbench_start', 'workbench_record'])
        self.assertEqual(replay['logs'][-1]['record']['error_type'], 'missing runner')
        self.assertEqual((await server.run_status())['error'], 'missing runner')

    def test_websocket_delivers_before_exit_and_stop_is_replayable(self):
        with TestClient(server.app) as client, client.websocket_connect('/ws') as socket:
            run = client.post('/run/start', json={'mode': 'local', 'task': 'read desktop',
                                                  'result_dir': self.tmp}).json()['run_id']
            self.assertEqual(socket.receive_json()['run_id'], run)
            event = {'kind': 'status', 'event': 'workbench_record', 'run_id': run,
                     'studio_run_id': run, 'record': {'event_type': 'action_context', 'step': 1}}
            client.post('/log', json=event).raise_for_status()
            action = socket.receive_json()
            self.assertEqual(action['record'], event['record'])
            self.assertEqual(client.get('/run/status').json()['status'], 'running')
            with patch.object(server, '_stop_runner_process'):
                self.assertTrue(client.post('/run/stop').json()['ok'])
            terminal = socket.receive_json()
            self.assertEqual(terminal['record']['error_type'], '任务已停止')
            replay = client.get('/history', params={'run_id': run, 'since': action['_wb_seq']}).json()
            self.assertEqual(replay['logs'], [terminal])

    def test_backpressure_preserves_action_order_and_latest_preview(self):
        pending = _LiveLogQueue(maxsize=3)
        for seq, event in enumerate(('workbench_start', 'live_frame', 'live_frame', 'workbench_record',
                                     'workbench_record')):
            self.assertTrue(pending.enqueue({'event': event, 'run_id': 'task', 'seq': seq}))
            if seq == 2:
                self.assertEqual([item['seq'] for item in pending.queue], [0, 2])
        self.assertFalse(pending.enqueue({'event': 'phase_timing'}))
        self.assertEqual([pending.get_nowait()['seq'] for _ in range(3)], [0, 3, 4])
        for _ in range(3):
            pending.task_done()
        self.assertEqual(pending.unfinished_tasks, 0)
