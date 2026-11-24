import json
import sys
import unittest
from unittest.mock import Mock

from PIL import Image

from hybrid_agent.model.terra_verification import (
    MODEL, TerraAPI, TerraGrounding, TerraJudge, TerraReply, _turn_input,
)


class TerraVerificationTests(unittest.TestCase):
    def test_system_one_runs_real_parser_and_keeps_deadline(self):
        api = Mock()
        questions = {"mode": {"type": "choice", "instructions": "Choose",
                              "criteria": {"gui": "visual", "desktop_ax": "accessible"}}}
        answer = {"answers": {"mode": {"type": "choice", "choice": "gui",
                                      "probabilities": {"gui": 0.8, "desktop_ax": 0.2}}}}
        api.generate.return_value = TerraReply(json.dumps(answer), MODEL, "t1", "r1", 15)
        judge = TerraJudge(api)
        self.addCleanup(judge.close)
        frame = Image.new("RGB", (7, 5))
        result = judge.decide({"foreground": "test"}, questions, [frame], remaining_s=37)
        self.assertEqual(result["answers"]["mode"]["label"], "gui")
        self.assertEqual(judge.last_call["response_id"], "r1")
        args, kwargs = api.generate.call_args
        self.assertEqual(kwargs["timeout"], 37)
        self.assertEqual(args[0][1]["content"][1]["type"], "image_url")
        answer["answers"]["mode"]["probabilities"] = {"gui": 1.0}
        api.generate.return_value = TerraReply(json.dumps(answer), MODEL, "t2", "r2", 16)
        self.assertEqual(judge.decide({}, questions)["status"], "unknown")

    def test_grounding_fails_closed_and_uses_original_dimensions(self):
        api = Mock()
        grounder = TerraGrounding(api)
        frame = Image.new("RGB", (41, 29))
        for text, expected in [('{"x":40,"y":28}', (40, 28)),
                               ('{"x":41,"y":28}', (-1, -1)), ('unparseable', (-1, -1)),
                               ('{"x":true,"y":1}', (-1, -1))]:
            api.generate.return_value = TerraReply(text, MODEL, "t", "r", 1)
            result = grounder.locate(frame, "button")
            self.assertEqual((result.x, result.y), expected)
            self.assertIn("41 x 29", api.generate.call_args.args[0][0]["content"][0]["text"])

    def test_input_preserves_order_and_rejects_external_images(self):
        instructions, items = _turn_input([{"role": "system", "content": "rules"}, {"role": "user",
            "content": [{"type": "text", "text": "before"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,Yg=="}},
                        {"type": "text", "text": "after"}]}])
        self.assertEqual(instructions, "rules")
        self.assertEqual([i["type"] for i in items], ["text", "image", "text"])
        with self.assertRaises(ValueError):
            _turn_input([{"role": "user", "content": [{"type": "image_url", "image_url": {
                "url": "https://example.org/private.png"}}]}])

    def test_receive_early_turn_events_and_refuse_model_rerouting(self):
        script = '''
import json, sys
def send(data): print(json.dumps(data), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method, id = msg["method"], msg.get("id")
    if method == "initialize":
        send({"id": id, "result": {}})
    if method == "thread/start":
        send({"id": id, "result": {"thread": {"id": "t"}, "model": "gpt-6.1-terra"}})
    if method == "turn/start":
        if sys.argv[1] == "reroute":
            send({"method": "model/rerouted", "params": {"threadId": "t"}})
        send({"method": "item/completed", "params": {"threadId": "t", "turnId": "r", "item": {
            "type": "agentMessage", "text": "answer", "phase": "final_answer"}}})
        send({"id": id, "result": {"turn": {"id": "r"}}})
        send({"method": "turn/completed", "params": {"threadId": "t", "turn": {
            "id": "r", "status": "completed"}}})
'''
        messages = [{"role": "user", "content": "hello"}]
        client = TerraAPI([sys.executable, "-u", "-c", script, "normal"])
        reply = client.generate(messages, timeout=5)
        self.assertEqual((reply.text, reply.thread_id, reply.turn_id), ("answer", "t", "r"))
        client = TerraAPI([sys.executable, "-u", "-c", script, "reroute"])
        with self.assertRaisesRegex(RuntimeError, "rerouted"):
            client.generate(messages, timeout=5)
