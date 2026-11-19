"""One seeded MiniWoB++ v1.1 episode behind the framework's Environment API."""
from __future__ import annotations

from contextlib import contextmanager
import math
from pathlib import Path
import time
from uuid import uuid4

from PIL import Image

from ..env import Environment, Observation, StepResult
from ..runtime.scene import browser_scene
from .miniwob_actions import SUPPORTED_ACTIONS, translate_action, validate_continuation


_ROLES = {
    "a": "link", "button": "button", "input_button": "button", "input_submit": "button",
    "input_checkbox": "checkbox", "input_radio": "radio", "input_range": "slider",
    "input_search": "searchbox", "select": "combobox", "option": "option",
    "textarea": "textbox", "img": "image", "t": "StaticText", "label": "label",
}


def _dom_node(element, viewport):
    tag = element["tag"].lower()
    ref = int(element["ref"])
    parent = int(element["parent"])
    # v1.1 encodes each axis as a one-element numpy array.
    bounds = []
    for key in ("left", "top", "width", "height"):
        value = element[key]
        bounds.append(float(value.item() if hasattr(value, "item") else value))
    left, top, width, height = bounds
    in_viewport = (width > 0 and height > 0 and left < viewport[0] and top < viewport[1]
                   and left + width > 0 and top + height > 0)
    role = _ROLES.get(tag, "textbox" if tag.startswith("input_") else "generic")
    value = str(element.get("value", ""))
    states = {"focused": bool(element["flags"][0])}
    if role in {"checkbox", "radio"}:
        states["checked"] = value.lower() in {"true", "1"}
    name = str(element.get("text", ""))
    if not name and tag in {"input_submit", "input_button"}:
        name = value
    return {
        "ref": str(ref), "parent": str(parent) if parent else None, "tag": tag,
        "role": role, "name": name,
        "value": "[redacted]" if tag == "input_password" and value else value,
        "states": states, "id": str(element.get("id", "")),
        "bounds": bounds, "in_viewport": in_viewport,
        # Positive MiniWoB refs identify real DOM elements, including custom widgets.
        # Negative refs are synthetic text spans that CLICK_ELEMENT cannot address.
        "direct": ref > 0 and tag not in {"html", "body", "option"} and in_viewport,
    }


def _dom_nodes(elements, viewport):
    nodes = [_dom_node(element, viewport) for element in elements]
    by_ref = {node["ref"]: node for node in nodes}
    # A wrapped checkbox and its caption arrive as separate siblings in
    # MiniWoB's serialized DOM. Reconstruct the label from visible text only.
    captions = {}
    for node in nodes:
        parent = by_ref.get(node["parent"])
        if parent and parent["tag"] == "label" and node["name"] and node["tag"] == "t":
            captions.setdefault(parent["ref"], []).append(node["name"])
    for ref, words in captions.items():
        by_ref[ref]["name"] = " ".join(words)
    for node in nodes:
        parent = by_ref.get(node["parent"])
        if not node["name"] and node["tag"].startswith("input_") and parent and parent["tag"] == "label":
            node["name"] = parent["name"]
    return nodes


