from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from hybrid_agent.actions import Action
from hybrid_agent.adapters.windows_env import WindowsEnvironment
from hybrid_agent.desktop.hcu_ax import DesktopAX, browser_window
from hybrid_agent.pipeline import Pipeline


WINDOW = {"title": "Example", "hwnd": 21, "pid": 98, "process_name": "Notepad.exe", "bounds": [0, 0, 180, 120],
          "class_name": "Notepad", "foreground": True, "visible": True}


class Provider:
    def __init__(self):
        self.nodes = [{"role": "textbox", "ref": "r1", "name": "Name"},
                      {"role": "button", "ref": "r2", "name": "Save"},
                      {"role": "combobox", "ref": "r3", "name": "Colour"},
                      {"role": "option", "ref": "r4", "name": "Blue", "parent": "r3"}]
        self.sent = []
        self.read_count = 0
        self.record = {"ok": True, "input_emitted": True, "completed": 1}

    def apps(self):
        return {"windows": [{"ref": "w1", "app": "Notepad", "title": "Example"}]}

    def observe(self, window_ref):
        return {"app": {"pid": 98, "name": "Notepad"}, "window": {"ref": window_ref, "title": "Example"},
                "snapshot_id": "s1", "elements": self.nodes}

    def read(self, refs):
        self.read_count += 1
        return {"read": [{"ok": True, "ref": n["ref"], "element": n} for n in self.nodes if n["ref"] in refs]}

    def find(self, *, window_ref, name, limit):
        return {"matches": [n for n in self.nodes if n["name"] == name]}

    def act(self, steps, **kwargs):
        self.sent.append((steps, kwargs))
        return self.record

    def close(self):
        pass


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    lease_key = 'test-' + uuid4().hex
    monkeypatch.setattr('hybrid_agent.desktop.desktop_capture.windows_desktop_key', lambda: lease_key)
    monitor = dict(id='test-monitor', name='test', left=0, top=0, width=180, height=120,
                   work_area=[0, 0, 180, 120], dpi=[96, 96], primary=True)
    monkeypatch.setattr('hybrid_agent.desktop.desktop_capture.list_desktop_monitors', lambda: [monitor])
    state = {"applications": [dict(WINDOW)]}
    env = WindowsEnvironment(task_mode="desktop", workspace_dir=str(tmp_path))
    monkeypatch.setattr(env, "_desktop_is_input", lambda: True)
    monkeypatch.setattr(env, "_get_browser_session", lambda: pytest.fail("CDP opened in desktop mode"))
    monkeypatch.setattr(env, "_runtime_state", lambda: deepcopy(state))

    def capture():
        env._desktop_capture = {"desktop_capture_id": uuid4().hex, "screen_origin": [0, 0],
                                "virtual_desktop": {"left": 0, "top": 0, "width": 180, "height": 120}}
        return Image.new("RGB", (180, 120))

    monkeypatch.setattr(env, "_take_screenshot", capture)
    env.prepare_workspace("test")
    env.begin_episode("test")
    client = Provider()
    env._desktop_ax = DesktopAX(connect=lambda session: client)
    yield env, state, client
    env.close()


def test_desktop_observes_without_cdp_or_hcu(desktop):
    env, _, _ = desktop
    env._desktop_ax.enabled = False
    obs = env.reset("task")
    assert (obs.screenshot.size, obs.info["task_mode"], obs.info["active_surface"]) == ((180, 120), "desktop", "desktop")
    assert obs.info["desktop_ax_status"] == "disabled" and not obs.context
    assert env.step(Action("observe_browser", {})).info["dispatched"] is False
    assert env.step(Action("goto", {"url": "about:blank"})).info["error_code"] == "desktop_scope"


def test_fresh_observation_confirms_window_that_became_ready_after_timeout(desktop):
    env, state, _ = desktop
    window = state['applications'][0]
    window.update(minimized=False, cloaked=False, enabled=True, responsive=True, bounds=[0, 0, 180, 120])
    env._pending_window = dict(window, hwnd=window['hwnd'] + 1)
    env._last_window_wait = {'status': 'timeout'}
    assert env.observe().info['desktop_window_wait']['status'] == 'timeout'
    env._pending_window = dict(window)
    assert env.observe().info['desktop_window_wait']['status'] == 'completed'
    assert env._pending_window is None


