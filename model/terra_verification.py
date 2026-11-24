"""Run the three desktop-agent roles against GPT-6.1 Terra through Codex app-server.

Each generation owns one ephemeral thread and process. No conversation or tool
access is shared between the policy, visual grounder and System One reviewer.
"""
import argparse
import json
import math
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, replace

import httpx

from ..config import ModelConfig
from .decision_judge import DecisionJudgeConfig
from .decision_model import ChatModel, encode_image
from .grounding import Grounding, GroundingResult, screenshot_size
from .judge_selection import RuntimeDecisionJudge


MODEL = "gpt-6.1-terra"
# HTTPX routes these requests exclusively to our in-process transport.
SYSTEM_ONE_URL = "https://terra-verification.invalid/v1"
ENDPOINT_INSTRUCTIONS = (
    "You are a stateless inference endpoint for an external Windows agent. The application has its "
    "own executor for GUI input, writing files and CLI operations. Your job is to return the "
    "requested JSON, text or pixel coordinates. In particular a policy response must select one of "
    "the external executor actions supplied in the request. Selecting a write, click or keyboard "
    "action is allowed: the application will validate and dispatch the action after receiving your "
    "JSON. Your own Codex process and its read-only sandbox are separate from the target computer. "
    "Use only the supplied observations, action menu and executor receipts for claims about the "
    "target. Never infer that the target filesystem is read-only or a GUI is unavailable from the "
    "model process's sandbox. Do not directly invoke Codex tools, touch local files or browse. "
    "Do not perform a requested target action yourself. Output the policy action for the external "
    "executor, or answer the supplied verification/grounding question, as instructed."
)


@dataclass
class TerraReply:
    text: str
    model: str
    thread_id: str
    turn_id: str
    elapsed_ms: float

    def record(self):
        return {"backend": "codex_app_server", "model": self.model,
                "response_model": self.model, "thread_id": self.thread_id,
                "response_id": self.turn_id, "latency_ms": self.elapsed_ms}


def _turn_input(messages):
    instructions, inputs = [], []
    for message in messages:
        message = ChatModel._to_message_dict(message)
        role, content = message["role"], message["content"]
        if role in {"system", "developer"}:
            if not isinstance(content, str):
                raise ValueError("Terra system instructions must be text")
            instructions.append(content)
            continue
        if role == "assistant":
            inputs.append({"type": "text", "text": "Previous assistant response:"})
        parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
        for part in parts:
            if part.get("type") == "text":
                inputs.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "image_url":
                url = part["image_url"]["url"]
                if not url.startswith("data:image/") or ";base64," not in url:
                    raise ValueError("Terra verification requires inline image data")
                inputs.append({"type": "image", "url": url})
            else:
                raise ValueError(f"Unsupported Terra input: {part.get('type')}")
    return "\n\n".join(instructions), inputs