class MiniWoBEnvironment(Environment):
    """Bind a single browser, task, seed, official timer and binary reward processor."""

    os_name = "linux"
    browser_only = True
    evaluation_available = True
    supported_actions = SUPPORTED_ACTIONS | {"observe_browser", "wait", "request_finish", "fail"}
    # Also tells Pipeline that the initial page is already open for planning.
    browser_start_url = "miniwob://episode"

    def __init__(self, task_id, seed, *, observation_mode="hybrid", wait_ms=150, render_mode=None):
        if observation_mode not in {"hybrid", "gui"}:
            raise ValueError("observation_mode must be hybrid or gui")
        if not isinstance(task_id, str) or not task_id.startswith("miniwob/"):
            raise ValueError("Expected a registered miniwob/... Gymnasium ID")
        if type(seed) is not int or seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        if not math.isfinite(wait_ms) or wait_ms < 0:
            raise ValueError("wait_ms must be finite and nonnegative")
        self.task_id = task_id
        self.seed = seed
        self.observation_mode = observation_mode
        self._gym_options = {"render_mode": render_mode, "wait_ms": wait_ms, "data_mode": "test"}
        self._episode_id = uuid4().hex
        self.initial_state = {"initial_state_id": f"{task_id}/test/{seed}", "task_id": task_id,
                              "seed": seed, "data_mode": "test", "observation_mode": observation_mode}
        self._gym = None
        self._closed = False
        self._utterance = None
        self._raw = None
        self._obs = None
        self._revision = 0
        self._started_at = None
        self._finished_scoring = False
        self._outcome = {"terminated": False, "truncated": False, "binary_reward": None,
                         "raw_reward": None, "env_reward": None, "reason": None,
                         "first_action_ms": None, "environment_steps": 0, "infrastructure_error": None,
                         "browser_version": None, "chromedriver_version": None}

    @contextmanager
    def _backend_call(self):
        # Pipeline normally catches execution/evaluation errors. Retain them so
        # the batch runner can distinguish a lost browser from a failed task.
        if self._outcome["infrastructure_error"]:
            raise RuntimeError(self._outcome["infrastructure_error"])
        try:
            yield
        except Exception as exc:
            self._outcome["infrastructure_error"] = f"{type(exc).__name__}: {exc}"
            raise

    def start(self):
        """Generate the instruction and cache this exact initial observation."""
        if self._closed:
            raise RuntimeError("Episode already closed")
        if self._utterance is None:
            import gymnasium as gym
            import miniwob
            from miniwob.reward import get_binary_reward

            gym.register_envs(miniwob)
            with self._backend_call():
                # Explicit file URI handles spaces and Windows drive letters;
                # FlightWoB uses the upstream HTTP server for its iframe tasks.
                base_url = None
                if not self.task_id.startswith("miniwob/flight."):
                    assets = Path(miniwob.__file__).resolve().parent / "html" / "miniwob"
                    base_url = assets.as_uri() + "/"
                self._gym = gym.make(self.task_id, reward_processor=get_binary_reward, base_url=base_url,
                                     **self._gym_options)
                driver = self._gym.unwrapped.instance.driver
                capabilities = driver.capabilities
                self._outcome["browser_version"] = capabilities.get("browserVersion")
                version = capabilities.get("chrome", {}).get("chromedriverVersion", "")
                self._outcome["chromedriver_version"] = version.partition(" ")[0] or None
                self._started_at = time.monotonic()
                raw, info = self._gym.reset(seed=self.seed, options={"record_screenshots": True})
                self._utterance = raw["utterance"]
                if not isinstance(self._utterance, str) or not self._utterance.strip():
                    raise ValueError("MiniWoB reset returned no task instruction")
                self._set_observation(raw)
                if info["done"]:
                    self._end(info, get_binary_reward(info), terminated=True, truncated=False)
        return self._utterance

    def reset(self, task):
        if task != self.start():
            raise ValueError("Agent instruction does not match this seeded MiniWoB episode")
        return self._obs

    def _set_observation(self, raw):
        self._revision += 1
        self._raw = raw
        img = Image.fromarray(raw["screenshot"])
        snapshot = f"{self._episode_id}/{self._revision}"
        info = {"active_surface": "browser", "screenshot_space": "css_viewport",
                "screenshot_size": list(img.size), "page_id": self._episode_id,
                "document_id": self._episode_id, "snapshot_id": snapshot, "captured_at": time.time(),
                "url": self.browser_start_url, "supported_actions": sorted(self.supported_actions)}
        blocks = []
        if self.observation_mode == "hybrid":
            nodes = _dom_nodes(raw["dom_elements"], img.size)
            blocks.append({"kind": "browser_ax", "snapshot_id": snapshot, "page_id": self._episode_id,
                           "document_id": self._episode_id, "url": self.browser_start_url,
                           "title": "MiniWoB++", "viewport": {"width": img.width, "height": img.height},
                           "nodes": nodes})
        info["scene"] = browser_scene(blocks[0] if blocks else None, img, page_id=self._episode_id,
                                      snapshot_id=snapshot).to_dict()
        self._obs = Observation(screenshot=img, info=info, context=blocks)

    def _end(self, info, reward, *, terminated, truncated):
        # Gym returns an empty (black) observation on termination. Never reuse
        # the previous screenshot or expose scorer metadata to the policy.
        self._outcome.update(terminated=bool(terminated), truncated=bool(truncated),
                             binary_reward=float(reward) if terminated else None,
                             raw_reward=float(info["raw_reward"]) if terminated else None,
                             env_reward=float(info["env_reward"]) if terminated else None,
                             reason=info.get("reason"))
        self._obs = Observation(info={"episode_ended": True, "supported_actions": []})
        self._raw = None

    def episode_finished(self):
        if self._outcome["terminated"] or self._outcome["truncated"]:
            return True
        if self._gym is None or self._closed or self._finished_scoring:
            return False
        # The page timer runs while the model thinks. v1.1 exposes read-only
        # metadata on the Selenium instance; no artificial Gym step is needed.
        with self._backend_call():
            instance = self._gym.unwrapped.instance
            info = instance.get_metadata()
            if info["done"]:
                self._end(info, instance.reward_processor(info), terminated=True, truncated=False)
                return True
        return False

    def observe(self):
        if self._gym is None or self._closed:
            raise RuntimeError("MiniWoB episode is not open")
        if not self.episode_finished() and not self._finished_scoring:
            with self._backend_call():
                raw, _ = self._gym.unwrapped.instance.get_observation(use_cached_fields=True)
                self._set_observation(raw)
                # Account for a timeout during screenshot capture.
                self.episode_finished()
        return self._obs

    def route_action(self, action, obs):
        args = action.args
        ref, element_id = args.get("target_ref"), args.get("element_id")
        if ref is None and not element_id:
            return None
        rejected = {"channel": "reobserve", "error_code": "stale",
                    "reason": "MiniWoB target is absent from the current exposed DOM snapshot"}
        if (self.observation_mode != "hybrid" or not obs or not obs.context or not self._obs
                or obs.info.get("snapshot_id") != self._obs.info.get("snapshot_id")):
            return rejected
        nodes = obs.context[0]["nodes"]
        candidates = [n for n in nodes if n["direct"] and (ref is None or n["ref"] == str(ref))
                      and (not element_id or n["id"] == element_id)]
        if len(candidates) != 1:
            return rejected
        node = candidates[0]
        target = node["ref"]
        if "exposed_refs" in obs.info and target not in obs.info["exposed_refs"]:
            return rejected
        name = node["name"].strip().casefold()
        requested_name = str(args.get("target") or "").strip().casefold()
        if name and requested_name and name != requested_name:
            return {"channel": "reobserve", "error_code": "target_mismatch",
                    "reason": f"target does not match the observed name of MiniWoB ref {target}"}
        return {"channel": "browser_dom", "snapshot_id": obs.info["snapshot_id"],
                "target_ref": target, "page_id": self._episode_id}

    def execute_routed(self, action, route):
        if self.episode_finished():
            return self._result("observed", False)
        current = self.route_action(action, self._obs)
        if not current or current.get("channel") != "browser_dom" or current != route:
            return self._result("rejected", False, error_code="stale", reason="MiniWoB DOM ref has expired")
        return self._dispatch(action, ref=int(route["target_ref"]))

    def step(self, action):
        if self._gym is None or self._closed:
            raise RuntimeError("MiniWoB episode is not open")
        if self.episode_finished():
            return self._result("observed", False)
        if action.action == "observe_browser":
            self.observe()
            return self._result("observed", False)
        if action.action == "wait":
            seconds = action.args.get("seconds")
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 60:
                return self._result("rejected", False, error_code="invalid_wait", reason="wait requires 0–60 seconds")
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not self.episode_finished():
                time.sleep(min(.1, max(0, deadline - time.monotonic())))
            self.observe()
            return self._result("observed", False)
        if action.args.get("target_ref") or action.args.get("element_id"):
            route = self.route_action(action, self._obs)
            return self.execute_routed(action, route)
        binding = action.args.get("observation_metadata")
        if binding is not None and binding.get("snapshot_id") != self._obs.info.get("snapshot_id"):
            return self._result("rejected", False, error_code="stale", reason="MiniWoB screenshot has expired")
        return self._dispatch(action)

    def _dispatch(self, action, ref=None):
        if self._finished_scoring:
            raise RuntimeError("MiniWoB episode has already been scored")
        primitives = []
        try:
            steps = translate_action(action, self._raw, ref=ref)
        except ValueError as exc:
            return self._result("rejected", False, error_code="invalid_action", reason=str(exc))
        for kind, kwargs in steps:
            if self.episode_finished():
                break
            if primitives:
                try:
                    validate_continuation(action, self._raw, ref=ref, primitive=(kind, kwargs))
                except ValueError as exc:
                    return self._result("uncertain", True, error_code="partial_action", reason=str(exc),
                                        primitives=primitives)
            with self._backend_call():
                primitive = self._gym.unwrapped.create_action(kind, **kwargs)
                if self._outcome["first_action_ms"] is None:
                    self._outcome["first_action_ms"] = round((time.monotonic() - self._started_at) * 1000, 2)
                raw, reward, terminated, truncated, info = self._gym.step(primitive)
                primitives.append(kind)
                self._outcome["environment_steps"] += 1
                if terminated or truncated:
                    self._end(info, reward, terminated=terminated, truncated=truncated)
                    break
                self._set_observation(raw)
        return self._result("executed" if primitives else "observed", bool(primitives), primitives=primitives)

    def _result(self, status, dispatched, **details):
        return StepResult(self._obs, reward=self._outcome["binary_reward"] or 0.0,
                          done=self._outcome["terminated"] or self._outcome["truncated"],
                          info={"status": status, "dispatched": dispatched, **details})

    def outcome(self):
        if self._utterance is not None and not self._outcome["infrastructure_error"]:
            self.episode_finished()
        return self._outcome.copy()

    def evaluate(self):
        if self._utterance is None or self._outcome["infrastructure_error"]:
            raise RuntimeError(self._outcome["infrastructure_error"] or "Episode was not initialized")
        self.episode_finished()
        self._finished_scoring = True
        return float(self._outcome["binary_reward"] == 1)

    def run_command(self, command):
        raise NotImplementedError("MiniWoB++ exposes browser interactions only")

    def run_python(self, code):
        raise NotImplementedError("MiniWoB++ exposes browser interactions only")

    def get_page_source(self):
        raise NotImplementedError("Use the configured MiniWoB++ observation")

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._gym is not None:
            self._gym.close()
