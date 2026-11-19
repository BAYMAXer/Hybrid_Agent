# 在 Linux 上评测 MiniWoB++

## 一键运行 15 局

在 Linux 上将仓库克隆到有写权限的目录，然后从其父目录运行：

```bash
git clone https://github.com/BAYMAXer/Hybrid_Agent.git hybrid_agent
bash hybrid_agent/scripts/run_miniwob_pilot.sh
```

脚本自动准备虚拟环境并安装 Python 依赖，先用固定动作运行浏览器自检。自检取得官方奖励 `+1` 后，使用项目原有的 `hybrid_agent/config.yaml` 运行 `click-test-2`、`enter-text`、`click-checkboxes`，每个任务跑 seed 0–4。决策和规划默认请求 `Qwen3.6-27B`（`http://10.44.101.62:8094/v1`），视觉定位请求 `UI-Venus-2-9B`（`http://7.246.80.189:8001/v1`）。请确保评测机可访问这两个服务。仅做浏览器自检时用下方的 `--smoke-test` 命令。

结果保存在仓库父目录的 `runs/miniwob-pilot/summary.json`。中断后可用相同代码和配置再次执行同一脚本来续跑。源码或配置变动后，或需要从头再跑时，请为新批次指定另一个目录：

```bash
MINIWOB_OUTPUT_DIR=runs/miniwob-pilot-2 bash hybrid_agent/scripts/run_miniwob_pilot.sh
```

