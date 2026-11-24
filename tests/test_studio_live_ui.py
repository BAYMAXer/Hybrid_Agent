"""Browser regression for desktop task submission and live observation replay."""
import multiprocessing
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path


def serve_studio(listener):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config("hybrid_agent.viz.server:app", log_level="error"))
    server.run(sockets=[listener])


class StudioLiveUITests(unittest.TestCase):
    def test_submit_live_frames_and_reload(self):
        try:
            import uvicorn
            from playwright.sync_api import expect, sync_playwright
        except ImportError as exc:
            self.skipTest(str(exc))

        playwright = self.enterContext(sync_playwright())
        candidates = [os.getenv("MINIWOB_CHROME_BINARY"), playwright.chromium.executable_path,
                      shutil.which("chromium"), shutil.which("google-chrome"),
                      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if os.getenv(key):
                candidates.append(Path(os.environ[key], "Google/Chrome/Application/chrome.exe"))
        executable = next((p for p in candidates if p and Path(p).is_file()), None)
        if not executable:
            self.skipTest("Install Playwright Chromium or set MINIWOB_CHROME_BINARY")
        browser = playwright.chromium.launch(headless=True, executable_path=executable)
        self.addCleanup(browser.close)
        page = browser.new_page()
        self.addCleanup(page.close)

        # The child owns its history and WebSocket clients independently of other tests.
        listener = self.enterContext(socket.socket())
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
        server = multiprocessing.get_context("spawn").Process(target=serve_studio, args=(listener,))
        server.start()
        self.addCleanup(server.join, 5)
        self.addCleanup(server.terminate)
        workspace = self.enterContext(tempfile.TemporaryDirectory())
        page.route("**/workspace/default", lambda route: route.fulfill(json={"ok": True, "workspace_dir": workspace}))

        pending = []
        task = "Read the Calculator display"
        running = {"status": "idle"}
        page.route("**/run/status", lambda route: route.fulfill(json=running))
        page.route("**/run/start", lambda route: pending.append(route))
        before = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                  "AAAAC0lEQVR42mP8/x8AAwMCAO+aQioAAAAASUVORK5CYII=")
        after = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAIAAAB7QOjd"
                 "AAAAD0lEQVR4nGPk081hYGAAAALzAKl5nYN7AAAAAElFTkSuQmCC")
        desktop = {"active_surface": "desktop"}

        def publish(run, event, **fields):
            response = page.request.post(origin + "/log", data={
                "kind": "status", "run_id": run, "event": event, **fields})
            self.assertTrue(response.ok, response.text())
            return response.json()

        def execute(run, step, target):
            return publish(run, "workbench_record", record={"event_type": "execution",
                "event_id": run + ":" + target, "parent_decision_id": f"a_{step}",
                "action": {"action": "click", "args": {"target": target}},
                "execution_info": {"status": "executed", "channel": "desktop"},
                "next_observation": {"info": desktop, "screenshot": after}})

        page.goto(origin + "/#live")
        expect(page.locator("#conn")).to_have_class("conn on")
        heading = page.locator("#live-workbench h2").first
        action = page.locator("#live-workbench .wb-action-line code")
        shot = page.locator("#live-workbench .wb-shot img")
        publish("old", "workbench_start", instruction="Previous task")
        execute("old", 7, "previous-target")
        expect(action).to_contain_text("previous-target")
        page.locator("#nav-run").click()
        page.locator("#local-task-mode").select_option("desktop")
        page.locator("#local-task").fill(task)
        with page.expect_request("**/run/start") as submitted:
            page.locator("#btn-local-start").click()
        request = submitted.value.post_data_json
        self.assertEqual((request["task"], request["task_mode"], request["workspace_dir"]),
                         (task, "desktop", workspace))

        # Deliver the first observation while the start response is still pending.
        running.update(status="running", run_id="current", task=task)
        publish("current", "workbench_start", instruction=task)
        publish("current", "workbench_record", record={"event_type": "action_context",
                "event_id": "current:context", "step": 1,
                "observation": {"info": desktop, "screenshot": before}})
        expect(heading).to_have_text(task)
        expect(shot).to_have_attribute("src", before)
        pending[0].fulfill(json={"ok": True, "run_id": "current"})
        expect(shot).to_be_visible()
        expect(shot).to_have_js_property("naturalWidth", 1)
        self.assertTrue(execute("old", 8, "late-old-target")["ignored"])
        execute("current", 1, "display")
        expect(action).to_contain_text("display")
        expect(shot).to_have_attribute("src", after)

        page.reload()
        expect(heading).to_be_visible()
        expect(heading).to_have_text(task)
        expect(shot).to_have_attribute("src", after)
        expect(shot).to_have_js_property("naturalWidth", 2)
        expect(action).to_contain_text("display")
        expect(page.locator("#live-workbench")).not_to_contain_text("previous-target")
        expect(page.locator("#live-workbench [data-wb=stop]")).to_be_enabled()
        execute("current", 2, "next-display")
        expect(action).to_contain_text("next-display")
