"""Run a seeded MiniWoB++ evaluation with the project's Agent."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
import uuid

from ..config import Config, resolve_secret


SMOKE_TASK = "miniwob/click-test-2-v1"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "miniwob.yaml"


def task_ids():
    import gymnasium as gym
    import miniwob

    gym.register_envs(miniwob)
    return sorted(env_id for env_id in gym.registry if env_id.startswith("miniwob/"))


def select_tasks(names, catalog):
    chosen = []
    for name in names:
        full = name if name.startswith("miniwob/") else "miniwob/" + name
        if full not in catalog:
            full += "-v1"
        if full not in catalog:
            raise ValueError(f"Unknown task {name!r}. Use --list-tasks for registered IDs.")
        if full in chosen:
            raise ValueError(f"Duplicate task {full!r}.")
        chosen.append(full)
    return chosen


def smoke_test(wait_ms):
    from ..actions import Action
    from .miniwob_env import MiniWoBEnvironment

    env = MiniWoBEnvironment(SMOKE_TASK, 0, wait_ms=wait_ms)
    try:
        instruction = env.start()
        obs = env.reset(instruction)
        button = next(node for node in obs.context[0]["nodes"]
                      if node.get("name") == "ONE" and node.get("direct"))
        action = Action("click", {"target": "ONE", "target_ref": button["ref"]})
        result = env.execute_routed(action, env.route_action(action, obs))
        outcome = env.outcome()
        print(json.dumps({"task_id": SMOKE_TASK, "seed": 0, "instruction": instruction,
                          **outcome}, ensure_ascii=False))
        if not (result.done and outcome["terminated"] and outcome["binary_reward"] == 1):
            raise RuntimeError("MiniWoB++ smoke test did not receive terminal reward +1.")
    finally:
        env.close()


def load_config(path, max_steps):
    cfg = Config.load(path)
    for model in (cfg.model, cfg.grounding_model, cfg.planner_model):
        if model is None:
            continue
        model.name = resolve_secret(model.name)
        model.url = resolve_secret(model.url)
        model.api_key = resolve_secret(model.api_key)
        url = urlsplit(model.url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Model endpoints must be HTTP(S) base URLs without embedded credentials, query or fragment.")
    if cfg.agent.decision_judge.get("api_key"):
        cfg.agent.decision_judge["api_key"] = resolve_secret(cfg.agent.decision_judge["api_key"])
    if max_steps is not None:
        cfg.agent.max_steps = max_steps
    if type(cfg.agent.max_steps) is not int or cfg.agent.max_steps < 1:
        raise ValueError("max_steps must be a positive integer.")
    if cfg.completion is not None or cfg.private_completion is not None:
        raise ValueError("MiniWoB++ uses its own evaluator. Remove completion and private_completion from this config.")
    return cfg


def public_config(cfg):
    # Keep only relevant settings. Credentials are excluded even from the compatibility hash.
    config = {"agent": asdict(cfg.agent), "context": asdict(cfg.context)}
    for name in ("model", "planner_model", "grounding_model"):
        model = getattr(cfg, name)
        if model is not None:
            config[name] = {k: v for k, v in asdict(model).items() if k != "api_key"}
    judge = config["agent"]["decision_judge"]
    judge.pop("api_key", None)
    return config


def make_manifest(args, tasks, cfg):
    from ..knowledge_base import load_knowledge

    config = public_config(cfg)
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    source = hashlib.sha256()
    package = Path(__file__).resolve().parents[1]
    for path in sorted([*package.rglob("*.py"), package / "config" / "action.yaml",
                        package / "config" / "skills.yaml"]):
        source.update(path.relative_to(package).as_posix().encode())
        source.update(path.read_bytes())
    return {
        "schema": 1, "tasks": tasks, "seeds": args.seeds, "data_mode": "test",
        "reward_processor": "get_binary_reward", "timer": "task_default",
        "observation_mode": args.observation_mode, "wait_ms": args.wait_ms,
        "decision_model": cfg.model.name, "grounding_model": cfg.grounding_model.name,
        "planner_model": (cfg.planner_model or cfg.model).name, "max_steps": cfg.agent.max_steps,
        "versions": {name: version(name) for name in ("miniwob", "gymnasium", "selenium")},
        "config_sha256": digest, "source_sha256": source.hexdigest(),
        "knowledge_sha256": load_knowledge()["revision"],
    }


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    temporary.replace(path)


@contextmanager
def batch_lock(root):
    # Advisory OS locks are released on process exit, including interrupted runs.
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".batch.lock").open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(f"Another runner is using {root}.") from exc
        yield


def prepare_batch(root, manifest, resume):
    path = root / "manifest.json"
    if path.exists():
        if not resume:
            raise ValueError(f"{root} contains a batch. Use --resume or a new output directory.")
        if json.loads(path.read_text(encoding="utf-8")) != manifest:
            raise ValueError("Existing manifest differs in tasks, seeds, source, model or settings. Use a new output directory.")
    else:
        if any(child.name not in (".batch.lock", "manifest.tmp") for child in root.iterdir()):
            raise ValueError(f"Refusing to mix a new batch with other files in {root}.")
        write_json(path, manifest)


def make_agent(cfg, env):
    from ..agent import Agent
    from ..model import build_decision_model, build_grounding_model

    model = build_decision_model(cfg.model)
    planner = build_decision_model(cfg.planner_model) if cfg.planner_model else None
    return Agent(decision_model=model, grounding=build_grounding_model(cfg.grounding_model),
                 env=env, planner_model=planner, context_config=cfg.context,
                 completion_spec={"checks": []}, **asdict(cfg.agent))


def failed_service(result):
    # The pipeline catches transport exceptions for retries. A rejected JSON plan is
    # a policy failure; exhausted service calls should remain retryable on --resume.
    calls = result.planning_calls or []
    if calls and all(call.get("error_type") for call in calls):
        return "planner_service"
    decisions = [step for step in result.trajectory if step.get("decision_calls")]
    if decisions and decisions[-1].get("execution_info", {}).get("error_code") == "model_transport":
        return "decision_service"
    ground_calls = [event["call"] for event in result.events
                    if event.get("event_type") == "model_call" and event.get("actor_role") == "grounder"]
    if ground_calls and all(call.get("error_type") for call in ground_calls):
        return "grounding_service"
    return None


def error_message(exc, secrets):
    message = str(exc)
    for secret in secrets:
        message = message.replace(secret, "[redacted]")
    return f"{type(exc).__name__}: {message[:1000]}"


def save_trajectory(directory, row, result, secrets):
    from ..trajectory import build_trajectory, dump_trajectory

    # Upstream providers can echo a rejected API key in an error body. Strip it
    # from archived text, including the planner's error feedback to itself.
    def scrub(value, error_text=False):
        if isinstance(value, str) and not value.startswith("data:image/"):
            for secret in secrets:
                if len(secret) >= 12 or error_text or "api key" in value.lower() or "api_key" in value.lower():
                    value = value.replace(secret, "[redacted]")
        elif isinstance(value, dict):
            return {k: scrub(v, error_text or k in ("error", "validation_error", "reason"))
                    for k, v in value.items() if k != "api_key"}
        elif isinstance(value, (tuple, list)):
            return [scrub(item, error_text) for item in value]
        return value

    trajectory = build_trajectory(task_id=row["task_id"], domain="miniwob", instruction=row["instruction"],
                                  result=result)
    trajectory["benchmark"] = {k: row[k] for k in ("task_id", "seed", "status", "score", "environment")}
    if secrets:
        trajectory = scrub(trajectory)
    for step, screenshot in result.screenshots.items():
        if screenshot is not None:
            screenshot.save(directory / f"step_{step}.png")
    dump_trajectory(str(directory / "trajectory.json"), trajectory)


def run_episode(directory, cfg, task_id, seed, args):
    from .miniwob_env import MiniWoBEnvironment

    attempt = "attempt-" + uuid.uuid4().hex
    artifacts = directory / attempt
    artifacts.mkdir(parents=True)
    row = {"task_id": task_id, "seed": seed, "attempt": attempt, "status": "error",
           "success": None, "score": None}
    env = None
    agent = None
    started = time.monotonic()
    stage = "initialize"
    secrets = {m.api_key for m in (cfg.model, cfg.planner_model, cfg.grounding_model)
               if m and m.api_key and m.api_key != "EMPTY"}
    judge_key = cfg.agent.decision_judge.get("api_key") or os.getenv("DECISION_JUDGE_API_KEY")
    if judge_key and judge_key != "EMPTY":
        secrets.add(judge_key)
    try:
        env = MiniWoBEnvironment(task_id, seed, observation_mode=args.observation_mode, wait_ms=args.wait_ms)
        agent = make_agent(cfg, env)
        stage = "reset"
        instruction = env.start()
        row["instruction"] = instruction
        stage = "agent"
        result = agent.run(instruction, run_id=f"{task_id.split('/')[1]}-{seed}-{attempt}")
        row.update(agent_steps=result.steps, agent_termination_reason=result.termination_reason)
        stage = "environment_evaluation"
        outcome = env.outcome()
        row["environment"] = outcome
        service_error = failed_service(result)
        if outcome["infrastructure_error"] or (service_error and outcome["binary_reward"] != 1):
            row["error"] = outcome["infrastructure_error"] or service_error
        elif result.termination_reason == "evaluation_error":
            row["error"] = "environment_evaluation"
        else:
            # An agent stopping before a Gym terminal is a failed trial. Its
            # original reward stays null in "environment"; score is pass/fail.
            success = bool(outcome["terminated"] and outcome["binary_reward"] == 1)
            row.update(status="success" if success else "failure", success=success, score=int(success))
        stage = "archive"
        save_trajectory(artifacts, row, result, secrets)
    except Exception as exc:
        row.update(status="error", success=None, score=None,
                   error=f"{stage}: " + error_message(exc, secrets))
        if env is not None and "environment" not in row:
            try:
                row["environment"] = env.outcome()
            except Exception:
                pass  # Preserve the error that ended the attempt.
    finally:
        resources = [env]
        if agent is not None:
            resources.append(agent.model)
            if agent.planner_model is not agent.model:
                resources.append(agent.planner_model)
            grounding_client = getattr(getattr(agent.grounding, "_delegate", agent.grounding), "_client", None)
            resources.append(grounding_client)
        for resource in resources:
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    row.setdefault("cleanup_errors", []).append(error_message(exc, secrets))
    row["elapsed_s"] = round(time.monotonic() - started, 3)
    write_json(artifacts / "result.json", row)
    write_json(directory / "result.json", row)
    return row


def score_counts(rows, planned):
    counts = Counter(row["status"] for row in rows)
    scored = counts["success"] + counts["failure"]
    return {"planned": planned, "scored": scored, "successes": counts["success"],
            "failures": counts["failure"], "errors": counts["error"], "pending": planned - len(rows),
            "success_rate_scored": counts["success"] / scored if scored else None,
            "success_rate": counts["success"] / planned if planned and scored == planned else None}


def run_batch(args, tasks):
    cfg = load_config(args.config, args.max_steps)
    manifest = make_manifest(args, tasks, cfg)
    root = args.output_dir
    planned = len(tasks) * len(args.seeds)
    rows = {}
    with batch_lock(root):
        prepare_batch(root, manifest, args.resume)
        episodes = [(task, seed, root / task / f"seed-{seed}") for task in tasks for seed in args.seeds]
        for task, seed, directory in episodes:
            path = directory / "result.json"
            if path.is_file():
                row = json.loads(path.read_text(encoding="utf-8"))
                if row["task_id"] != task or row["seed"] != seed or row["status"] not in {"error", "success", "failure"}:
                    raise ValueError(f"Invalid result in {path}.")
                rows[task, seed] = row

        def update_summary():
            summary = score_counts(rows.values(), planned)
            summary["by_task"] = {task: score_counts([rows[task, seed] for seed in args.seeds
                                                       if (task, seed) in rows], len(args.seeds)) for task in tasks}
            write_json(root / "summary.json", summary)
            return summary

        summary = update_summary()
        for idx, (task, seed, directory) in enumerate(episodes, start=1):
            if rows.get((task, seed), {}).get("status") in {"success", "failure"}:
                continue
            row = run_episode(directory, cfg, task, seed, args)
            rows[task, seed] = row
            summary = update_summary()
            print(f"[{idx}/{planned}] {task} seed={seed} {row['status']} {row.get('error', '')}", flush=True)
    print(json.dumps({k: v for k, v in summary.items() if k != "by_task"}))
    return 1 if summary["errors"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list-tasks", action="store_true", help="Print the installed MiniWoB++ task IDs.")
    mode.add_argument("--smoke-test", action="store_true", help="Check a deterministic DOM click without calling any model.")
    mode.add_argument("--tasks", nargs="+", help="Task names or full Gymnasium IDs.")
    mode.add_argument("--all-tasks", action="store_true", help="Run every registered MiniWoB++ task.")
    parser.add_argument("--seeds", nargs="+", type=int, default=[0], help="One or more distinct non-negative seeds.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--observation-mode", choices=("hybrid", "gui"), default="hybrid")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--wait-ms", type=int, default=150, help="Official MiniWoB++ wait after each primitive.")
    args = parser.parse_args(argv)
    if min(args.seeds) < 0 or len(set(args.seeds)) != len(args.seeds) or args.wait_ms < 0:
        parser.error("Seeds must be distinct and non-negative; wait-ms must be non-negative.")
    if (args.tasks or args.all_tasks) and args.output_dir is None:
        parser.error("Model runs require --output-dir.")
    try:
        catalog = task_ids()
        if args.list_tasks:
            print("\n".join(catalog))
            return 0
        if args.smoke_test:
            smoke_test(args.wait_ms)
            return 0
        tasks = catalog if args.all_tasks else select_tasks(args.tasks, catalog)
        return run_batch(args, tasks)
    except (ImportError, ValueError, RuntimeError, OSError) as exc:
        print(f"MiniWoB++: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
