import asyncio
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hybrid_agent import run_windows_local as runner
from hybrid_agent.viz import server


PRIMARY = {'id': 'DISPLAY-A', 'name': 'Office', 'width': 2560, 'height': 1440, 'primary': True}
SECONDARY = {'id': 'DISPLAY-B', 'name': 'Office', 'width': 1920, 'height': 1080, 'primary': False}


class StudioTaskModeTests(unittest.TestCase):
    def test_modes_and_demo_routing(self):
        demo = "http://127.0.0.1:8088/demo/paper-search"
        with patch.object(sys, "platform", "win32"), patch('hybrid_agent.desktop.desktop_capture.resolve_desktop_monitor', return_value=PRIMARY):
            self.assertEqual(server._local_task_options({}), ("desktop", ["--task-mode", "desktop", "--desktop-monitor", "DISPLAY-A"]))
            self.assertEqual(server._local_task_options({"browser_start_url": demo}),
                             ("browser", ["--task-mode", "browser", "--browser-start-url", demo]))
            self.assertEqual(server._local_task_options({"cli_only": True})[0], "cli")
            for cfg in ({"task_mode": "desktop", "browser_start_url": demo},
                        {"task_mode": "browser", "cli_only": True},
                        {"task_mode": "other"}, {"verification_backend": "wrong"},
                        {"disable_desktop_ax": "false"}, {"desktop_monitor": ""},
                        {"task_mode": "browser", "desktop_monitor": "DISPLAY-B"}):
                with self.assertRaises(ValueError):
                    server._local_task_options(cfg)
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(server._local_task_options({})[0], "cli")
            with self.assertRaises(ValueError):
                server._local_task_options({"task_mode": "desktop"})
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(server._local_task_options({})[0], "browser")

    def test_single_and_batch_pass_same_validated_terra_options(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "LOCAL_RUNS_DIR", tmp), \
                patch.dict(server._agent_state), patch.dict(server._batch_state, {"running": False, "stop": False}), \
                patch.object(server.threading, "Thread") as thread, patch.object(server.subprocess, "Popen") as spawn, \
                patch.object(server, "logs", []), patch.object(sys, "platform", "win32"), \
                patch("hybrid_agent.persistent_chrome.browser_settings") as browser, \
                patch("hybrid_agent.desktop.desktop_capture.resolve_desktop_monitor", return_value=PRIMARY) as resolve:
            cfg = {"workspace_dir": tmp, "verification_backend": "terra", "knowledge_method": "jev",
                   "disable_desktop_ax": True, "disable_ocr_grounding": True}
            task = "test action"
            self.assertTrue(server._start_local({**cfg, "task": task, "result_dir": tmp})["ok"])
            single = spawn.call_args.args[0]
            file_data = base64.b64encode(("instruction\n" + task + "\n").encode()).decode()
            body = {"filename": "sheet.csv", "content_b64": file_data, "config": cfg}
            request = SimpleNamespace(json=AsyncMock(return_value=body))
            self.assertTrue(asyncio.run(server.start_batch(request))["ok"])
            batch_command, tasks = thread.call_args.kwargs["args"]
            self.assertEqual(single, batch_command + ["--task", task, "--result-dir", tmp])
            self.assertEqual(tasks[-1]["instruction"], task)
            self.assertIn("--disable-desktop-ax", single)
            self.assertIn("--disable-ocr-grounding", single)
            self.assertNotIn("--model-api-key", single)
            self.assertNotIn("--browser-session-mode", single)
            self.assertEqual(single[single.index("--task-mode") + 1], "desktop")
            self.assertEqual(batch_command[batch_command.index("--desktop-monitor") + 1], PRIMARY['id'])
            self.assertEqual(single[single.index("--desktop-monitor") + 1], PRIMARY['id'])
            self.assertEqual(resolve.call_count, 2)  # Once per submission, before the worker starts.
            self.assertEqual(single[single.index("--verification-backend") + 1], "terra")
            browser.assert_not_called()
            server._batch_state["running"] = False
            body["config"] = {**cfg, "task_timeout_s": -1}
            self.assertFalse(asyncio.run(server.start_batch(request))["ok"])

    def test_runner_desktop_skips_chrome_and_explicit_browser_keeps_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            for mode in ("desktop", "browser"):
                args = ["run_local", "--task", "test", "--result-dir", tmp, "--workspace-dir", tmp]
                if mode == "browser":
                    args += ["--task-mode", "browser", "--browser-session-mode", "isolated"]
                else:
                    args += ["--desktop-monitor", SECONDARY['id']]
                with patch.object(sys, "argv", args), patch.dict(sys.modules,
                        {"pyautogui": SimpleNamespace(size=lambda: (320, 200))}), \
                        patch.object(runner, "ensure_chrome", return_value=(True, tmp)) as start, \
                        patch.object(runner, "close_test_chrome") as stop, \
                        patch.object(runner, "WindowsEnvironment") as env, patch.object(runner, "Agent") as agent, \
                        patch.object(runner, "log_status"), patch.object(runner, "_lifecycle_status"), \
                        patch("hybrid_agent.desktop.desktop_capture.resolve_desktop_monitor", return_value=SECONDARY) as resolve:
                    agent.return_value.run.side_effect = RuntimeError("test episode initialized")
                    with self.assertRaisesRegex(RuntimeError, "test episode initialized"):
                        runner.main("windows")
                    self.assertEqual(env.call_args.kwargs["task_mode"], mode)
                    if mode == "desktop":
                        resolve.assert_called_once_with(SECONDARY['id'])
                        self.assertEqual(env.call_args.kwargs["desktop_monitor"], SECONDARY['id'])
                        self.assertEqual(env.call_args.kwargs["width"], SECONDARY['width'])
                        self.assertEqual(agent.call_args.kwargs['run_manifest']['desktop_monitor_info'], SECONDARY)
                    else:
                        resolve.assert_not_called()
                        self.assertNotIn("desktop_monitor", env.call_args.kwargs)
                    self.assertEqual(start.call_count, int(mode == "browser"))
                    self.assertEqual(stop.call_count, int(mode == "browser"))
                    env.return_value.close.assert_called_once()

    def test_runtime_info_and_unplugged_selected_monitor(self):
        with patch.object(sys, 'platform', 'win32'), \
                patch('hybrid_agent.desktop.desktop_capture.list_desktop_monitors', return_value=[PRIMARY, SECONDARY]):
            self.assertEqual(asyncio.run(server.runtime_info())['desktop_monitors'], [PRIMARY, SECONDARY])
        with patch.object(sys, 'platform', 'win32'), \
                patch('hybrid_agent.desktop.desktop_capture.resolve_desktop_monitor', side_effect=ValueError('设备已拔出')), \
                patch.object(server.subprocess, 'Popen') as spawn:
            response = server._start_local({'task': 'read file', 'task_mode': 'desktop', 'desktop_monitor': SECONDARY['id']})
            self.assertFalse(response['ok'])
            self.assertIn(SECONDARY['id'], response['error'])
            spawn.assert_not_called()

    def test_runner_records_monitor_disconnected_between_batch_rows(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(sys, 'argv', ['runner', '--task', 'read document', '--result-dir', tmp,
                                           '--desktop-monitor', SECONDARY['id']]), \
                patch('hybrid_agent.desktop.desktop_capture.resolve_desktop_monitor', side_effect=ValueError('设备已拔出')), \
                patch.object(runner, 'Agent') as agent, patch.object(runner, '_lifecycle_status'), patch.object(runner, 'flush_logs'):
            self.assertEqual(runner.main('windows'), 1)
            agent.assert_not_called()
            trace = json.loads((Path(tmp) / 'trajectory.json').read_text(encoding='utf-8'))
            self.assertEqual(trace['run_manifest']['desktop_monitor'], SECONDARY['id'])
            self.assertFalse(trace['outcome']['success'])
            self.assertEqual(trace['outcome']['termination_reason'], 'desktop_monitor_unavailable')
            self.assertIn('设备已拔出', trace['outcome']['final_answer'])
            events = (Path(tmp) / 'events.jsonl').read_text(encoding='utf-8').splitlines()
            self.assertEqual(json.loads(events[-1])['event_type'], 'episode_error')