@pytest.mark.parametrize("action,raw,args,expected", [
    ("click", "r2", {}, {"type": "click", "ref": "r2"}),
    ("type", "r1", {"text": "中文"}, {"type": "type", "ref": "r1", "text": "中文"}),
    ("type", "r1", {"text": "中文", "overwrite": True}, {"type": "set_value", "ref": "r1", "value": "中文"}),
    ("select", "r3", {"option": "Blue"}, {"type": "select", "ref": "r4"}),
])
def test_native_action_validates_and_dispatches_once(desktop, action, raw, args, expected):
    env, _, client = desktop
    obs = env.observe()
    node = next(n for n in obs.context[0]["nodes"] if n["ref"].endswith(":" + raw))
    request = Action(action, {"target": node["name"], "target_ref": node["ref"], **args})
    route = env.route_action(request, obs)
    assert env.execute_routed(request, route).info["status"] == "executed"
    assert len(client.sent) == client.read_count == 1 and client.sent[0][0] == [expected]
    assert client.sent[0][1]["request_id"] and client.sent[0][1]["window_ref"] == "w1"
    assert env.route_action(request, obs)["channel"] == "reobserve"
    assert env.execute_routed(request, route).info["dispatched"] is False
    assert len(client.sent) == 1


def test_no_dispatch_for_window_switch_or_ambiguous_option(desktop):
    env, state, client = desktop
    client.nodes.append({**client.nodes[-1], "ref": "r5"})
    obs = env.observe()
    selector = next(n for n in obs.context[0]["nodes"] if n["role"] == "combobox")
    assert env.route_action(Action("select", {"option": "Blue", "target_ref": selector["ref"]}), obs)["channel"] == "reobserve"
    node = next(n for n in obs.context[0]["nodes"] if n["role"] == "button")
    action = Action("click", {"target_ref": node["ref"]})
    route = env.route_action(action, obs)
    state["applications"][0]["hwnd"] = 23
    assert env.execute_routed(action, route).info["dispatched"] is False and not client.sent


def test_stop_prohibits_observation_and_replay(desktop, monkeypatch):
    env, _, client = desktop
    obs = env.observe()
    node = next(n for n in obs.context[0]["nodes"] if n["role"] == "button")
    action = Action("click", {"target_ref": node["ref"]})
    route = env.route_action(action, obs)
    client.record = {"ok": False, "code": "preparation_failed", "evidence": {"continuation": {"state": "stop"}}}
    monkeypatch.setattr(env, "_build_observation", lambda: pytest.fail("Observed after HCU stop"))
    result = env.execute_routed(action, route)
    assert result.done and result.info["status"] == "uncertain" and result.observation.screenshot is None
    assert env.observe().screenshot is None and env.step(Action("wait", {"seconds": 1})).done
    assert len(client.sent) == 1


def test_stale_then_partial_delivery_stops_without_reobserving_or_replaying(desktop, monkeypatch):
    env, _, client = desktop

    def request(obs):
        control = next(n for n in obs.context[0]['nodes'] if n['role'] == 'textbox')
        return Action('type', {'target_ref': control['ref'], 'text': 'test text'})

    old = env.observe()
    action = request(old)
    route = env.route_action(action, old)
    env.observe()  # Capture generation changes while the old action is queued.
    stale = env.execute_routed(action, route)
    assert stale.info['dispatched'] is False and not client.sent

    action = request(stale.observation)
    route = env.route_action(action, stale.observation)
    client.record = {'ok': False, 'code': 'effect_unknown', 'input_emitted': True,
                     'completed': 0, 'evidence': {'continuation': {'state': 'stop'}}}
    monkeypatch.setattr(env, '_build_observation', lambda: pytest.fail('Observed after partial native input'))
    pipe = Pipeline.__new__(Pipeline)
    pipe.env = env
    pipe._active_actions = ['type']
    task = SimpleNamespace(obs=stale.observation, route=route, task='Use Notepad to type text', queue=None)
    result = pipe.execute(action, task)
    assert result.info['dispatched'] is True and result.info['status'] == 'uncertain'
    assert result.info['continuation']['state'] == 'stop' and result.obs.screenshot is None
    assert pipe.execute(action, task).done
    assert env.observe().screenshot is None and len(client.sent) == 1


def test_regular_browser_excludes_native_dialog_and_electron(desktop):
    env, state, _ = desktop
    window = dict(WINDOW, class_name="Chrome_WidgetWin_1", process_name="chrome.exe")
    assert browser_window(window)
    assert not browser_window(dict(window, class_name="#32770"))
    assert not browser_window(dict(window, process_name="Code.exe"))
    state["applications"] = [window]
    obs = env.observe()
    assert obs.info["desktop_browser_foreground"] and not obs.context
    result = env.step(Action("press", {"key": "enter"}))
    assert result.info["error_code"] == "browser_task_out_of_scope" and not result.info["dispatched"]


