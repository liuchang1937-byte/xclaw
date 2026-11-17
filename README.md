# XClaw

基于 Python / asyncio 的 Coding Agent 原型，包含工具调用、文件编辑、Git、Bash、
Markdown Skill、上下文压缩、会话记忆，以及飞书、QQ 和个人微信入口。

```powershell
uv sync --group dev
uv run xclaw --config config.yaml --demo
```

模型和 IM 凭据在本地配置中设置。进程工具需要 Docker，并预先构建执行镜像：

```powershell
docker build -f Dockerfile.sandbox -t xclaw-sandbox:latest .
```

QQ 可选 WebSocket 直连或 HTTP 桥接，个人微信通过 iLink ClawBot 扫码绑定。
配置和启动方式见 [QQ 与微信接入](docs/QQ_WEIXIN.md)。

正常服务模式支持按间隔或每天定时运行任务，可以读取指定网址、生成报告并通过 SMTP 发邮件。
配置和运行记录见 [定时任务](docs/SCHEDULED_TASKS.md)。

执行权限可配置为 `view / edit / execute`，后端为 Host / Docker。
默认 `execute`，独立文件删除工具默认关闭；Docker 仍共享指定工作区。
完整配置、边界和验证方法见 [执行环境说明](docs/EXECUTION_ENVIRONMENTS.md)。

IM 私聊按平台和发送者分配持久工作区。群聊中文件按发送者和群隔离。参见
[聊天用户工作区](docs/EXECUTION_ENVIRONMENTS.md#聊天用户工作区)。

已提供目录型 Word / Excel Skill，支持 `.docx` / `.xlsx` 的读取、创建、编辑、模板填充和数据合并。
本地安装 `uv sync --extra office --group dev`，Agent 执行需重建沙箱镜像并重启。
目录、调用方法及限制见 [Word / Excel 技能说明](docs/WORD_EXCEL_SKILLS.md)。

上下文发送前会检查完整请求的估算预算，按对话轮次压缩历史，并保留本轮用户输入。
超长工具结果会标记截断；仍然超限则明确报错。配置与保留策略见
[上下文管理说明](docs/CONTEXT_MANAGEMENT.md)。

复杂聊天任务可以用 `/task <目标>` 启动，各子 Agent 使用独立上下文，支持依赖调度、
验收与续跑。使用方式见 [子 Agent 任务](docs/SUBTASKS.md)。

支持接入本地 stdio 和远程 Streamable HTTP MCP 工具服务，配置与示例见
[MCP 接入说明](docs/MCP.md)。

同一轮多个工具支持 LLM 判断串行、并行及前置依赖，代码校验计划并限制副作用。
独立只读工具可并发，写入与未知副作用保持互斥；配置和边界见
[工具调度说明](docs/TOOL_SCHEDULING.md)。

简单 Excel 数据表可用可信的本地 `create_excel` 工具创建，无需 Docker。
飞书机器人支持把生成的 Excel / Word 等最终文件作为附件发回原会话，并处理 DeepSeek
异常工具调用文本。启动准备、文件上传权限与交付流程见
[飞书文件交付说明](docs/FEISHU_FILES.md)。

```powershell
uv run pytest -q
```

项目架构与面试说明见 [架构手册](DEVCLAW_ARCHITECTURE_INTERVIEW.md)。
当前功能、实现原理、完整流程与逐项面试问答见
[项目功能与面试详解](docs/PROJECT_FEATURES_IMPLEMENTATION_INTERVIEW.md)。

默认评测最终方案“摘要＋依赖调度”，提供 24 个内置任务、独立验收和自定义仓库任务入口。
每个任务记录 run_id、模型与工具耗时、辅助调用用量、依赖计划及压缩结果，输出逐次结果和报告。
开始前可运行无模型调用的夹具自检：

```powershell
uv run python -m xclaw.evaluation.runner --self-check
```

真实模型评测命令、统计口径和限制见 [任务评测说明](docs/EVALUATION.md)。
支持独立验收、LLM-as-Judge 四维质量评分、工具/上下文组件统计、配对回归和人工校准。
对既有产物可直接评分，无须重跑任务；命令和指标口径见
[多层质量评测](docs/QUALITY_EVALUATION.md)。
新增效果、效率、鲁棒性、过程质量四维评测，提供 20 题分层集（60/20/10/10）、
全量/冒烟入口、确定性过程规则、失败归因、离线重算和配对回归门禁：
以下命令仅运行最终方案，冒烟为 3 次任务尝试，全量三次重复为 60 次。

```powershell
uv run python -m xclaw.evaluation.runner --dataset balanced --self-check
uv run python -m xclaw.evaluation.runner --dataset balanced --mode smoke
uv run python -m xclaw.evaluation.runner --dataset balanced --change prompt --repeats 3
```

指标、版本隔离、反馈导入和门禁用法见 [四维评测与优化闭环](docs/FOUR_DIMENSION_EVALUATION.md)。
后两条命令使用真实模型与 Docker；自检仅检查夹具，不能当作 Agent 成功率。

已完成 24 个合成任务、四方案、三次重复的 288 条真实模型有效尝试。
实测结果、失败复盘与数据审计见 [真实实验报告](docs/REAL_EXPERIMENT_REPORT.md)，
可用于简历的限定表述见 [简历版本](docs/RESUME_PROJECT.md)。
服务和 Demo 可配置独立任务副本，完成后生成 diff，显式回写前检查冲突；
配置和命令见 [任务工作区说明](docs/TASK_WORKSPACES.md)。

内置文件编辑使用原子替换；服务和 Demo 默认逐轮保存任务 Checkpoint，支持
`/checkpoints` 与 `/resume <id>`。已完成工具复用结果，不确定的副作用调用暂停等待核对。
SDK 用法、持久化边界与中断恢复限制见 [Checkpoint 说明](docs/CHECKPOINTS.md)。