class TerraAPI:
    """JSONL app-server client. A single absolute deadline covers startup and generation."""

    def __init__(self, command=None):
        self.command = command

    def generate(self, messages, *, timeout, on_delta=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise TimeoutError("Terra call budget expired")
        instructions, inputs = _turn_input(messages)
        command = self.command
        if not command:
            executable = shutil.which("codex")
            if not executable:
                raise RuntimeError("Codex app-server executable is unavailable")
            command = [executable, "app-server", "--listen", "stdio://", "-c", "mcp_servers={}",
                       "-c", "project_doc_max_bytes=0",
                       "-c", 'web_search="disabled"', "--disable", "shell_tool",
                       "--disable", "apps", "--disable", "plugins", "--disable", "browser_use",
                       "--disable", "multi_agent"]
        started = time.monotonic()
        deadline = started + timeout
        with tempfile.TemporaryDirectory(prefix="terra-agent-") as workspace:
            # Binary pipes accept UTF-8 even when Windows uses a legacy locale.
            proc = subprocess.Popen(command, cwd=workspace, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            events = queue.Queue()

            def read_events():
                try:
                    for line in proc.stdout:
                        events.put(json.loads(line))
                except (OSError, ValueError) as exc:
                    events.put(exc)
                finally:
                    events.put(None)

            reader = threading.Thread(target=read_events, daemon=True)
            reader.start()

            def send(message):
                proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                proc.stdin.flush()

            def receive():
                try:
                    event = events.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty:
                    raise TimeoutError("Terra API exceeded the call deadline") from None
                if event is None or isinstance(event, Exception):
                    raise RuntimeError("Terra app-server closed its JSONL stream")
                if "id" in event and "method" in event:
                    raise RuntimeError("Terra verification rejected server tool or approval request")
                if event.get("method") == "model/rerouted":
                    raise RuntimeError("Terra model rerouted; verification identity is unconfirmed")
                return event

            pending = []

            def request(number, method, params):
                send({"id": number, "method": method, "params": params})
                while True:
                    message = receive()
                    if message.get("id") != number:
                        pending.append(message)
                        continue
                    if "error" in message:
                        error = message["error"]
                        raise RuntimeError(f"Terra {method}: {str(error.get('message','RPC error'))[:320]}")
                    return message["result"]

            try:
                request(1, "initialize", {"clientInfo": {"name": "hybrid_agent_verification",
                        "version": "1"}, "capabilities": {"experimentalApi": True}})
                send({"method": "initialized"})
                thread = request(2, "thread/start", {"model": MODEL, "sandbox": "read-only",
                    "approvalPolicy": "never", "ephemeral": True, "cwd": workspace,
                    "baseInstructions": ENDPOINT_INSTRUCTIONS,
                    "developerInstructions": ENDPOINT_INSTRUCTIONS + "\n\n" + instructions})
                if thread.get("model") != MODEL:
                    raise RuntimeError(f"Unexpected Terra deployment: {thread.get('model')!r}")
                thread_id = thread["thread"]["id"]
                turn = request(3, "turn/start", {"threadId": thread_id, "model": MODEL,
                               "effort": "low", "input": inputs})["turn"]
                turn_id, answer, streamed = turn["id"], "", False
                final_items = set()
                while True:
                    message = pending.pop(0) if pending else receive()
                    method, params = message.get("method"), message.get("params") or {}
                    if params.get("threadId") != thread_id:
                        continue
                    if params.get("turnId", turn_id) != turn_id:
                        continue
                    item = params.get("item") or {}
                    if method in {"item/started", "item/completed"} and item.get("type") not in {
                            "userMessage", "agentMessage", "reasoning"}:
                        raise RuntimeError(f"Terra verification rejected model tool: {item.get('type')}")
                    if method == "item/started" and item.get("phase") == "final_answer":
                        final_items.add(item.get("id"))
                    if method == "item/agentMessage/delta" and on_delta and params.get("itemId") in final_items:
                        on_delta(params["delta"])
                        streamed = True
                    if method == "item/completed" and item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                        answer = item.get("text", "")
                    if method == "turn/completed" and params["turn"]["id"] == turn_id:
                        result = params["turn"]
                        if result.get("status") != "completed" or not answer.strip():
                            reason = (result.get("error") or {}).get("message", result.get("status"))
                            raise RuntimeError(f"Terra returned no completed answer: {str(reason)[:320]}")
                        if on_delta and not streamed:
                            on_delta(answer)
                        return TerraReply(answer, thread["model"], thread_id, turn_id,
                                          round((time.monotonic() - started) * 1000, 2))
            finally:
                # Never leave an in-flight generation running after timeout or cancellation.
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                proc.stdin.close()
                reader.join(timeout=1)
                proc.stdout.close()


class TerraChat(ChatModel):
    verification_backend = "terra"

    def __init__(self, api, role="decision"):
        super().__init__(ModelConfig(name=MODEL, url=SYSTEM_ONE_URL, timeout=120))
        self.api, self.role = api, role

    def prepare_client(self):
        # App-server authenticates with the operator's existing Codex session.
        pass

    def independent_reviewer(self):
        return TerraChat(self.api, "verifier")

    def _generate(self, messages, timeout, on_delta=None):
        self.last_call = {"actor_role": self.role, "requested_model": MODEL,
                          "messages": messages, "policy_trainable": False}
        try:
            reply = self.api.generate(messages, timeout=timeout, on_delta=on_delta)
        except Exception as exc:
            self.last_call["error"] = str(exc)
            raise
        self.last_call.update(reply.record(), response=reply.text, provenance="sampled")
        return reply.text

    def chat(self, messages, retries=2, json_mode=False):
        # The caller validates action/plan/verification JSON with its own schema.
        if json_mode:
            messages = [{"role": "system", "content": "Return exactly one valid JSON object."}, *messages]
        return self._generate(messages, self.cfg.timeout)

    def chat_stream(self, messages, on_delta, timeout=None):
        return self._generate(messages, min(timeout, self.cfg.timeout) if timeout is not None
                              else self.cfg.timeout, on_delta)


class TerraGrounding(Grounding):
    model = MODEL

    def __init__(self, api):
        self.api = api
        self.last_calls = []

    def locate(self, screenshot, target):
        self.last_calls = []
        size, url = screenshot_size(screenshot), encode_image(screenshot)
        if not size or not url:
            return GroundingResult(-1, -1, 0)
        width, height = size
        prompt = (f"Locate this visible target in the supplied {width} x {height} screenshot: {target}\n"
                  'Return only JSON {"x":integer,"y":integer} using pixels from the top left. '
                  'Use (-1,-1) if the target cannot be uniquely identified.')
        call = {"actor_role": "grounding", "target": target, "requested_model": MODEL,
                "image_size": list(size), "policy_trainable": False}
        self.last_calls.append(call)
        try:
            reply = self.api.generate([{"role": "user", "content": [{"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}}]}], timeout=120)
            call.update(reply.record(), response=reply.text, provenance="sampled")
            point = json.loads(reply.text)
            x, y = point["x"], point["y"]
            if type(x) is int and type(y) is int and 0 <= x < width and 0 <= y < height:
                return GroundingResult(x, y, 1.0)
            raise ValueError("Grounding point outside the input image")
        except (RuntimeError, TimeoutError, ValueError, OSError, TypeError, KeyError) as exc:
            call["error"] = str(exc)
            return GroundingResult(-1, -1, 0)


class TerraSystemOneTransport(httpx.BaseTransport):
    def __init__(self, api):
        self.api, self.last_record = api, {}

    def handle_request(self, request):
        self.last_record = {}
        if request.method != "POST" or str(request.url) != SYSTEM_ONE_URL + "/systemone":
            raise ValueError("Unexpected Terra verification route")
        payload = json.loads(request.content)
        prompt = ("Answer the System One questions using only the supplied evidence. Return JSON "
                  '{"answers":{"<question id>":{"type":"choice","probabilities":{"<option>":0.0}}}}. '
                  "For every choice provide all and only the supplied option names, finite probabilities "
                  "summing to 1. For noul return type and noul in [0,1]; for score return the supplied "
                  "legend, probabilities for the numeric ranks and the probability-weighted score. "
                  "Do not invent evidence. Images follow in the supplied order.")
        content = [{"type": "text", "text": json.dumps(
            {"state": payload["state"], "questions": payload["questions"]}, ensure_ascii=False)}]
        content.extend({"type": "image_url", "image_url": {"url": url}} for url in payload.get("images", []))
        reply = self.api.generate([{"role": "system", "content": prompt},
                                   {"role": "user", "content": content}],
                                  timeout=request.extensions["timeout"]["read"])
        self.last_record = reply.record()
        body = json.loads(reply.text)
        if not isinstance(body, dict) or not isinstance(body.get("answers"), dict) or set(body["answers"]) != set(payload["questions"]):
            raise ValueError("Terra System One question ids differ from request")
        return httpx.Response(200, json={"answers": body["answers"], "model": reply.model,
                                        "response_id": reply.turn_id})


class TerraJudge(RuntimeDecisionJudge):
    def __init__(self, api, config=None, selection_policy="maximum_probability"):
        self.api = api
        self.selection_policy = selection_policy
        self.adapter = TerraSystemOneTransport(api)
        cfg = replace(config or DecisionJudgeConfig(), url=SYSTEM_ONE_URL, model=MODEL,
                      revision="", api_key="")
        super().__init__(cfg, transport=self.adapter, selection_policy=selection_policy)
        self.mode_timeout_s = self.transition_timeout_s = cfg.timeout

    def independent_judge(self):
        return TerraJudge(self.api, self.config, self.selection_policy)

    def decide(self, *args, **kwargs):
        self.adapter.last_record = {}
        result = super().decide(*args, **kwargs)
        self.last_call.update(self.adapter.last_record, verification_backend="terra")
        return result


def verify_roles(api):
    """One real, independently checked response per role using only a synthetic image."""
    import secrets
    from PIL import Image, ImageDraw

    frame = Image.new("RGB", (400, 240), "white")
    cells = [(12, 12, 188, 108), (212, 12, 388, 108),
             (12, 132, 188, 228), (212, 132, 388, 228)]
    colors = ["red", "blue", "green", "yellow"]
    secrets.SystemRandom().shuffle(colors)
    color = secrets.choice(colors)
    box = cells[colors.index(color)]
    for cell, fill in zip(cells, colors):
        ImageDraw.Draw(frame).rectangle(cell, fill=fill)
    model = TerraChat(api)
    request = 'Choose one click using JSON {"next_action":{"type":"click","target":"<color>"},' \
              '"summary":"short text"}. Click the ' + color + ' rectangle. Write the color name alone in target.'
    action = json.loads(model.chat([model.build_vision_message(request, [("Current frame", frame)])],
                                   json_mode=True))["next_action"]
    if action.get("type") != "click" or action.get("target") != color:
        raise RuntimeError("Terra policy smoke failed to select the requested target")
    grounder = TerraGrounding(api)
    point = grounder.locate(frame, color + " rectangle")
    if not (box[0] <= point.x <= box[2] and box[1] <= point.y <= box[3]):
        raise RuntimeError(f"Terra grounding smoke missed its target: {(point.x, point.y)}")
    judge = TerraJudge(api)
    try:
        result = judge.decide({"target_color": color}, {"vertical": {"type": "choice",
            "instructions": "Which half of the image contains the target color?",
            "criteria": {"top": "upper half", "bottom": "lower half", "unknown": "cannot tell"}}}, [frame])
        expected = "top" if box[1] < frame.height / 2 else "bottom"
        if result["status"] != "answered" or result["answers"]["vertical"]["label"] != expected:
            raise RuntimeError(f"Terra Jev smoke failed: {result.get('reason') or result['status']}")
        records = {"decision": model.last_call, "grounding": grounder.last_calls[-1],
                   "jev": judge.last_call}
        return {"model": MODEL, "roles": {role: {"response_model": record["response_model"],
                    "thread_id": record["thread_id"], "response_id": record["response_id"]}
                for role, record in records.items()}, "grounding": [point.x, point.y], "vertical": expected}
    finally:
        judge.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check all three Terra roles with an in-memory color image")
    parser.add_argument("--smoke", action="store_true", required=True)
    parser.parse_args()
    print(json.dumps(verify_roles(TerraAPI()), ensure_ascii=False))
