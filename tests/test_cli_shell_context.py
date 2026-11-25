import json
import os
import sys
import venv
from types import SimpleNamespace

import pytest
from PIL import Image

from hybrid_agent.adapters.cli_env import CLIEnvironment
from hybrid_agent.config import ModelConfig
from hybrid_agent.env import Observation
from hybrid_agent.model.decision_model import ChatModel
from hybrid_agent.pipeline import Pipeline
from hybrid_agent.runtime.cli_executor import cli_capability, execute_cli
from hybrid_agent.runtime.execution_queue import ExecutionQueue


def policy_input(env, obs, task='List files', step=None, mode_prompts=None):
    pipeline = Pipeline(model=ChatModel(ModelConfig()), grounding=None, env=env,
                        system_prompt='', mode_prompts=mode_prompts)
    state = pipeline._init_state(task)
    state.obs = obs
    if obs.screenshot is not None:
        state.screenshot_history.append(obs.screenshot)
    if step is not None:
        state.queue = ExecutionQueue({'steps': [step]})
    messages = pipeline.build_input(state)
    return state, messages[0]['content']


@pytest.mark.parametrize('custom', [False, True])
def test_cli_decision_receives_real_runner_python_and_shell(tmp_path, custom):
    env = CLIEnvironment(workspace_dir=str(tmp_path))
    obs = env.reset('List files')
    state, system = policy_input(env, obs, mode_prompts={'cli': 'Pick one CLI operation.'} if custom else None)
    shell = 'cmd.exe' if os.name == 'nt' else '/bin/bash'
    assert state.prompt_metadata['prompt_profile']['mode'] == 'cli'
    assert json.dumps(os.path.abspath(sys.executable), ensure_ascii=False) in system
    assert f'"shell": "{shell}"' in system and 'python_executable' in system
    assert 'write' in system and '脚本' in system
    if os.name == 'nt':
        assert 'powershell.exe -NoProfile' in system and 'cmd.exe /d /s /c' in system


@pytest.mark.parametrize('target_os,dialect', [('linux','/bin/bash'), ('windows','cmd.exe'), ('','')])
def test_guest_uses_only_guest_capabilities(target_os, dialect):
    guest = SimpleNamespace(cli_target='guest', os_name=target_os, cli_only=True, guest_workspace_dir='~/guest-test',
                            controller=SimpleNamespace(run_python_script=lambda code: None))
    cap = cli_capability(guest)
    assert cap.get('shell', '') == dialect
    state, system = policy_input(guest, Observation(info={'active_surface': 'terminal'}))
    assert state.prompt_metadata['prompt_profile']['mode'] == 'cli'
    assert 'python_executable' not in cap and json.dumps(sys.executable, ensure_ascii=False) not in system
    assert 'guest' in system
    if dialect:
        assert f'"shell": "{dialect}"' in system


def test_required_desktop_method_does_not_offer_shell(tmp_path):
    env = SimpleNamespace(cli_target='local', os_name='windows', initial_state={'workspace_dir': str(tmp_path)})
    obs = Observation(screenshot=Image.new('RGB', (40, 40)), info={'task_mode':'desktop', 'active_surface':'desktop'})
    step = {'goal':'写文件', 'expected_evidence':'文件出现', 'actions':['cli']}
    state, system = policy_input(env, obs, task='使用 Word 保存文件，禁止 CLI。', step=step)
    assert 'cli' not in state.prompt_metadata['selected_actions']
    assert 'cli_shell' not in state.prompt_metadata['prompt_rules'] and 'python_executable' not in system


@pytest.mark.skipif(os.name != 'nt', reason='requires the Windows cmd.exe parser')
def test_real_cmd_uses_native_quotes_shell_operators_and_explicit_powershell(tmp_path, monkeypatch):
    # Exercise quoted executable *and* script paths, including on CI installed
    # in a space-free directory. No pip or third-party executable is needed.
    venv_dir = tmp_path / 'runtime with spaces'
    venv.EnvBuilder(with_pip=False).create(venv_dir)
    monkeypatch.setattr(sys, 'executable', str(venv_dir / 'Scripts' / 'python.exe'))
    env = CLIEnvironment(workspace_dir=str(tmp_path))
    env.prepare_workspace()
    cap = cli_capability(env)
    script = tmp_path / 'a script.py'
    write = execute_cli(env, {'operation':'write', 'path':str(script), 'content':"print('RUNNER_OK')"})
    assert write['status'] == 'executed'
    commands = [(f'"{cap["python_executable"]}" "{script}"', 'RUNNER_OK'),
                ('powershell.exe -NoProfile -Command "Write-Output (2 + 3)"', '5'),
                ('echo CMD_OK>receipt.txt && type receipt.txt', 'CMD_OK')]
    for command, expected in commands:
        result = execute_cli(env, {'operation':'shell', 'command':command})
        assert result['command'] == command and result['exit_code'] == 0, result
        assert result['stdout'].strip() == expected
