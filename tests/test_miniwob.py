"""Browser integration checks for the MiniWoB++ adapter; no model server needed."""
import json
import unittest
from unittest.mock import patch

from hybrid_agent.actions import Action
from hybrid_agent.adapters.miniwob_env import MiniWoBEnvironment
from hybrid_agent.agent import Agent
from hybrid_agent.config import ModelConfig
from hybrid_agent.decision_context import action_scope, system_sections
from hybrid_agent.model import ChatModel


class MiniWoBTests(unittest.TestCase):
    def open_episode(self, mode="hybrid"):
        env = MiniWoBEnvironment("miniwob/click-test-2-v1", 0, observation_mode=mode)
        self.addCleanup(env.close)
        instruction = env.start()
        return env, instruction, env.reset(instruction)

    def test_agent_uses_prepared_episode_and_official_terminal_score(self):
        env, instruction, obs = self.open_episode()
        target = next(n for n in obs.context[0]["nodes"] if n["name"] == "ONE" and n["direct"])
        plan = {"steps": [{"goal": instruction, "expected_evidence": "Correct button clicked",
                           "verification": [{"kind": "visual"}]}],
                "final_check": "Correct button clicked"}
        decision = {"next_action": {"type": "click", "target": "ONE", "target_ref": target["ref"]},
                    "summary": "Click the ONE button."}
        model = ChatModel(ModelConfig())
        agent = Agent(model, grounding=None, env=env, verifier_model=model, max_steps=2)
        with patch.object(model, "prepare_client"), patch.object(model, "chat", side_effect=[
            json.dumps(plan), json.dumps(decision)
        ]) as chat:
            result = agent.run(instruction, completion_spec={"checks": []})
        self.assertEqual(chat.call_count, 2)
        self.assertTrue(result.success)
        self.assertEqual(result.reward_source, "environment_evaluator")
        self.assertEqual(env.outcome()["binary_reward"], 1)
        self.assertEqual(env.outcome()["environment_steps"], 1)

    def test_stale_route_and_wrong_button(self):
        env, _, obs = self.open_episode()
        target = next(n for n in obs.context[0]["nodes"] if n["name"] == "TWO" and n["direct"])
        click = Action("click", {"target": "TWO", "target_ref": target["ref"]})
        old_route = env.route_action(click, obs)
        current = env.observe()
        rejected = env.execute_routed(click, old_route)
        self.assertFalse(rejected.info["dispatched"])
        self.assertEqual(env.outcome()["environment_steps"], 0)
        self.assertTrue(env.execute_routed(click, env.route_action(click, current)).done)
        self.assertEqual(env.outcome()["binary_reward"], -1)
        self.assertEqual(env.evaluate(), 0)

    def test_gui_observation_and_action_menu(self):
        env, _, obs = self.open_episode("gui")
        self.assertFalse(obs.context)
        self.assertIsNotNone(obs.screenshot)
        self.assertEqual(env.route_action(Action("click", {"target_ref": "4"}), obs)["channel"], "reobserve")
        actions = action_scope(None, obs)
        self.assertIn("click", actions)
        self.assertNotIn("run_browser_js", actions)
        self.assertNotIn("skills", dict(system_sections(actions, obs)))


if __name__ == "__main__":
    unittest.main()
