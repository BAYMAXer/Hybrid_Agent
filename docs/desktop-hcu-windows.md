# Windows 通用桌面任务 V1 修改计划与落地对照

## 目标与入口

V1 为 Windows 浏览器以外的任务提供独立入口，包括跨原生应用操作，以及用户允许的文件和终端步骤。从预览到验收复用浏览器的执行顺序，即预览当前环境 → 规划可验收的子目标 → 决策一个动作 → 定位和执行 → 重新观察 → 核查步骤 → 继续、重规划或最终验收。本版新增通用桌面能力，浏览器与桌面混合任务的编排安排在下一版。

| 场景 | 入口与执行行为 |
|---|---|
| 通用桌面 | Windows runner 和 Studio 单次、批量默认使用 `desktop`，也可显式传 `--task-mode desktop`。从当前桌面截屏启动，无需 Chrome 或 CDP。可切换多个目标原生应用。 |
| 浏览器 | `--task-mode browser` 启用已有 Chrome/CDP 和页面 DOM 流程。论文回放演示绑定此入口。 |
| 纯后台文件／终端 | `--task-mode cli` 与 `--cli-only` 均启动已有后台环境。桌面任务内部也可以有已规划且符合用户要求的 CLI 步骤。 |

改造前，Windows runner 和 `WindowsEnvironment.begin_episode()` 默认准备浏览器。已有的 `observe_desktop`、截图和真实键鼠可用于执行中途接管。改造后，`prepare_workspace()` 与 `begin_episode()` 按任务模式分开初始化，桌面模式从预览到收尾都可独立运行。两种环境共享队列和验收服务，桌面任务的动作菜单、技能权限和执行分发共同约束可操作范围。入口实现见 [Windows runner](../run_windows_local.py)、[Windows 环境](../adapters/windows_env.py) 和 [Studio 单次与批量启动](../viz/server.py)。

## HCU 到本地的移植清单

