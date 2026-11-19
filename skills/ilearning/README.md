# iLearning 自动完成 Skill

帮用户自动完成 iLearning（华为学习平台）课程。全程在**用户自己的 Chrome**（通过 playwright-cdp 连接）里操作，课程记到用户自己的账号。

## 目录结构

```
skills/ilearning/
├── SKILL.md                   # skill 说明：指导 agent 如何编排（navigate → 执行脚本 → 报告）
├── scripts/
│   └── complete_course.js     # 核心脚本：自包含，由 Python 版逐行翻译
└── README.md                  # 本文件
```

- `complete_course.js` 封装了全部刷课逻辑：扫目录、逐章处理视频/文档/文本/测验/考试/问卷、验证绿勾、进度统计。
- `SKILL.md` 只做编排，**不让 agent 自己现编操作**——它只负责打开课程页、执行脚本、报告结果。

## 原理（为什么课程记到自己的账号）

agent 内置的 playwright MCP 直接操作用户本人日常用的 Chrome，所以登录态就是用户自己的账号（不需要传 cookie、不需要扩展、不需要服务机）。脚本用 `browser_run_code_unsafe` 在 Playwright 进程里跑，直接操作这个 Chrome。

## 安装

把整个 `ilearning` 文件夹复制到 agent 的 skills 目录，然后重启 agent：

- Claude Desktop：`C:\Users\<用户>\AppData\Roaming\Claude\skills\`（或 `~/.claude/skills/`）
- 其他 agent：按其 skill 加载目录（通常是 `<config>/skills/`）

## 使用

对 agent 说：

> 帮我学完 https://ilearning.huawei.com/course/100000/application-learn/CNE202308101128048-734?...

agent 会：

1. 用 `browser_navigate` 打开课程页；
2. 若未登录，提示用户本人登录一次（之后持久化）；
3. 读 `scripts/complete_course.js` 并用 `browser_run_code_unsafe` 执行；
4. 脚本从头跑完课程，返回 `{ ok, completedAfter, total, remaining, failed }`，agent 如实汇报。

## 本地验证

本仓库带一个 node 测试 harness，用服务机 `.profile` 登录态跑同一份脚本：

```sh
node scripts/test-skill.cjs <courseUrl>
```

已验证：目录扫描、完成检测、主流程、报告均正确（一门 4 章课程报告 `完成 4/4`）。

## 已知限制

- 视频章节：平台有"真实观看时长"防作弊，快进未标记完成时脚本会自动降 1x 重播；若仍无绿勾属正常限制。
- 测验/考试：依赖内网 Qwen 服务（脚本内已配好地址/key）。