def test_shell_picker_is_readable_but_uses_real_keyboard_for_filename(desktop, monkeypatch):
    env, state, client = desktop
    window = state["applications"][0]
    window.update(title="另存为", class_name="#32770")
    monkeypatch.setattr(client, "apps", lambda: {"windows":[{"ref":"w1","app":"Notepad","title":"另存为"}]})
    monkeypatch.setattr(client, "observe", lambda window_ref: {"app":{"pid":98},"window":{"ref":"w1","title":"另存为"},
                       "snapshot_id":"s1","elements":[{"role":"textbox","ref":"r1","name":"文件名:"}]})
    obs = env.observe()
    block = obs.context[0]
    assert obs.info["desktop_file_dialog"] and block["read_only"] and not block["nodes"][0]["direct"]
    action = Action("type", {"target":"文件名", "text":"new.txt", "target_ref":block["nodes"][0]["ref"]})
    assert env.route_action(action, obs)["channel"] == "reobserve"
    assert not env.execute_routed(action, {"channel":"desktop_ax"}).info["dispatched"]
    assert not client.sent


def test_created_file_contract_blocks_overwrite_confirmation(desktop, monkeypatch):
    env, state, _ = desktop
    state["applications"][0].update(title="确认另存为", class_name="#32770")
    env.set_desktop_file_intent("保存报告", [{"kind":"desktop_file","change":"created","path":"report.txt"}])
    obs = env.observe()
    assert obs.info["desktop_overwrite_protected"]
    assert env.step(Action("press", {"key":"Enter"})).info["error_code"] == "unexpected_file_overwrite"
    from hybrid_agent.env import StepResult
    monkeypatch.setattr(env, "_step_desktop", lambda action: StepResult(obs, info={"status":"allowed","key":action.args["key"]}))
    assert env.step(Action("press", {"key":"Esc"})).info["key"] == "Esc"


def test_unsupported_native_operation_is_retired_after_safe_rejection(desktop):
    env, _, client = desktop
    obs = env.observe()
    textbox = next(n for n in obs.context[0]["nodes"] if n["role"] == "textbox")
    action = Action("type", {"target_ref":textbox["ref"],"text":"value","overwrite":True})
    client.record = {"ok":False,"code":"runtime_error","evidence":{"continuation":{"state":"observe_required","retry_same_request":False}}}
    result = env.execute_routed(action, env.route_action(action, obs))
    after = next(n for n in result.observation.context[0]["nodes"] if n["role"] == "textbox")
    assert result.info["status"] == "rejected"
    assert "set_value" not in after["actions"] and "type" in after["actions"]


def test_prompt_exposure_is_checked_again_at_native_dispatch(desktop):
    env, _, client = desktop
    obs = env.observe()
    node = next(n for n in obs.context[0]["nodes"] if n["role"] == "button")
    action = Action("click", {"target_ref": node["ref"]})
    route = env.route_action(action, obs)
    obs.info["exposed_refs"] = []
    assert env.route_action(action, obs)["error_code"] == "unexposed_desktop_ref"
    assert env.execute_routed(action, route).info["error_code"] == "unexposed_desktop_ref"
    assert not client.sent


def test_inactive_win32_desktop_supplies_no_image_and_no_input(desktop, monkeypatch):
    env, _, client = desktop
    obs = env.observe()
    target = next(n for n in obs.context[0]["nodes"] if n["role"] == "button")
    action = Action("click", {"target_ref":target["ref"]})
    route = env.route_action(action, obs)
    monkeypatch.setattr(env, "_desktop_is_input", lambda: False)
    monkeypatch.setattr(env, "_take_screenshot", lambda: pytest.fail("inactive desktop was captured"))
    assert not env.execute_routed(action, route).info["dispatched"]
    assert env.observe().screenshot is None
    assert env.step(Action("hotkey", {"keys":["win","e"]})).info["error_code"] == "input_desktop_inactive"
    assert not client.sent


def test_overwrite_guard_respects_distinct_existing_and_new_targets(desktop):
    env, _, _ = desktop
    env.set_desktop_file_intent("更新旧文件并创建报告", [
        {"kind":"desktop_file", "change":"modified", "path":r"C:\desk\old.txt"},
        {"kind":"desktop_file", "change":"created", "path":r"C:\desk\new.txt"}])
    assert not env._overwrite_protected("old.txt already exists.")
    assert env._overwrite_protected("new.txt already exists.")
    env.set_desktop_file_intent("Create a new file and do not overwrite any existing file.")
    assert env._overwrite_protected("old.txt already exists.")
    env.set_desktop_file_intent("不要新建文件，更新原来的文件")
    assert not env._overwrite_protected("old.txt 已存在。")