HCU 暴露两个原生桌面工具。`AX` 读可访问性树，并按控件引用操作。`GUI` 提供截图、局部放大和指针键盘输入。宿主 Agent 另行提供文件、Shell 和浏览器 CDP 通道。本地已有视觉定位与输入链，因此V1 接入 HCU 的 Windows AX 部分，沿用本地视觉执行器，并保留项目自己的浏览器 DOM 入口。上游源码以 [HCU 固定版本](https://github.com/microsoft/Hybrid-Computer-Use/tree/697d3fbda04cb322915866bab1aad79c726a61e3) 为基准，见其 `AGENTS.md` 的通道约定。

| 实施顺序 | HCU 提供的代码与能力 | 本地复用和适配 |
|---|---|---|
| 1. 建立纯视觉基线 | HCU 有自己的 `GUI` 工具。本地已有等价的截图、定位和输入入口 | 复用 `capture_virtual_desktop`、`Pipeline.ground`、`Grounding.locate`。传入全桌面原图，执行前核对帧、窗口和几何范围，正确处理 DPI 与多显示器负原点。关闭 AX 后仍可单独验收。 |
| 2. 接入原生观察 | `sdk/python/hcu_sdk/__init__.py` 的 `Desktop.apps/observe`，Go `internal/core/records.go` 的 `Record`，Windows `UiaProvider.Observe` | [DesktopAX.observe](../desktop/hcu_ax.py) 用当前前台窗口匹配 HCU 记录，将控件角色、名称、值、状态和可用动作投影为 `desktop_ax`。只有匹配当前采样窗口的树才与图像一起进入决策。 |
| 3. 按引用执行 | SDK 的 `find/read/act`、Windows `UiaProvider.ResolveAndAct`、Go `RefRegistry` 与 `DesktopShared.ReplayOnce` | `DesktopAX.route/execute` 及 `WindowsEnvironment.execute_routed` 新增 `desktop_ax` 派发。执行前再次核对前台身份、窗口、快照和目标，按动作分配请求 ID。`type` 追加输入，显式 `overwrite:true` 才允许映射为 `set_value`。`select` 须解析到目标控件下唯一、可选且当轮已展示的选项。 |
| 4. 改造规划、路由与核查 | HCU 返回当前节点、动作回执、回读及恢复指示 | [模式选择](../planning/decision_modes.py) 从原来的桌面固定 GUI 扩展到 `gui/desktop_ax/cli`。[上下文编译](../planning/context.py) 保留真正写入本轮提示的 `exposed_refs`。[Jev 证据](../verification/judge_evidence.py)、[报告来源](../reporting/report_delivery.py) 和 Studio 轨迹能识别原生 AX 及实际通道。 |
| 5. 交付与降级 | 上游 `computer-use/native/windows/build.ps1` 生成 Go 核心与 Windows UIA provider | [安装脚本](../scripts/install-hcu-windows.ps1) 构建并安装固定版二进制、同版 Python SDK 及许可。HCU 缺失、窗口不匹配或当前应用未提供控件时，由该帧截图继续视觉操作。 |

**可行性取决于两层。** 桌面入口、视觉链、模型协议与验收队列已有实现支撑。AX 的收益取决于应用是否暴露可操作控件、窗口能否可靠匹配以及执行后是否有足够回读。标准按钮、文本框和选项适合 AX。自绘画布、手势和控件语义不完整的应用优先依赖截图。Windows 公共文件对话框还存在独立的 Shell 提交状态，处理方式见下文。

## 三个模型的输入输出协议

| 角色 | 输入 | 输出与约束 |
|---|---|---|
| 决策模型 | 初始规划读原任务、用户指定的方法、公开验收条件、工作目录和桌面摘要。动作轮读当前子目标、预期证据、合法菜单和最近回执。GUI/AX 模式提供新鲜截图，AX 模式附本轮控件，CLI 模式提供后台能力与工作目录。 | 规划返回含 1–32 个子目标的 JSON `steps`，每步含 `goal` 与 `expected_evidence`，重规划补 `strategy_change`。每个动作轮只给 `{"next_action":{...},"summary":"..."}`，摘要不超过 120 字。框架填写坐标、窗口句柄、观察 ID 和请求 ID。报告阶段另用受观测来源约束的 Markdown 正文。 |
| 定位模型 | `locate(screenshot, target)` 接收当前未标注原图和一个可见目标描述。输入框要写的正文由执行器持有。 | 各适配器统一返回 `GroundingResult(x,y,confidence)`，坐标以输入图左上角为原点，单位是该图的像素。框架检查可用性、边界和帧身份后换算到 Windows 虚拟桌面。单点定位失败时可使用现有唯一文本 OCR 回退。可在严格验证中关闭。 |
| Jev 判断模型 | `POST /v1/systemone` 请求含 `model`、JSON 字符串 `state`、按题名组织的 `questions`，可附按序排列的 `images`。选模式看当前目标和可操作控件。动作后看前后图、控件回读与系统回执。最终核查看仍待确认的要求和新鲜证据。 | 选择题返回 `answers.<题名>.type="choice"` 和覆盖所有给定选项的 `probabilities`。各值有限且位于 0–1，合计与 1 偏差至多 0.001。可选 `choice` 必须为最高概率项。按已配置的最高概率或阈值策略选标签。超时、协议错误或证据不足可得 `unknown`。 |

桌面 AX 和浏览器 DOM 都让决策模型从结构中选一个目标，再给出动作。桌面引用绑定本次会话、截图帧及窗口。代表同一控件的字符串也可能在重新观察后变化。示例 `desktop:session:frame:r4` 是框架生成并在本轮展示的完整引用。在 AX 模式，对当前文本框的覆盖输入可输出下面的动作。

```json
{"next_action":{"type":"type","target":"文档标题","target_ref":"desktop:session:frame:r4","text":"验收稿","overwrite":true},"summary":"覆盖当前标题，再读取新值核对"}
```

同一目标走纯视觉时，决策输出描述和文本，省略 `target_ref`，定位器收到当前图和“文档标题输入框”。例如定位结果 `(380,196)` 只对这张输入图有效。Jev 选模式的示例回答为 `{"answers":{"next_mode":{"type":"choice","probabilities":{"desktop_ax":0.96,"gui":0.03,"unknown":0.01}}}}`。实际选项与本次请求逐项一致。

## 按场景调用与恢复

以下次数指一次**逻辑调用**，不包含格式修正或网络重试。常规动作轮生成一个决策。规划、重规划和报告各使用独立调用。Jev 的选模式、动作后诊断、步骤核查与最终核查可以分别发生，因此不能把每个动作固定记为三次模型调用。

| 场景 | 决策模型输入 → 输出 | 定位模型 | Jev 输入 → 输出 |
|---|---|---|---|
| 初始规划、等待重规划 | 原任务和桌面预览 → 子目标计划。受阻时加失败事实和剩余目标 → 新计划及换法 | 0 次 | 规划本身跳过选模式。启用的任务分类、知识筛选是独立调用。 |
| 纯视觉单点 | 新图＋子目标 → 带可见目标描述的点击、双击或输入。`type` 另给文本 | 每目标 1 次，双击复用同一点 | 只有 GUI 可用时选模式 0 次。动作后可用前后图和真实派发结果判断稳定性及效果。 |
| 当前 AX 控件可操作 | 新图＋节点 → 一个带本轮 `target_ref` 的动作 | 0 次 | 有多种合法路径时按本轮证据询问 `next_mode`，相同证据可复用判断。AX 路径的动作后证据包括原生回执、回读、后图或新树。 |
| 引用失效、猜测引用或结果不确定 | 明确未派发时读拒绝原因，下一轮重新观察并选新动作。若提供方返回 `continuation.state=stop`，结束自动输入与恢复读取 | 被拒轮 0 次。新轮明确转 GUI 且有新图才定位 | 拒绝派发由代码确定。动作可能已执行且要求停止时保留不确定结果。语义判断不能授权重发。 |
| 当前截图缺失或过期 | 输入观察错误与窗口信息 → 请求重新观察或等待。已有合法后台任务时仍可走 CLI | 0 次，等到新帧再定位 | 没有可判断的画面时保留 `unknown`。独立的文件证据仍按自身验证规则判断。 |
| 快捷键、等待、切窗、原生模态窗 | 焦点和窗口状态 → `press/hotkey/wait/switch_applications` 等动作。焦点需要先由点击建立时，另占一个动作轮 | 无坐标动作 0 次。新窗按钮遵循 AX 或单点规则 | 使用前后窗、焦点和截图判断是否就绪。公共文件选择器使用 GUI 模式，选模式 0 次。 |
| 拖拽与画布 | 原图＋手势目标 → `drag_and_drop`，含 `from_target/to_target` | 同一帧 2 次，起点和终点均成功才派发 | 明确拖拽时选模式 0 次。用后图或产物核对图形变化。 |
| 允许的后台步骤 | 工作目录、任务指定方式、计划动作 → 一条 `cli` 操作。用户要求用 Excel 时走 Excel。独立授权的文件步骤才可走 CLI | 0 次 | 只允许 CLI 时选模式 0 次。以退出码、标准输出和真实文件回读核查。要求窗口可见时还要取得窗口证据。 |
| 步骤、报告与最终验收 | 读取核查反馈继续下一步，或输出 `request_finish` 附 `answer`。文字交付用实际来源生成正文 | 0 次 | 动作后可同批问 `settled/correct/next_mode/replan`。步骤问 `step`，区分进展和完成。最终先做文件等硬条件，再核查剩余语义。报告按正文、要求和来源核对。 |
| 浏览器与混合任务 | 显式浏览器入口读页面 DOM 与视口截图。桌面任务遇到需要操作网页的步骤，报告本版范围或返回目标原生应用 | 现有浏览器定位沿用视口规则 | 浏览器继续用原有 `dom/gui` 等配置。原生桌面引用与页面引用各自校验。混合调度留待后续版本。 |

动作后四问中，`settled` 取 `complete/waiting/unknown`，`correct` 和 `replan` 取 `yes/no/unknown`，`next_mode` 的候选由当前能力决定。步骤或最终语义使用 `passed/failed/pending/unknown`，`exclusive` 步骤还可标记 `progress`。例如，输入框出现新内容只证明编辑取得进展，要求“保存文件”的子目标仍需文件证据。

对“关闭后重开”或跨应用任务，最终核查还读取最近 16 条窗口与快捷键记录。每条对应前后两次 Win32 观测，包含前台 HWND、PID、标题和实际派发的快捷键。完整枚举可确认原窗口是否仍在可见窗口列表中。这样可以分别核对操作顺序、最后的界面以及落盘结果。

Jev 未配置或选模式证据不足时，有可用新鲜截图的桌面轮可走 GUI。核查依据 `assist/exclusive` 配置分流。`assist` 步骤在 Jev 不确定时可使用独立上下文审查（可复用决策端点），普通最终语义由独立审查处理。`exclusive` 的非文本步骤及最终语义由 Jev 判断，补充只读观察后证据确有变化才重问。文字报告保留现有回退。硬条件缺失或失败不能用模型判断覆盖。具体实现见 [决策提示与菜单](../decision_context.py)、[定位接口](../model/grounding.py)、[Jev 选择](../model/judge_selection.py) 和 [核查分流](../verification/review_service.py)。

## 安装

准备 PowerShell 7、Git、Go 1.22 或以上版本、.NET 8 SDK 和含有提交 `697d3fbda04cb322915866bab1aad79c726a61e3` 的 Hybrid-Computer-Use 仓库。在本仓库根目录运行以下命令，其中路径替换为实际 HCU 源码位置。

```powershell
pwsh -File ./scripts/install-hcu-windows.ps1 -SourceDirectory 'D:\project\复现\Hybrid-Computer-Use'
```

脚本在临时目录中从固定 Git 对象导出源码，使用 HCU 的构建脚本生成 Windows x64 核心和 UIA provider，并复制同版本 Python SDK。默认安装目录为 `%LOCALAPPDATA%\HybridAgent\hcu\697d3fb`。首次构建需要从 NuGet 恢复上游依赖。分发完整安装目录时一并保留 `LICENSE`、`THIRD_PARTY_NOTICES.md`、完整 `licenses` 目录及 `REVISION` 文件。

自定义安装位置时使用 `-Destination` 指定一个新目录，并将 `HCU_RUNTIME` 设置为该绝对目录。安装目录包含配套 Python SDK。桌面任务各自使用独立的 HCU 会话，守护进程在首次需要原生 AX 时启动。观察结果中的 `desktop_ax_status` 给出当前帧的可用性，`available` 表示窗口身份与本次桌面观测一致。

同一 Windows 会话的同一桌面同时只运行一个桌面任务。任务关闭时释放输入权，进程退出时由 Windows 回收。独立的 Win32 桌面拥有各自的输入权和 HCU 守护进程。鼠标动作使用截图内像素，发送前核对目标所在位置的窗口及其几何范围。

Windows 公共“打开／另存为”选择器可能已通过 UIA 回读到新文件名，而 Shell 仍保留旧选择。Agent 在这些窗口可读 AX，文件名输入和提交统一走真实键鼠。新建文件要求与覆盖确认冲突时，按 Esc 返回核查。保存结果从指定路径重新读取。

## 验收与证据

1. **协议与回归。** 重放合法／失效／未展示／跨场景引用、窗口和 DPI 变化、拖拽一端失败、Jev 异常概率及 `stop`。断言是否派发和定位次数。浏览器回归使用原有 MiniWoB 测试。模拟 provider 的断言与真实环境测试分别记录。
2. **三个角色的真实 API。** 在仓库父目录运行 `python -m hybrid_agent.model.terra_verification --smoke`。本机需要已登录 Codex app-server 且账号可用 `gpt-6.1-terra`。用随机色块图分别验证决策动作、像素是否命中、Jev 类型与标签是否正确。三个临时会话记录 `response_model/thread_id/response_id`。Terra 按相同协议代行 Jev，生产 Jev 的概率质量仍由生产部署验证。
3. **真实桌面端到端。** 用 `--task-mode desktop --verification-backend terra` 运行。纯视觉对照增加 `--disable-desktop-ax --disable-ocr-grounding`。记事本保存要逐字读回目标文件。资源管理器重命名要核对新旧路径。电子表格要读取指定单元格／公式。画图拖拽要检查保存图像中的实际线段。应用未能启动、只有派发回执、或只有模型自述时，应按实际情况记录阻塞或证据不足。
4. **入口与交付。** 分别运行 Studio 单次、批量、CLI 与显式浏览器入口。保留原始 `trajectory.json`、`events.jsonl`、实际产物和独立回读，核对所用模型、动作通道、输入次数及最终状态。纯文字结果附实际窗口、AX 或文件来源。各应用是否完成，以对应运行证据为准。

### 2026-11-24 实机验证记录

| 路径 | 独立检查 |
|---|---|
| 记事本，纯视觉与 HCU 两组 | 纯视觉组关闭 AX 和 OCR。HCU 组从 Windows“运行”冷启动记事本，在文件选择器用 GUI 保存。两组文件逐字读回均为 `桌面验收已完成`，SHA-256 相同。 |
| 资源管理器、Excel、画图 | 改名后旧路径消失，新文件内容一致。读取保存的 `.xlsx` 得到 `Sheet1!A1 = TERRA-AX-42`。画图在 HCU 缺失、OCR 关闭时完成拖拽，保存图像的连续 300 列均检测到笔迹。 |
| 计算器切换到记事本 | 原生计算器显示 `37 × 12 = 444`。核对切换到测试记事本的窗口身份和保存后的 `result.txt`，该文件恰含 `444` 三个字节。 |
| 系统设置关闭后重开 | Alt+F4 后原窗口从可见列表消失，Win+I 后出现新窗口。独立 UIA 检查确认设置主页。对同一张最终截图移除历史或互换快捷键，Terra 的判断均变为 `unknown`。 |
| 入口与兼容性 | Studio 单次和批量、两种 CLI 入口均实际生成文件。显式浏览器环境用独立无头 Chrome 和 Terra 点击一次 DOM 按钮，服务端收到一次可信点击，CDP 回读页面状态符合预期。 |

完整测试套件此次通过 62 项，包含真实 Chromium 的 MiniWoB++ 回归和启用真实 Terra 的报告核查。三个角色均使用授权的 `gpt-6.1-terra` 单独验证。上述结果覆盖列出的 Windows 应用和操作，生产三模型各自的准确率及延迟需在其接口就绪后另行测量。