脚本优先使用本机已有的浏览器和驱动，也支持显式设置 `MINIWOB_CHROME_BINARY` 和 `MINIWOB_CHROMEDRIVER` 两个绝对路径。Debian 缺少 Chromium 或驱动时，脚本可通过 `apt-get` 安装。其他 Linux 环境未找到现成组合时，自检会调用 Selenium Manager 发现或下载浏览器和驱动；首次自动下载需能访问对应软件源，并具备 Chrome 所需的系统共享库。安装系统包可能需要 `sudo`；评测本身使用当前用户运行。[Selenium Manager 说明](https://www.selenium.dev/documentation/selenium_manager/)

以下为手工分步运行的方法。示例从能看到 `hybrid_agent/` 的目录执行。

## 1. 安装并验证浏览器

下面的系统安装命令适用于 Debian 12。其他发行版请安装 Python 3.10+、Chrome/Chromium 和匹配的 ChromeDriver，并将两个环境变量指向各自的可执行文件。

```bash
sudo apt-get update
sudo apt-get install -y python3-venv chromium chromium-driver
python3 -m venv .venv-miniwob
source .venv-miniwob/bin/activate
python -m pip install -r hybrid_agent/requirements/requirements-miniwob.txt

export MINIWOB_CHROME_BINARY="$(command -v chromium)"
export MINIWOB_CHROMEDRIVER="$(command -v chromedriver)"
"$MINIWOB_CHROME_BINARY" --version
"$MINIWOB_CHROMEDRIVER" --version

python -m hybrid_agent.adapters.run_miniwob --list-tasks
python -m hybrid_agent.adapters.run_miniwob --smoke-test
```

自检走项目的 MiniWoB++ 适配器，读取按钮引用，在真实浏览器点击 `ONE`，然后检查官方终局。成功时命令退出码为 0，输出包含 `"terminated": true`、`"binary_reward": 1.0` 和 `"environment_steps": 1`。此步可在配置模型之前运行。[官方安装说明](https://pypi.org/project/miniwob/1.1.0/) · [MiniWoB++ 接口](https://miniwob.farama.org/content/basic_usage/)

要进一步验证 Agent 主循环、错点扣分、过期引用拒绝和纯视觉模式的动作菜单，可在同一环境运行以下集成测试。测试用固定决策替代远程服务，仍由真实浏览器评分。

```bash
python -m unittest -v hybrid_agent.tests.test_miniwob
```

## 2. 配置模型并跑首题

要手工使用与一键脚本相同的配置跑首题，可直接执行：

```bash
python -m hybrid_agent.adapters.run_miniwob --config hybrid_agent/config.yaml \
  --tasks click-test-2 --seeds 0 --output-dir runs/miniwob-first
```

若要通过环境变量改连另一套服务，Python 入口在省略 `--config` 时会读取 `hybrid_agent/config/miniwob.yaml`。下面两个 URL 假定决策与定位服务分别监听本机的 8000、8001 端口，请改为 Linux 评测机实际能访问的 OpenAI 兼容地址。决策服务使用支持图像和 JSON 输出的模型。使用鉴权服务时，通过运行环境提供相应的密钥。

```bash
export MINIWOB_MODEL_NAME='Qwen3.6-27B'
export MINIWOB_MODEL_URL='http://127.0.0.1:8000/v1'
export MINIWOB_GROUND_URL='http://127.0.0.1:8001/v1'
# 下面两个值用于无需鉴权的服务。
export MINIWOB_MODEL_API_KEY='EMPTY'
export MINIWOB_GROUND_API_KEY='EMPTY'

python -m hybrid_agent.adapters.run_miniwob \
  --tasks click-test-2 --seeds 0 --output-dir runs/miniwob-custom
cat runs/miniwob-custom/miniwob/click-test-2-v1/seed-0/result.json
```

这次由实际 Agent 规划并选择动作。看单局的 `status`、`agent_termination_reason`、`environment.first_action_ms` 和 `environment.binary_reward`。退出码 0 表示批次跑完且没有基础设施错误，任务本身可能成功也可能失败。首次模型联调时，优先核对模型地址、模型名和首次动作耗时。已有部署配置也可通过 `--config /path/to/config.yaml` 指定；该配置须使用环境评分，去掉 `completion` 与 `private_completion`。

## 3. 小批量、全量和续跑

```bash
python -m hybrid_agent.adapters.run_miniwob --config hybrid_agent/config.yaml \
  --tasks click-test-2 enter-text click-checkboxes \
  --seeds 0 1 2 3 4 --output-dir runs/miniwob-pilot
cat runs/miniwob-pilot/summary.json

# 中断或修复模型/浏览器服务后，使用相同输出目录和参数续跑。
python -m hybrid_agent.adapters.run_miniwob --config hybrid_agent/config.yaml \
  --tasks click-test-2 enter-text click-checkboxes \
  --seeds 0 1 2 3 4 --output-dir runs/miniwob-pilot --resume

# 全部已注册任务 × 5 个固定 seed。
python -m hybrid_agent.adapters.run_miniwob --config hybrid_agent/config.yaml \
  --all-tasks --seeds 0 1 2 3 4 --output-dir runs/miniwob-all
```

默认 `--observation-mode hybrid` 提供题面、任务截图和可见 DOM 节点。要测纯视觉，使用 `--observation-mode gui` 并换一个输出目录。支持点击、双击、输入、按键、组合键、滚动和拖拽；下拉列表使用点击和方向键。终端、任意 JavaScript、框架技能和站外导航不属于该评测的动作空间。一次输入或拖拽可能需要多个 Gym 动作，`agent_steps` 统计决策轮数，`environment.environment_steps` 统计实际提交的 Gym 动作数。

每题每个 seed 使用独立浏览器和一次 `reset(seed=...)`，数据模式为 `test`。默认每个 Gym 动作后等待 150 毫秒，可用 `--wait-ms` 指定；每局默认最多 40 轮决策，可用 `--max-steps` 覆盖。网页本身的计时器从回合开始就运行，基础时限为 10 秒，个别任务会覆盖。规划、推理和核查都会占用这段墙钟时间。修改 Agent 的最大步数或超时不会延长网页时限。某些任务即使固定 seed 仍受浏览器渲染时序影响。[官方计时实现](https://github.com/Farama-Foundation/miniwob-plusplus/blob/v1.1/miniwob/html/core/core.js#L38-L95) · [官方可复现性说明](https://miniwob.farama.org/content/basic_usage/)

## 4. 读成绩和复盘

| 文件或字段 | 含义 |
| --- | --- |
| `manifest.json` | 任务、种子、模型、观察模式、软件版本、配置和代码指纹。续跑校验同一批次。 |
| `summary.json` | 整批和分任务计数。所有计划样例均有计分结果时才给出 `success_rate`；`success_rate_scored` 仅以当前已计分样例为分母。 |
| `miniwob/<任务>-v1/seed-<种子>/result.json` | 单局状态、离散成功分、官方奖励、步数、终止原因和本次尝试目录。 |
| `attempt-*/trajectory.json` | 规划、决策、动作、观察与核查记录，同目录保存截图和图像资源。 |

成功要求 MiniWoB++ 判定终局且 `get_binary_reward` 返回 `+1`。同时保存 `raw_reward`（含部分分）和 `env_reward`（网页原始得分，可能含时间折扣）。错误点击或网页超时计失败；Agent 提前结束且没有官方成功终局也计失败，缺失的官方终局奖励记为 `null`。浏览器或模型服务异常单列在 `errors`。续跑跳过已计分的成功和失败，重新尝试错误和未完成项。配置、任务清单、种子或源码有变化时，另选输出目录。[官方奖励定义](https://miniwob.farama.org/content/reward/)
