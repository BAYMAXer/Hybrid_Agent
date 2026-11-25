# 在新 Windows 电脑部署 Hybrid Agent 并接入公司模型

给接手机器上的 agent：在目标电脑上安装项目，先用公司接口跑通一次文件任务和一次视觉定位，再从 Studio 下发桌面任务并检查实时轨迹。下文按 Windows x64、Python 3.12、本机执行、公司服务器推理编写。示例域名和模型 ID 均为占位符，请先取得实际值。目标机无需部署模型权重，也无需登录 Terra。

若目标机使用 macOS，先按 [README 的 macOS 安装说明](../README.md#连接远程模型运行)创建环境并授予屏幕录制和辅助功能权限，模型角色和接口配置仍可参照第 1、3 节。Linux 的本机入口支持 CLI 文件与终端任务。

## 1. 先确认公司提供哪些接口

| 角色 | 向服务方索取 | 项目中的接法 |
| --- | --- | --- |
| 决策、规划、语义核查 | OpenAI 兼容的 Chat Completions 基址、实际请求用的模型 ID、鉴权方式。桌面任务需要支持截图和 JSON 输出 | 一个公司多模态模型可承担三个逻辑角色。Studio 选择该决策模型，语义验证选「复用决策模型」 |
| 视觉定位 | 接收截图和目标描述的模型 ID、基址、凭据，以及模型返回的坐标格式 | 通用多模态模型可用 `qwen-plus` 适配器，依次在整图和局部截图上返回网格 JSON。也支持 `ui-venus2` 的 0–1000 归一化坐标和 `ui-tars` 默认的原图像素坐标 |
| 独立 Jev（可选） | System One 完整 URL、模型 ID、单独的鉴权要求 | 仅当公司部署了该协议时启用。它接收 `state`、`questions` 和可选 `images`，返回按问题 ID 组织的 `answers`。普通聊天模型可以通过「复用决策模型」承担语义核查 |

**公司只有一个多模态接口时**，下面两组预设填同一个 URL 和模型 ID，定位类型选 `qwen-plus`，共用同一份凭据即可。如果只有文本模型，可以先验收 CLI 文件任务，桌面任务的决策和视觉定位还需可读图的模型。

聊天基址填到服务方公布的 API 前缀，例如 `https://inference.company.example/v1`，末尾不带斜杠。客户端会追加 `/chat/completions`。先确认网关接受 Bearer 鉴权、Base64 `image_url`、`response_format=json_object`，以及当前请求携带的 `chat_template_kwargs.enable_thinking=false`。如果公司只提供不同协议、自定义认证头或专有请求格式，需要由网关转换或适配客户端。Studio 的 Provider 下拉不会改变本机 runner 使用的 Chat Completions 协议。[决策客户端](../model/decision_model.py) · [本机 runner](../run_windows_local.py)

## 2. 安装项目

在目标 Windows 用户的 PowerShell 中执行。准备好 Git、64 位 Python 3.12 和访问公司推理服务所需的网络。执行桌面任务时，使用已登录且解锁的交互式桌面，并安装任务实际涉及的应用。浏览器任务另需 Google Chrome。

```powershell
$DeployRoot = Join-Path $env:USERPROFILE 'yaero-deploy'
$RepoRoot = Join-Path $DeployRoot 'hybrid_agent'
$AgentPython = Join-Path $DeployRoot '.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force -Path $DeployRoot | Out-Null
Set-Location -LiteralPath $DeployRoot
git clone https://github.com/BAYMAXer/Hybrid_Agent.git hybrid_agent
py -3.12 -m venv .venv
& $AgentPython -m pip install -r (Join-Path $RepoRoot 'requirements\requirements-windows.txt')
& $AgentPython -m pip check
& $AgentPython -m hybrid_agent.run_local --help
git -C $RepoRoot rev-parse HEAD
```

目录名保留为 `hybrid_agent`。下文所有 Python 命令都在其**父目录**运行。已有检出时跳过 clone，先用 `git -C $RepoRoot status --short --branch` 核对本地改动和分支。记录部署的提交号。安装检查应显示 `No broken requirements found`，并能打印 runner 的参数列表。[Windows 依赖清单](../requirements/requirements-windows.txt)

若系统找不到 `py` 启动器，使用已安装的 Python 3.12 可执行文件完整路径替代 `py -3.12`，例如 `& 'C:\实际安装目录\python.exe' -m venv .venv`。后续命令统一使用 `$AgentPython`。

## 3. 替换模型预设和凭据

编辑目标机的 [config/model_presets.yaml](../config/model_presets.yaml)。把公司配置放在各列表第一项，保留其他仍需使用的预设。下面示例适用于同一个多模态模型承担决策和定位。使用两套服务时，分别替换各自的 `name`、`url` 和密钥引用。`name` 填服务接受的模型 ID。Studio 刷新页面时从本机 `/model-presets` 读取该文件，并选中每组第一项。

```yaml
decision_models:
  - name: "company-vision-model"
    provider: "openai"
    url: "https://inference.company.example/v1"
    api_key: "env:COMPANY_DECISION_KEY"

grounding_models:
  - name: "company-vision-model"
    provider: "openai"
    type: "qwen-plus"
    url: "https://inference.company.example/v1"
    api_key: "env:COMPANY_GROUND_KEY"
```

在同一个 PowerShell 会话注入凭据，后面也用它启动 Studio。公司已经通过其凭据管理器设置好相应的进程环境变量时，跳过输入。

```powershell
$env:COMPANY_DECISION_KEY = [System.Net.NetworkCredential]::new('', (Read-Host '公司决策服务密钥' -AsSecureString)).Password
$env:COMPANY_GROUND_KEY = $env:COMPANY_DECISION_KEY
# 定位服务有独立密钥时，改为单独输入
# $env:COMPANY_GROUND_KEY = [System.Net.NetworkCredential]::new('', (Read-Host '公司定位服务密钥' -AsSecureString)).Password
```

Studio 会将预设的 `api_key` 字段返回给前端，本机 runner 也通过命令行传递该字段，所以 YAML 和 Studio 的 API Key 输入框都只填 `env:变量名`。决策和 `qwen-plus` 会优先从进程环境解析，缺失时再查仓库根目录已被 Git 忽略的 `.env`（格式为 `变量名=值`）。公司明确采用免鉴权接口时，对应预设可填 `EMPTY`。进程变量不会跨新的终端会话保留。[密钥解析实现](../config/__init__.py)

**专用定位模型的鉴权差异。** 当前 [UI-TARS](../adapters/uitars_grounding.py) 和 [UI-Venus-2](../adapters/uivenus2_grounding.py) 会把 `api_key` 原样交给 SDK。若这两种服务要求真实密钥，目标机要先适配所选类：参照 [Qwen 定位适配器](../adapters/qwen_grounding.py) 导入 `from ..config import resolve_secret`，将 `_get_client()` 中的 `api_key=self.api_key` 改为 `api_key=resolve_secret(self.api_key)`，再执行第 4 节定位测试。直接填写 `env:COMPANY_GROUND_KEY` 而未做该适配，会把这串字面量发给服务器。

**不同入口的生效范围**

| 使用入口 | 如何替换 |
| --- | --- |
| Studio 本机任务 | 修改上述 YAML、刷新页面、在「模型与知识设置」选择公司模型。每次启动本机任务时，Studio 把当前选择作为显式参数传给 runner |
| 直接运行 `python -m hybrid_agent.run_local` | 显式传 `--model*` 和 `--ground-*` 参数，见下一节。命令行不会自动读取 Studio 预设 |
| 需要单独的规划或聊天核查服务 | 在下面的 CLI 参数数组中加入 `--planner-model ID --planner-url BASE` 或 `--verifier-model ID --verifier-url BASE`。独立规划须同时提供模型 ID。这两个角色共用主决策的 `--model-api-key`。Studio 本机表单当前未暴露这两组独立参数 |
| 使用 `main.py --config` 的其他入口 | 修改其实际加载的配置文件。只改根目录的 `config.yaml` 不会改变上述 Studio 或 `run_local` 的连接 |

## 4. 在目标机分别验证文件任务和视觉定位

**文件任务。** 在第 2、3 节的 PowerShell 会话中运行。命令读取刚配置的第一组预设，使用独立临时目录，只要求模型创建一个测试文件。验收同时检查实际文件内容、轨迹终态和后端类型。改模型或启用 Jev 后复测时，从代码块第一行重新运行，给每次验收分配新的文件和轨迹目录。

```powershell
$PresetJson = & $AgentPython -c "import json; from hybrid_agent.config import CONFIG_DIR, load_yaml; print(json.dumps(load_yaml(CONFIG_DIR / 'model_presets.yaml')))"
$Presets = $PresetJson | ConvertFrom-Json
$Decision = $Presets.decision_models[0]
$Ground = $Presets.grounding_models[0]
$SmokeRoot = Join-Path $env:TEMP ('yaero-handoff-' + [guid]::NewGuid().ToString('N'))
$Work = Join-Path $SmokeRoot 'workspace'
$Day = Join-Path $Work (Get-Date -Format 'yyyy-MM-dd')
$Out = Join-Path $Day 'handoff.txt'
$Run = Join-Path $SmokeRoot 'trace'
$Spec = Join-Path $SmokeRoot 'completion.json'
New-Item -ItemType Directory -Force -Path $Day | Out-Null
$Check = @{id='handoff'; kind='desktop_file'; path=$Out; change='created'; format='txt'; text_equals='HANDOFF_OK'}
[IO.File]::WriteAllText($Spec, (@{checks=@($Check)} | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
$RunArgs = @(
    '--verification-backend', 'configured', '--task-mode', 'cli',
    '--model', $Decision.name, '--model-url', $Decision.url, '--model-api-key', $Decision.api_key,
    '--ground-type', $Ground.type, '--ground-model', $Ground.name, '--ground-url', $Ground.url, '--ground-api-key', $Ground.api_key,
    '--knowledge-method', 'all', '--workspace-dir', $Work, '--result-dir', $Run,
    '--completion-spec', $Spec, '--max-steps', '8',
    '--task', "Create the text file $Out with exactly HANDOFF_OK and no trailing newline."
)
& $AgentPython -m hybrid_agent.run_local @RunArgs
if ($LASTEXITCODE -ne 0) { throw '任务进程异常退出' }
$Trace = Get-Content -LiteralPath (Join-Path $Run 'trajectory.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Trace.run_manifest.verification_backend -ne 'configured' -or $Trace.outcome.success -ne $true -or [IO.File]::ReadAllText($Out) -cne 'HANDOFF_OK') { throw "验收失败，请检查 $Run" }
"CLI 验收通过，轨迹目录 $Run"
```

**视觉定位。** CLI 成功时可能完全没调用定位模型。下面的命令直接使用预设选中的实际适配器，发送一张合成测试图，并检查返回坐标是否落在蓝色矩形里。它不会发送真实桌面截图或移动鼠标。

```powershell
@'
from PIL import Image, ImageDraw
from hybrid_agent.config import CONFIG_DIR, load_yaml
from hybrid_agent.adapters.qwen_grounding import QwenPlusGrounding
from hybrid_agent.adapters.uitars_grounding import RealUItarsGrounding
from hybrid_agent.adapters.uivenus2_grounding import UIVenus2Grounding

cfg = load_yaml(CONFIG_DIR / "model_presets.yaml")["grounding_models"][0]
types = {"qwen-plus": QwenPlusGrounding, "ui-tars": RealUItarsGrounding, "ui-venus2": UIVenus2Grounding}
adapter = types[cfg["type"]](url=cfg["url"], model=cfg["name"], api_key=cfg["api_key"])
img = Image.new("RGB", (800, 400), "white")
ImageDraw.Draw(img).rectangle((530, 60, 690, 145), fill="blue")
point = adapter.locate(img, "the center of the only solid blue rectangle")
print(cfg["name"], cfg["type"], point.x, point.y, point.confidence)
if not (point.confidence > 0 and 530 <= point.x <= 690 and 60 <= point.y <= 145):
    raise SystemExit("Grounding check failed")
'@ | & $AgentPython -
if ($LASTEXITCODE -ne 0) { throw '公司定位接口实测未通过' }
```

## 5. 从 Studio 下发桌面任务并看实时轨迹

继续在已设置凭据的 PowerShell 执行。运行期间保留这个窗口。脚本会复用同一目录、同一端口的旧 Studio；更换了进程环境中的密钥或解释器时，先在旧启动窗口按 Ctrl+C，再启动。

```powershell
$env:OSWORLD_RUNNER_PYTHON = $AgentPython
$env:OSWORLD_KEEP_MODEL_WARM = '0'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot 'scripts\start_studio_windows.ps1') -PythonPath $AgentPython -Port 8090 -OpenBrowser
```

1. 在另一 PowerShell 运行 `Invoke-RestMethod 'http://127.0.0.1:8090/runtime/info'`。预期 `service=yaero-studio`、`platform=win32`、`project_root` 指向目标机的 `hybrid_agent`。若端口已被其他程序占用，改用 `-Port 8091`，同步替换访问地址。
2. 打开 [本机 Studio](http://127.0.0.1:8090/#run)，选择「本机任务 → Windows 通用桌面」，**取消勾选「使用已授权的 GPT-6.1 Terra 执行决策、定位和核查」**。Windows 初次选桌面场景时可能默认勾选，切换场景后要再次确认。勾选时，任务会走 Terra 专用后端。
3. 在「模型与知识设置」核对两个模型 ID 和服务地址。没有独立 Jev 时，将「语义验证」切到「复用决策模型」，知识选择「全部提供」。点击「测试决策模型连接」；它实际发送一张随机六字符图片，要求模型读图并返回 JSON。显示成功才算主模型的读图和输出检查通过。
4. 打开一个新的空白记事本窗口。任务填写“在已打开的记事本中输入 HANDOFF_DESKTOP_OK，并将文字保留在窗口中”，点击「下发任务并查看实时轨迹」。观察「实时监控」是否陆续收到动作、截图和核查结果，同时确认记事本实际出现指定文字。专门验证视觉路径时，可勾选「纯视觉桌面验证」，此时会关闭 AX 和 OCR 回退。
5. 从「本机轨迹」打开同一次运行。默认文件位于仓库 `local_runs/ui_*/`，其中 `trajectory.json` 的 `run_manifest.verification_backend` 应为 `configured`，`outcome.success` 应为 `true`。在 `events.jsonl` 的 `model_call` 记录里核对实际调用的模型，必要时按时间与公司网关请求记录对照。简单桌面任务可能使用键盘或 AX 完成，定位能力以第 4 节独立测试为准。[Studio 启动脚本](../scripts/start_studio_windows.ps1) · [Studio 到 runner 的参数传递](../viz/server.py)

## 6. 公司提供独立 Jev 或需要原生 AX 时

**独立 Jev。** 在启动 Studio 或 CLI 前把密钥注入相应父进程的环境变量 `DECISION_JUDGE_API_KEY`，可以沿用第 3 节的隐藏输入方式。Studio 的 Jev 下拉当前提供 `vllm-sr/d3-flash`，对应部署可直接选用并填写公司 System One 地址。公司使用其他模型 ID 时，在 CLI 的 `$RunArgs` 中增加 `--judge-url https://judge.company.example/v1/systemone --judge-model 实际模型ID --judge-review-mode exclusive`，让核查模式与 Studio 一致。CLI 默认核查模式为 `assist`。

知识筛选可以继续用 `all`。若也要用 Jev，把 `--knowledge-method` 的值改为 `jev` 并指定 `--knowledge-jev-url 公司SystemOne地址`。知识 Jev 只单独配置地址，模型 ID 跟随已启用的动作 Jev，共用上述鉴权。单独开启知识 Jev 时，当前代码使用默认 ID `vllm-sr/d3-flash`。[知识筛选的调用位置](../pipeline.py)

在 Studio 运行期间，用另一 PowerShell 执行以下只读判题请求，替换占位值。预期 `ok=True`、`status=answered` 且 `answers.file.top_label=yes`。仅检查 `/health` 或模型列表无法覆盖这个协议。[Jev 调试入口](../viz/jev_debug.py) · [System One 客户端](../model/decision_judge.py)

```powershell
$Probe = @{
    config = @{url='https://judge.company.example/v1/systemone'; model='company-jev-id'}
    state = 'Observed filename: handoff.txt'
    questions = @{file=@{type='choice'; instructions='Is the observed filename handoff.txt?'; criteria=@{yes='It matches'; no='It differs'}}}
}
$Jev = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8090/jev/decide' -ContentType 'application/json' -Body ($Probe | ConvertTo-Json -Depth 8)
$Jev | Select-Object ok, status, model
$Jev.answers.file | Select-Object top_label, probabilities
```

**原生桌面 AX。** 基础部署先用视觉路径。需要读取 Windows 可访问性控件树时，在目标机准备 PowerShell 7、Git、Go 1.22+ 和 .NET 8 SDK，从部署父目录运行以下命令。安装器使用固定提交 `697d3fbda04cb322915866bab1aad79c726a61e3`，默认安装到 `%LOCALAPPDATA%\HybridAgent\hcu\697d3fb`。正常使用 AX 时，取消「纯视觉桌面验证」，通过支持 UIA 的窗口核对轨迹里出现的 `desktop_ax` 观察。[HCU 安装与版本说明](desktop-hcu-windows.md)

```powershell
$HcuSource = Join-Path $DeployRoot 'Hybrid-Computer-Use'
git clone https://github.com/microsoft/Hybrid-Computer-Use.git $HcuSource
pwsh -NoProfile -File (Join-Path $RepoRoot 'scripts\install-hcu-windows.ps1') -SourceDirectory $HcuSource
```

## 7. 遇到问题时

| 现象 | 优先检查 |
| --- | --- |
| `No module named hybrid_agent`，或 Studio 能开、任务缺包 | 目录名、当前是否在仓库父目录、`$AgentPython` 和 `OSWORLD_RUNNER_PYTHON` 是否指向同一虚拟环境 |
| 独立测试通了，Studio 任务仍走 Terra 或旧服务器 | 在所选场景重新检查 Terra 勾选状态、刷新模型预设、核对语义验证和知识筛选是否仍指向旧 Jev。更换进程变量后重启原 Studio |
| 401/403、404 或 model not found | 分别核对决策和定位的密钥引用、服务账号授权、模型 ID 和基址；404 时检查是否重复拼接了 `/v1` 或 `/chat/completions` |
| 400/422、读图失败或连接超时 | 核对网关对图片、JSON 模式和 `chat_template_kwargs` 的支持。确认目标机能直连内网域名及端口、TLS 证书受信任。运行时模型客户端配置了 `trust_env=False`，仅设置系统代理可能无法打通 |
| 文件成功但桌面定位失败 | 查看合成图的返回点和置信度，确认适配器与公司模型的坐标协议一致。若只有原生定位适配器返回 401，检查第 3 节的密钥解析 |

交接回报保留部署提交号、Python 版本、各角色的模型 ID 和适配器类型、Studio 地址、CLI 与桌面轨迹目录、合成图返回坐标，以及 Jev 的实测结果或「未启用」。只有在目标机实际发起公司请求并核对产物后，才记录该项通过。凭据不写入文档、轨迹备注或 Git。

## 8. 双屏选择与公司环境复验

在 Studio 选择「本机任务 → Windows 通用桌面 → 任务屏幕」。初始选项为 Windows 主屏，选项会显示设备标识和物理分辨率。单任务和批量任务都在下发时绑定具体设备。命令行可传 `--desktop-monitor primary` 或设备 ID；Studio 运行期间执行以下命令可查看本机可用的标识。

```powershell
(Invoke-RestMethod 'http://127.0.0.1:8090/runtime/info').desktop_monitors |
    Format-Table id, name, width, height, primary
```

1. 在目标电脑上分别建立两个空测试目录。设为公司模型、选「纯视觉桌面验证」、取消 Terra。第一轮要求“通过资源管理器和 Word 在第一个目录新建 test.docx，禁止后台或 CLI”；第二轮只要求在第二个目录新建同名 Word 文档，允许计划选择 CLI。把实际绝对路径写入每轮任务。正式验收可像第 4 节一样传 `--completion-spec`，将 `desktop_file` 的 `format` 改为 `docx`，保留 `change=created`，删除该节示例中无关的文本要求。确认目标文件可由 Word 打开、轨迹验收成功，并核对第一轮全程没有后台写入。
2. 在副屏打开任务明确指定的文档，同一应用另开一份无关文档。下发指定任务屏的 GUI 任务，检查目标及其已确认的保存对话框位于任务屏、无关文档仍在原位。对照 `trajectory.json` 的 `run_manifest.desktop_monitor`、`desktop_monitor_info` 及逐步截图尺寸。若设备被拔出，检查事件里记录的停止原因。导航回放中，单击选中文件夹不能证明进入目录，核对 `steps[].step_progress` 是否在活动视图确证目标路径后出现进展。
3. 对比旧版合屏与新版单屏时，分别用两个检出目录，在**同一台双屏电脑**固定 Windows 布局、任务屏、公司模型版本、历史帧配置和任务指令；两组使用各自的全新输出目录。每版至少复测三轮，记录截图尺寸、每次请求的图片数、服务实际返回的输入 token、决策耗时和总耗时中位数。回放旧轨迹中的记录可作旁证；模型未上报 token 时填「未上报」。另记成功率和实际步数，避免把少走操作步骤带来的差异归入单次推理耗时。
