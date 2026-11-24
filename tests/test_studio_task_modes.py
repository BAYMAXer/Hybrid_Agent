import asyncio
import base64
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hybrid_agent import run_windows_local as runner
from hybrid_agent.viz import server


class StudioTaskModeTests(unittest.TestCase):
    def test_modes_and_demo_routing(self):
        demo = "http://127.0.0.1:8088/demo/paper-search"
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(server._local_task_options({})[0], "desktop")
            self.assertEqual(server._local_task_options({"browser_start_url": demo}),
                             ("browser", ["--task-mode", "browser", "--browser-start-url", demo]))
            self.assertEqual(server._local_task_options({"cli_only": True})[0], "cli")
            for cfg in ({"task_mode": "desktop", "browser_start_url": demo},
                        {"task_mode": "browser", "cli_only": True},
                        {"task_mode": "other"}, {"verification_backend": "wrong"},
                        {"disable_desktop_ax": "false"}):
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
                patch("hybrid_agent.persistent_chrome.browser_settings") as browser:
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
                with patch.object(sys, "argv", args), patch.dict(sys.modules,
                        {"pyautogui": SimpleNamespace(size=lambda: (320, 200))}), \
                        patch.object(runner, "ensure_chrome", return_value=(True, tmp)) as start, \
                        patch.object(runner, "close_test_chrome") as stop, \
                        patch.object(runner, "WindowsEnvironment") as env, patch.object(runner, "Agent") as agent, \
                        patch.object(runner, "log_status"), patch.object(runner, "_lifecycle_status"):
                    agent.return_value.run.side_effect = RuntimeError("test episode initialized")
                    with self.assertRaisesRegex(RuntimeError, "test episode initialized"):
                        runner.main("windows")
                    self.assertEqual(env.call_args.kwargs["task_mode"], mode)
                    self.assertEqual(start.call_count, int(mode == "browser"))
                    self.assertEqual(stop.call_count, int(mode == "browser"))
                    env.return_value.close.assert_called_once()
