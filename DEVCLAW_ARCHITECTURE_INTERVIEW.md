# XClaw 架构与面试手册

> 项目定位：参考 pi-mono 的极简、透明、可组合理念，使用 Python/asyncio 独立实现的本地 Coding Agent 原型。
>
> 面试回答原则：明确区分“当前已实现”“原型实现”“生产化设计”。可以讲演进方案，但不要把尚未接入主流程的能力描述成已经在线稳定运行。

---

## 1. 一句话介绍项目

XClaw 是一个面向本地代码仓库的 Coding Agent Runtime。它把模型调用、ReAct 工具循环、文件/Git/Bash 工具、上下文与记忆、Local/Docker 执行环境、飞书消息入口、会话持久化和生命周期事件组合成一条完整链路，使模型能够在受控工作区内完成“理解需求—读取代码—修改文件—运行验证—输出结果”的闭环。

项目统一使用 XClaw；旧简历名称应同步更新。

---

## 2. 如何说明“实现状态”

面试中可使用下面三个标签：

- **已实现**：已接入当前主请求链路，并有代码支撑。
- **原型实现**：模块代码存在，但默认未启用、测试不足或未接入主链路。
- **演进设计**：为解决已知问题设计的生产化方案，当前没有完整实现。

项目当前状态：

| 能力 | 状态 | 说明 |
|---|---|---|
| ReAct 工具循环 | 已实现 | LLM 返回 tool calls 后执行并回填，直到输出最终文本或达到轮次上限 |
| 多工具调度 | 已实现 | LLM 提出串行/并行/依赖计划，代码校验后并发执行独立只读工具，副作用操作互斥 |
| OpenAI-compatible 模型调用 | 已实现 | 可通过不同 base URL 接入 OpenAI、DeepSeek、Qwen 等兼容服务 |
| 文件/Bash/Git/Markdown Skill | 已实现 | 包含文件、Git、Bash、7 个 Skill，以及资源读取/脚本执行入口；可用数量由权限配置决定 |
| 执行权限与后端 | 已实现 | view/edit/execute 权限预设独立于 Host/Docker 后端；Git/Bash 统一在 Docker 执行，工作区仍共享 |
| 飞书 WebSocket | 已实现 | 官方 SDK 长连接运行在线程中，消息桥接到 asyncio 主循环 |
| 多会话历史与重启恢复 | 已实现 | 私聊按用户、群聊按群保存 JSON；首次消息时惰性恢复 |
| 上下文压缩 | 已实现 | 完整请求按估算预算检查；历史按整轮压缩、本轮用户输入保留、工具组配对校验，仍超限时阻断发送 |
| KV 长期记忆 | 已实现 | 每轮完成后自动提取，按 session 单独持久化、恢复和重置 |
| 渐进式加载 | 已实现（Skill 内容） | 首轮仅发送 Skill 名称、简介和参数；调用后才返回完整 Markdown。普通工具发送名称、简介和参数 Schema，从不发送函数实现 |
| Planner/Queue/Worker | 原型实现 | 模块存在，但没有进入主要 IM 请求链路 |
| 重复调用拦截 | 演进设计 | 当前没有实现状态指纹和重复检测 |
| 任务评测与对照实验 | 已实现入口 | 24 个合成任务、独立验收、自定义仓库任务、四方案对照与报告；真实模型成绩需实际运行 |
| 任务执行记录 | 已实现 | run_id 关联主/辅助模型调用、工具状态耗时、依赖计划和压缩信息；不记录完整内容 |
| 独立任务工作区 | 已实现（可选） | 服务/Demo 创建任务副本与 diff；显式回写前检查冲突，默认不自动回写 |

---

## 3. 本地 Coding Agent 的完整架构

### 3.1 总体分层

```text
┌──────────────────────────────────────────────────────────────┐
│                         入口层                               │
│            CLI Demo / Feishu / QQ / future HTTP             │
└──────────────────────────────┬───────────────────────────────┘
                               │ IMMessage
┌──────────────────────────────▼───────────────────────────────┐
│                       应用编排层                             │
│ XClawServer                                                  │
│ 配置加载、Provider 注册、IM 初始化、会话路由、历史恢复/保存 │
└──────────────────────────────┬───────────────────────────────┘
                               │ user_input
┌──────────────────────────────▼───────────────────────────────┐
│                       Agent Runtime                          │
│ CodingAgent → Agent                                         │
│ ReAct 循环、轮次控制、消息编排、并行工具调用、生命周期事件  │
└──────────────┬──────────────────────────────┬────────────────┘
               │                              │
┌──────────────▼──────────────┐  ┌────────────▼────────────────┐
│          AI 层              │  │          Tool 层             │
│ AIClient                    │  │ ToolRegistry                 │
│ BaseProvider                │  │ File / Bash / Git / Skill    │
│ OpenAIProvider              │  │ JSON Schema / 参数反序列化   │
│ ChatMessage/Response 类型   │  └────────────┬────────────────┘
└──────────────┬──────────────┘               │
               │                              │
               │                 ┌────────────▼────────────────┐
               │                 │       Execution 层           │
               │                 │ SandboxRouter                │
               │                 │ Policy → Host / Docker       │
               │                 │ Docker(code execution)       │
               │                 └──────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────┐
│                     状态与上下文层                          │
│ 完整对话历史 / ContextCompressor / KV Memory / WorkingMemory│
│ sessions/*.json                                             │
└──────────────────────────────────────────────────────────────┘

横切能力：EventBus、Loguru、token 统计、重试、PM2 进程守护
```

### 3.2 各层职责

#### 入口与 IM Adapter

`IMAdapter` 将飞书、QQ 的平台特定事件统一为 `IMMessage`。上层 Server 只依赖标准字段，如 `message_id`、`chat_id`、`chat_type`、`sender_id` 和 `content`，不需要感知平台 SDK 的事件格式。

飞书 WebSocket SDK 使用独立线程运行。收到事件后，Adapter 使用 `asyncio.run_coroutine_threadsafe` 把协程安全地提交回主事件循环，避免 SDK 自己的事件循环与 Agent 的 asyncio 运行时冲突。

#### 应用编排层

`XClawServer` 负责：

1. 加载 YAML/环境变量配置。
2. 注册模型 Provider。
3. 初始化飞书或 QQ Adapter。
4. 创建默认 CodingAgent。
5. 注册 EventBus 生命周期日志。
6. 根据消息来源路由会话。
7. 在会话首次出现时加载 JSON 历史，在回复完成后保存历史。

它是 Composition Root，即所有模块被组合到一起的位置。

#### Agent Runtime

`Agent` 是核心状态机，承担：

- 构建当前请求消息。
- 携带历史向模型发起请求。
- 识别普通文本或工具调用。
- 执行工具并把观察结果回填给模型。
- 控制最大工具轮次。
- 写入最终对话历史。
- 发射 started、tool.executed、completed 等生命周期事件。

`CodingAgent` 是具体 Agent，通过系统提示词和默认工具集把通用 Agent 组合成编程助手。

#### AI 与 Provider 层

内部使用统一数据类型：

- `ChatMessage`：system/user/assistant/tool 消息。
- `ToolDefinition`：工具名、描述和 JSON Schema。
- `ToolCall`：模型给出的工具名和 JSON 参数。
- `ToolResult`：工具调用 ID 和执行结果。
- `ChatResponse`：文本、tool calls、finish reason 和 token usage。

`BaseProvider` 定义非流式 `chat` 和流式 `chat_stream`。`OpenAIProvider` 负责把内部类型转换成 OpenAI-compatible 请求，并把 SDK 响应还原为内部类型。上层 Agent 因而不依赖具体厂商 SDK 数据结构。

#### Tool 与 Skill 层

`Tool` 是对可调用函数的统一封装，包含名称、描述、参数 Schema 和执行函数。`ToolRegistry` 负责注册、发现、定义导出、参数解析和执行。模型每轮看到的是工具名称、简介和调用 Schema，而不是 Python 实现。

Skill 在启动时只读取名称和简介等元数据，调用后才读取完整 Markdown 流程。支持目录型 SKILL.md 与旧单文件格式；参考资料通过 read_skill_resource 按需读取，脚本通过 run_skill_script 提交固定 argv 请求，在 Docker 中执行。Word/Excel 技能包含可复用脚本和操作格式说明，覆盖文件创建、编辑与验证。资源与入口路径受目录边界校验，Skill 元数据不会扩大 ExecutionPolicy 权限。完整说明见 [Word / Excel 技能](docs/WORD_EXCEL_SKILLS.md)。

#### Sandbox 层

`ExecutionPolicy` 管理 view/edit/execute 权限预设以及工具的具体能力。Agent 私有的 `SandboxRouter` 先检查权限和参数，再选择实际后端：

- `HostBackend`：处理可信文件和提示词工具，路径访问受策略限制，禁止启动任意进程。
- `DockerSandbox`：执行 Git/Bash 的 `RunProcess / ProcessPlan` 请求；Git 使用 argv 并禁用 hooks，限制 CPU、内存、进程、网络和 Linux capabilities。
- `BaseSandbox / LocalSandbox`：只保留旧 API 兼容，已退出 CodingAgent 的主链路。

查看和编辑模式的 Docker 挂载只读，执行模式挂载可写。结构化文件工具会检查路径与权限；任意 Bash 内部的文件访问不能靠这些 Python 路径检查约束。工作区仍共享，没有任务副本与冲突检测回写。配置与完整边界见 [执行环境说明](docs/EXECUTION_ENVIRONMENTS.md)。

Docker 后端使用项目提供的 `Dockerfile.sandbox` 镜像，首次运行前构建：

```bash
docker build -f Dockerfile.sandbox -t xclaw-sandbox:latest .
```

#### 状态与记忆层

可以按三个层次理解：

1. **工作记忆**：单次 `run` 内的临时状态、中间结果和错误，任务开始时清空。
2. **会话历史**：用户输入和最终回复组成的多轮历史，参与后续 prompt，并保存到 session JSON。
3. **长期 KV 记忆**：从对话提取的用户偏好、项目约束、架构决策等稳定事实，以 key-value 形式注入后续 prompt。服务默认开启自动提取，按 session 原子持久化和恢复；调用方可关闭。

---

## 4. 服务启动流程

```text
main()
  → load_config()
  → XClawServer(config)
      → setup_logging()
      → setup_providers()
      → setup_im()
      → setup_agent()
          → CodingAgent()
              → AIClient()
              → SandboxRouter(Base/Local/Docker)
              → 注册文件、Git、Bash、Markdown Skill
      → setup_event_handlers()
  → server.start()
      → FeishuAdapter.start()
          → 获取 tenant token
          → 启动 SDK WebSocket 线程
      → 主事件循环等待消息
```

PM2 从外部启动 Python 进程，配置崩溃重启、最大重启次数、延迟重启、内存阈值和日志文件。PM2 是进程级可靠性机制，不负责恢复正在执行到一半的 ReAct 工具链。

---

## 5. 一次请求的完整流程

下面以飞书消息“修复登录接口的空指针并运行测试”为例。

### 5.1 接收与会话路由

1. 飞书通过 WebSocket 推送 `im.message.receive_v1`。
2. 飞书 Adapter 解析文本、用户、群聊、消息 ID 等字段，构造统一 `IMMessage`。
3. SDK 回调所在线程通过 `run_coroutine_threadsafe` 把 `_dispatch` 提交给主 asyncio 循环。
4. `IMAdapter._dispatch` 调用 Server 注册的消息处理函数。
5. Server 计算 session ID：私聊使用 `private-{sender_id}`，群聊使用 `group-{chat_id}`。
6. 如果 session 尚不存在，则创建 CodingAgent；如果对应 JSON 存在，则加载历史。

### 5.2 构建上下文

7. `Agent.run()` 清空本轮 WorkingMemory，并发送 `agent.started` 事件。
8. Agent 获取工具定义。当前默认获取全部注册工具；调用方也可以显式传入工具名子集。
9. Agent 构建当前 user message。若存在 KV 记忆，会先把它格式化后拼到当前输入前面。
10. `AIClient` 按以下顺序生成最终模型输入：system prompt → 历史消息 → 当前 user message。
11. ContextCompressor 在真正发送前估算 `system prompt + 会话历史 + 当前 ReAct 消息 + tool/skill 简介与 Schema` 的总输入 token。超预算时按完整轮次逐步压缩历史，再压缩本轮较早的完整工具组；原始本轮用户输入固定保留。超长工具结果有标记地保留首尾。最终长度与工具配对检查不通过时阻断发送并回滚临时状态。输入预算为：`(模型 context window - 最大输出 token - 安全余量) × 触发比例`。配置和完整规则见 [上下文管理说明](docs/CONTEXT_MANAGEMENT.md)。

### 5.3 ReAct 循环

12. Agent 调用 Provider，进入 Think 阶段。
13. 模型如果直接返回文本，说明本轮任务结束。
14. 模型如果返回 tool calls，Agent 进入 Act 阶段：
    - 一个工具直接执行。
    - 多个工具先校验依赖计划；独立只读调用可并发，写入及未知副作用互斥。
15. ToolRegistry 根据工具名查找 Tool，将 arguments JSON 解析为关键字参数，再调用同步或异步函数。
16. 文件工具检查 workspace 与能力策略；Bash/Git 构建进程请求并由 Docker 执行。
17. 工具执行结果被封装成 `ToolResult`，并发送 `tool.executed` 事件。
18. Agent 进入 Observe 阶段，严格按照协议追加：

```text
assistant: tool_calls=[read_file(...), search_in_files(...)]
tool: read_file 的结果
tool: search_in_files 的结果
```

19. 携带这些观察结果再次请求模型。模型可能继续读文件、编辑代码、运行测试，也可能输出最终总结。
20. 循环达到最大轮次时，Agent 强制停止工具调用并请求模型总结当前进度。

### 5.4 完成与持久化

21. 模型给出最终文本后，Agent 将本轮原始用户输入和最终回复写入会话历史。当前设计不保存中间工具轨迹，减少后续上下文噪声。
22. Agent 发送 `agent.completed` 事件。
23. 如果开启自动长期记忆，MemoryExtractor 会再次调用 LLM，从本轮对话提取稳定 KV；当前默认关闭。
24. Server 把会话历史写入 `sessions/{session_id}.json`。
25. 飞书 Adapter 将最终回复发送回原消息所在会话。

### 5.5 异常流程

- 模型请求失败：AIClient 最多重试 3 次，使用 1、2 秒指数退避，最终仍失败则向上抛出。
- 工具函数失败：Tool 捕获异常并把错误字符串作为 observation 返回，让模型有机会修正参数或换方案。
- 命令超时：Sandbox 终止子进程并返回超时结果。
- 达到最大工具轮次：请求模型输出阶段性总结。
- Server 处理失败：捕获异常并向用户返回错误信息。

生产化时还应增加 `agent.failed` 事件、错误分类、超时预算、可重试判断、幂等消息 ID 和失败现场持久化。

---

## 6. 高频项目问题与参考答案

### Q1：完整说明本地 Coding Agent 的整体架构

**概率：极高。**

参考回答：

> 我把项目分成入口适配、应用编排、Agent Runtime、模型适配、工具执行、状态记忆六层。飞书或 CLI 负责接收输入；Server 做配置、会话路由和生命周期管理；Agent 运行 ReAct 循环；AIClient 和 Provider 屏蔽不同 OpenAI-compatible 服务的差异；ToolRegistry 管理文件、Git、Bash 和 Skill；Sandbox 控制命令执行环境；历史和记忆层负责跨轮上下文及重启恢复。一次请求会不断经历模型决策、工具执行、结果回填，直到模型返回不含工具调用的最终文本。

追问时再展开消息协议、沙箱、会话恢复和并发控制，不要一开始陷入文件级细节。

### Q2：项目支持两类后端，两类后端怎么设计？如何统一不同模型后端的请求、响应和错误？

**概率：高，但问题有歧义。**

本项目有两组“后端”概念，要先向面试官确认：

#### 如果指执行后端

> 我把执行权限和执行位置分开：view/edit/execute 是权限预设，工具声明具体能力。Router 检查能力、参数和路径后，把文件工具交给 HostBackend，把 Git/Bash 请求交给 Docker。容器超时或取消会触发销毁，清理失败时禁止复用。当前仍使用共享挂载，严格文件隔离和冲突检测回写需要后续独立任务工作区。

#### 如果指模型后端

> 当前严格来说只有一个 OpenAI-compatible 协议实现，但可以通过不同 api_key、base_url 和 model 接入多个兼容厂商。BaseProvider 定义 chat/chat_stream；请求统一转换为 ChatMessage 和 ToolDefinition，响应统一为 ChatResponse、ToolCall 和 TokenUsage。AIClient 负责通用重试和历史，Provider 只负责协议转换。每个模型还配置 context_window，供发送前计算上下文预算。未来接 Anthropic 原生协议时新增 AnthropicProvider 即可，不改 Agent 循环。

错误处理应分层：

- Provider 层把 HTTP、鉴权、限流、超时、协议解析错误转换成统一异常。
- Client 层只重试超时、限流和 5xx 等瞬态错误，不重试 4xx 参数错误。
- Agent 层记录 run/tool/model 上下文，决定降级、终止或向用户说明。
- 重试采用指数退避加 jitter，并保留原始错误码和 request ID。

当前实现是捕获所有异常并统一重试，属于简化版本。面试时要主动说明生产化会按错误类型分类。

### Q3：工具怎么注册？注册、发现和调用流程是什么？

**概率：极高。**

参考回答：

> 工具有两种注册方式。第一种通过装饰器把函数包装为 Tool 并加入 ToolRegistry；第二种直接构造 Tool，用于批量注册内置文件、Git、Bash 和 Skill。注册时读取函数签名，生成 JSON Schema。请求开始时模型只看到名称、简介和调用参数，不会看到函数实现；模型返回工具名和 JSON 参数后，Registry 查找 Tool、解析参数并执行。Skill 也被注册成 Tool，但首轮只暴露简介，调用后才返回完整 Markdown 指南，随后模型再调用原子工具。这是两阶段的 progressive disclosure。

应主动说明的当前问题：项目启用了 postponed annotations，简单的 `inspect.signature` 可能拿到字符串注解，导致 `int/bool` 被错误生成成 `string`。正确方案是 `typing.get_type_hints()` 配合 Pydantic TypeAdapter，支持 Optional、Literal、Enum、List，并在执行前做参数校验。

### Q4：上下文压缩时，怎么评估请求有没有被压缩“踩坏”？

**概率：极高。**

不要只回答“看模型最终答案”，应该从结构、信息和任务三个层次评估：

1. **结构完整性**：不能把 assistant tool call 与对应 tool result 切开；不能留下无对应调用的 tool message。
2. **信息保持**：压缩前后的任务目标、用户约束、文件路径、修改范围、失败原因、未完成项必须一致。
3. **任务可继续性**：把压缩后的上下文交给 Agent，验证下一步动作是否与未压缩版本一致或等价。
4. **结果回归**：在固定任务集上对比成功率、工具调用数、重复读文件率、token 成本和最终 patch 正确率。

可以设计自动检查：

- 从原上下文和摘要分别抽取结构化 facts，计算关键字段召回率。
- 对比压缩前后模型的 next-action/tool-call 一致性。
- 在 SWE-bench 风格任务上做 A/B：无压缩、截断、滚动摘要。
- 保存压缩前后 trace，人工抽检失败案例。

当前实现使用中英文混合的近似 Token 估算，按模型窗口计算预算。历史从完整用户轮次处切割，本轮工具轨迹按完整调用组处理；发送前校验调用 ID 与结果配对。摘要请求分块且限制调用次数/超时，摘要失败或最终超预算时不提交临时压缩状态。预算与协议已有回归测试，摘要语义质量仍需任务评测，并应按 Provider 接入对应 tokenizer。

### Q5：举例说明如何解决多轮任务中的上下文膨胀

**概率：高。**

参考场景：

> 用户要求修改登录模块。Agent 前几轮读取了项目结构、认证代码和测试，随后编辑文件、运行测试、根据报错再次定位。每一次读文件和测试输出都可能有几千 token。如果完整保留，后续模型的大部分上下文会被旧代码和重复日志占用。我会保留最近若干轮原始消息，把较早内容压缩为结构化摘要，例如：任务目标、已检查文件及其版本哈希、已完成修改、测试失败原因、用户约束和下一步。原始大段文件内容不进入长期摘要，需要时根据当前文件重新读取。这样既减少 token，又避免让过时代码内容长期影响模型。

当前项目的持久历史只追加用户输入和最终回复，压缩后还包含摘要，不保存工具轨迹。单次长任务内已支持旧工具组摘要和工具结果首尾截断，保持本轮用户输入原文。主请求发送前重新检查预算；这仍不能替代原始执行轨迹归档与任务恢复检查点。

### Q6：任务摘要、文件摘要怎么治理？如何防止信息丢失？

**概率：高。**

建议把不同信息分开治理，而不是生成一段自由文本：

```yaml
goal: 修复登录接口空指针
constraints:
  - 不改变公开 API
decisions:
  - 在 service 层处理缺失用户
files:
  - path: src/auth/service.py
    content_hash: abc123
    summary: login() 在用户不存在时直接解引用
completed:
  - 增加 None 检查
failed_attempts:
  - 在 controller 返回默认用户，破坏语义
pending:
  - 运行认证模块测试
```

治理原则：

- 任务事实、用户约束、文件事实、过程笔记分字段存储。
- 文件摘要必须绑定 path、content hash、Git revision 和生成时间。
- 摘要不能代替源文件；做写操作前重新读取目标片段。
- 用户明确约束优先级高于模型摘要。
- 新摘要不是无限拼接，而是对旧摘要进行合并、去重和版本替换。
- 对关键数字、接口签名、未完成事项做规则校验。
- 压缩失败时保留原历史，不应生成空摘要后删除原文。

### Q7：分层记忆每层何时写入、何时召回、失败策略是什么？

**概率：极高。**

| 层级 | 写入时机 | 召回条件 | 失败策略 |
|---|---|---|---|
| Working Memory | 工具执行、计划步骤状态变化时 | 当前 run 内始终可用 | 丢失时可从当前消息和工具结果重建；不跨任务持久化 |
| Session History | 一轮用户请求成功结束时 | 同一 session 的后续请求 | 加载损坏时记录错误并使用新历史；写入使用临时文件+原子替换 |
| Rolling Summary | token 超阈值且找到安全切割点时 | 每次构建该 session 的 prompt | 摘要失败则保留原历史；空间不足时再降级为受控截断 |
| Long-term KV | 一轮结束后提取到稳定偏好、约束、决策时 | 与当前用户、项目和任务 scope 匹配时 | 提取失败不阻塞主回复；低置信度记忆不自动注入 |
| File/Repo Facts | 读取或分析文件后 | path/hash/revision 仍匹配时 | 版本不匹配立即失效，重新读取源文件 |

当前代码已经接入 Session History、上下文压缩和 KV 自动提取，服务及 Demo 使用一致的相关配置；Working Memory 的主链路使用深度仍有限。

### Q8：介绍任务恢复机制

**概率：高。**

当前实现应这样回答：

> Server 保存已完成对话和记忆；执行中任务另有轮次级 Checkpoint，保存工具消息、轮次、摘要、调度计划与各工具结果。服务和 Demo 支持 `/checkpoints`、`/resume <id>`，恢复时复用已完成结果，继续原任务副本。结果不确定的副作用调用先暂停核对，不直接重放。详见 [Checkpoint 说明](docs/CHECKPOINTS.md)。

当前实现与继续演进的边界：

- `run_id`、当前 round、pending tool calls、tool results、状态版本。
- 每一步执行后写 checkpoint，而非只在最终完成时保存。
- 已完成结果去重与不确定调用核对已接入；外部服务幂等键和发送事务尚未提供。
- 已有原子写与 schema version；不兼容配置拒绝恢复，没有通用旧版本迁移。
- 重启后显式恢复，根据工具性质决定只读重试或暂停核对，没有自动任务投递。

### Q9：重复调用拦截怎么判断重复状态？

**概率：中高；当前未实现，不要声称已经实现。**

参考回答：

> 当前版本只有最大工具轮次，没有做语义级重复调用拦截。如果继续完善，我会给每次工具调用计算指纹：`tool_name + canonical_json(arguments) + workspace_state_version`。在一个滑动窗口中，如果相同指纹连续出现，且对应 observation 没变化，就判定 Agent 陷入重复状态。对于 read_file，还会加入文件 content hash；对于 search，加入索引版本；对于写工具，必须使用幂等键并检查目标 diff。触发后先向模型注入“重复调用及已有结果”，再次重复则终止并输出诊断。

不能仅以“工具名和参数相同”判断重复，因为文件可能已经被其他工具修改，同一个读取调用可能获得新结果。

### Q10：Follow-up 阶段重复读文件从 60 次降到 0 次，是怎么优化的？

**概率：仅当简历写了该指标时才高；本项目当前不应使用这个数字。**

如果没有真实评测记录，最安全的回答是：

> 这个项目没有可靠数据证明 60 次降到 0 次，所以我不会把这个指标写进简历。对应的优化思路是给文件观察结果建立带版本的缓存：key 包含绝对路径、行范围和 content hash；Follow-up 继承上一个 run 的结构化工作集和文件版本。如果文件未变化，直接复用摘要或已读片段；文件被 edit、git checkout 或外部修改后使缓存失效。是否成功要通过 trace 统计重复 read_file 次数，而不是凭主观感受。

将次数降到绝对 0 通常也不是合理目标，因为写操作前重新读取文件可能是必要的安全验证。更好的指标是“无效重复读取率”。

### Q11：长期记忆保存了过时文件摘要或错误笔记，怎么检查和纠正？

**概率：高。**

参考回答：

> 我不会把文件摘要当作永久事实。每条文件记忆都要带 path、Git commit、content hash、来源工具调用和生成时间。召回时先比较当前文件 hash；不一致就标记 stale，不注入模型，并重新读取生成新版本。过程笔记要区分 observation 和 model inference：工具直接观察到的事实权重高，模型推断权重低。重要操作前仍以当前源文件、测试和 Git diff 为准。

### Q12：过时信息和错误信息分别怎么处理？如何判断一条信息应该弃用？

**概率：高。**

- **过时信息**：曾经正确，但版本或时间变化。通过版本号、hash、TTL、Git revision 判断；处理方式是失效并重新获取。
- **错误信息**：写入时就不正确。通过源数据冲突、测试失败、用户纠正或多个可靠来源交叉验证识别；处理方式是保留审计记录，但将状态标记为 superseded/invalid，并写入指向正确记忆的新版本。

弃用规则可以是：

1. 当前源文件与记忆绑定的 hash 不一致。
2. 工具观察与模型笔记冲突时，以工具观察为准。
3. 新的用户明确指令覆盖旧偏好。
4. 测试或编译结果否定了原过程假设。
5. 记忆超出其 user/project/branch scope。

### Q13：模型幻觉导致错误记忆，后续怎么纠正？

**概率：高，与 Q11/Q12 可合并回答。**

核心原则是“模型不能给自己的推断盖事实章”：

- 记忆记录 `source`：user、tool、model_inference。
- 记录 `confidence`、`scope`、`created_at`、`version`。
- 只有用户明确事实或工具验证事实可以自动进入高可信长期记忆。
- 模型推断进入候选区，需要后续工具验证。
- 召回时将事实和推断分栏展示给模型。
- 用户纠正时写新版本并 tombstone 旧版本，不直接静默覆盖，便于审计。
- 涉及代码事实时，源文件和测试永远是最终依据。

### Q14：多智能体旅行规划助手如何拆解任务？

**概率：对当前项目低。**

这不是 XClaw 项目的功能。如果面试官问错项目，先纠正：

> 我的 XClaw 是单 Agent Coding Runtime，目前没有实现多智能体旅行助手。不过如果讨论通用拆解，我会按航班、住宿、景点、预算约束分别产生候选，再由协调 Agent 做约束合并和冲突消解。

不要为了迎合问题把自己的单 Agent 项目说成多 Agent。

如果延伸讨论，多 Agent 拆解需要明确：共享状态、任务依赖 DAG、预算、超时、冲突解决、结果验证和失败降级。能并行的是独立信息获取，最终路线整合必须由一个确定的协调步骤完成。

### Q15：规划时用户输入不足怎么办？

**概率：中高。**

参考回答：

> 我先区分缺失信息是否会实质改变结果。仓库路径、目标行为、安全边界等关键字段缺失时，需要向用户澄清；低风险细节可以采用项目惯例，并在最终结果中显式说明假设。对于结构化前端，可以在提交前完成必填和格式校验；对于对话式 Agent，可以让 Planner 输出 missing_fields 和 blocking_questions。原则是一次集中询问最少的阻塞问题，避免每执行一步就打断用户。

Coding Agent 的具体例子：用户说“把登录改快点”，需要确认性能症状或目标指标；但测试命令未说明时，可以先读取 README、CI 配置和 pyproject 自动发现。

### Q16：介绍你对 Agent Harness 的理解

**概率：极高。**

参考回答：

> 模型只是生成下一步动作的决策器，Agent Harness 是让模型能够稳定完成真实任务的运行时脚手架。它负责组织 system prompt 和上下文、定义工具协议、执行 ReAct 循环、管理状态和记忆、限制权限和资源、处理重试与超时、记录 trace、支持暂停恢复，并通过评测判断系统是否真的变好。XClaw 中 Agent、AIClient、ToolRegistry、Sandbox、Session、EventBus 合起来就是 Harness。模型可以替换，但 Harness 决定任务能不能安全、可重复、可观测地完成。

可以概括为：

```text
Agent 能力 ≈ Model 能力 × Context 质量 × Tool 可靠性 × Harness 控制能力
```

### Q17：RAG、Memory、Context Engineering 各解决什么？如何组合？

**概率：极高。**

#### RAG

从外部知识源按当前问题检索相关信息，主要解决模型参数中没有、太旧或属于私有仓库的信息。Coding Agent 中，文件搜索、符号索引、README/文档检索都可以视为广义 RAG。

#### Memory

保存跨步骤或跨会话积累的状态和经验，主要解决“之前发生过什么、用户偏好什么、任务做到哪里”。它强调时间连续性和主体/session scope。

#### Context Engineering

决定在某次模型调用中放入什么、以什么顺序和格式放入、删除或压缩什么。它包含 system prompt、历史、记忆召回、检索结果、工具 Schema、预算分配和摘要策略。

三者关系：

```text
RAG：从外部世界取候选信息
Memory：从过去交互取候选信息
Context Engineering：选择、排序、压缩并组装这些信息给模型
```

在 XClaw 中：文件搜索和读取提供仓库事实；Session/KV 提供历史状态；AIClient/Compressor 把 system prompt、历史、记忆、当前问题和工具定义装入有限上下文。当前没有向量数据库，因此不要把项目描述成已经实现完整向量 RAG。

### Q18：介绍整个评测过程

**概率：极高。**

当前真实回答：

> 当前测试覆盖 Tool、Memory、MemoryExtractor、Context，以及执行策略、请求路由和 Docker 生命周期。真实子进程测试验证输出限制与超时，Docker CLI 使用模拟测试；真实容器测试需要显式启用。已有 Agent 主循环与飞书文件链路回归测试，另提供 24 个合成任务、自定义仓库任务及独立验收入口；真实模型成功率尚需实际运行评测。

如果介绍完整评测设计，可以分四层：

1. **组件测试**：Schema、路径越界、超时、压缩切割点、记忆失效、Provider 响应解析。
2. **轨迹测试**：给 Mock Provider 固定 tool calls，验证消息顺序、最大轮次、并行行为、错误重试。
3. **任务评测**：准备 bug fix、功能实现、代码解释、测试修复等真实仓库任务，检查 patch 和测试结果。
4. **非功能评测**：token、延迟、工具调用次数、重复调用率、恢复成功率、安全攻击用例。

建议核心指标：

- Task Success Rate / Tests Pass Rate。
- 正确 patch 比率和无关改动量。
- 平均 token、模型调用次数、工具调用次数、P50/P95 延迟。
- 压缩后关键事实召回率和 next-action 一致率。
- 越界访问拦截率、危险命令拦截率。
- 断点恢复成功率、重复副作用率。

评测必须固定模型版本、temperature、代码基线和任务集，多次运行报告均值与方差。只有建立 baseline 和 A/B 对比后，才能在简历中写“降低多少”“提高多少”。

### Q19：平时如何使用 Codex？一个需求如何完成最终开发？

**概率：高，尤其项目是 AI 辅助开发时。**

参考回答：

> 我把 Codex 当作有工具能力的协作开发者，而不是代码生成器。拿到需求后，我先明确目标、验收标准和不能改的边界；让它读取仓库规则、入口和相关测试，形成对现状的证据；复杂任务再拆成可验证的小步骤。实施时要求最小改动，每完成一部分就运行相关测试和静态检查。最后查看 diff，重点检查异常路径、并发、权限、配置和是否引入无关修改，再补测试和文档。对于我不了解的生成代码，我不会直接接受，而是要求解释调用链，并通过源码、运行结果和测试交叉验证。

完整流程：

```text
需求澄清
  → 定义验收标准和风险边界
  → 读取 AGENTS/README/配置/入口/测试
  → 搜索调用链并建立现状模型
  → 制定最小实现计划
  → 分步修改
  → 单元测试/集成测试/静态检查
  → 审查 Git diff 和异常路径
  → 修正问题
  → 输出变更、验证证据和剩余风险
```

如果被追问“项目是 vibe coding 吗”，可以回答：

> AI 确实提高了编码速度，但架构取舍、边界定义、代码验收和问题归因由我负责。我能从入口讲到请求、工具、沙箱和持久化链路，说明压缩与调度的取舍，并用执行记录、独立验收和失败案例验证行为。我的目标不是证明每行代码都手写，而是证明我对系统行为和工程风险负责。

---

## 7. 额外高概率追问

### Q20：为什么自己实现，不直接用 LangChain/LangGraph？

> 这个项目的目标不是追求功能数量，而是理解 Agent Runtime 的关键机制。自己实现后，我能直接控制消息协议、工具结果顺序、上下文压缩、状态持久化和沙箱边界，也更容易定位失败属于模型、工具还是 Harness。生产项目是否使用框架要看需求；如果需要复杂 DAG、人工审批和成熟生态，我会考虑 LangGraph，但仍需要理解底层，否则出现上下文污染和重复副作用时很难调试。

### Q21：为什么只持久化用户输入和最终回答，不保存工具轨迹？

> 优点是历史小、噪声少，不会把大量过时文件内容和测试日志带入下一轮。缺点是无法恢复执行到一半的任务，也丢失了调试审计信息。更合理的生产设计是把“模型上下文”和“审计 trace”分开：prompt 历史只保存结构化摘要和必要事实，完整工具轨迹单独持久化用于恢复、评测和排错。

### Q22：多个工具什么时候可以并行？

> 只有相互独立且没有读写冲突的工具才应并行，例如读取两个文件。当前在同轮多个工具调用后增加一次独立的 LLM 调度请求，模型返回串行、并行或依赖计划。代码检查调用 ID 和循环依赖，按拓扑分批执行；工具还必须明确声明 parallel_safe，且能力限于只读/提示词，才能并发。写入、进程、MCP 和文件交付保守互斥；前置失败会跳过依赖调用，无效计划退回串行。同文件交付有固定前置规则，但这还不是完整的自动数据流分析，也没有跨会话的资源锁。具体配置见 [工具调度说明](docs/TOOL_SCHEDULING.md)。

### Q23：你的沙箱安全吗？

> HostBackend 与 ExecutionPolicy 是应用策略边界，DockerSandbox 提供进程环境隔离。Docker 配置了 CPU、内存、PID、禁网、capability 和 no-new-privileges 限制，但执行模式仍共享可写工作区。Python 文件路径策略不能约束 Bash 内部访问；更严格的方案是在独立任务工作区中执行，并检测 diff 回写冲突。

### Q24：多会话真的隔离吗？

> 每个私聊用户或群聊拥有独立 Agent、ToolRegistry、SandboxRouter 和 JSON 历史；同一 session 已通过消息锁串行执行，文件工具绑定 Agent 私有工作区。可启用独立任务副本，不直接修改原目录；显式回写前比较基线哈希并协调 XClaw 写入。外部编辑器写入与多文件断电事务仍需另外处理。

### Q25：EventBus 带来了什么？

> 它把 Agent 核心逻辑和日志、指标、审计等观察者解耦。Agent 只发布 started、tool.executed、completed，Server 决定如何处理。新增 metrics 或 trace exporter 时不需要修改 Agent 循环。当前实现仍会等待 handlers 完成，所以“异步 handler”不等于完全非阻塞；对于慢消费者应改为队列或后台任务，并处理背压和事件丢失。

### Q26：如何防止模型无限循环？

当前实现有最大工具轮次。完整设计还包括：

- 总 token、总时间和总成本预算。
- 相同状态/调用指纹检测。
- 无进展检测，例如连续多轮 workspace diff 和 observation 都不变化。
- 每类工具调用上限。
- 模型自评进度不能作为唯一依据，必须结合外部状态。
- 超限后输出已完成、失败原因、剩余步骤，而不是简单返回超时。

### Q27：为什么工具异常返回字符串，而不是直接抛出？

> 可恢复的业务错误作为 observation 返回，模型能看到“文件不存在”或“测试失败”并调整计划；框架错误、权限错误、状态损坏等不可恢复错误应抛出并终止。当前 Tool 捕获所有异常过于宽泛，生产化要使用结构化 ToolResult，例如 success、error_code、retryable、stdout、stderr 和 metadata，让 Agent 能区分可恢复和不可恢复错误。

### Q28：上下文压缩和长期记忆有什么区别？

> 压缩的目标是把同一个会话的旧过程放进有限 token 窗口，强调任务连续性；长期记忆的目标是跨任务保留稳定事实，强调复用价值和作用域。测试日志适合被压缩但不应进入长期记忆；用户编码偏好适合长期记忆；当前文件内容通常两者都不应该长期保存，而应按版本重新读取。

### Q29：项目当前最需要重构的地方是什么？

> 下一步重点是运行真实仓库任务评测，分析压缩与调度收益；完善结构化工具错误、记忆来源与版本治理，以及任务副本的多轮继续执行体验。现有 run_id 执行记录和独立验收可支撑问题归因。

---

## 8. 当前代码中需要能主动承认的问题

这些不是“项目失败”，而是展示工程判断力的机会：

1. `config.yaml` 中存在疑似真实凭据，应立即轮换并改为环境变量。
2. ContextCompressor 已接入 Server；当前使用保守 token 估算，生产环境仍应接入模型官方 tokenizer。
3. KV Memory 已开启自动提取并按 session 持久化；后续还需增加来源、版本、置信度和过期治理。
4. Skill 完整内容采用渐进式加载；普通工具仍会发送全部名称、简介和 Schema，可进一步增加工具路由缩小候选集。
5. 工具数量随 Skill 与权限配置变化，应通过 registry 动态统计，不应在简历中固定写旧数量。
6. postponed annotations 的 Schema 问题已通过 get_type_hints 修复，并加入工具参数类型校验。
7. 流式 tool-call 索引与类型聚合问题已修复，并有多技能调用和 Provider 分片回归测试；真实模型行为还需端到端评测。
8. `system_prompt`、`max_tool_rounds` 等配置已传入 Agent，并有服务/Demo 回归测试。
9. default provider 与 model 已按同一个 ProviderConfig 选择。
10. 文件、Git、Bash 已进入能力策略与请求路由；Git 在容器内执行并禁用 hooks；独立任务副本可配置启用，默认共享模式仍需调用方协调。
11. Registry、SandboxRouter 与文件工具策略已按 Agent 隔离，不再覆盖模块级 workspace root。
12. 同一 session 已串行执行；跨会话共用原目录时应启用任务副本或由调用方协调。
13. 历史与 KV 已采用原子 JSON 替换；两份文件不构成联合事务，也不是任务续执行检查点。
14. Planner、MessageQueue、WorkerPool 没接入主链路。
15. 已有主循环、IM 文件链路和评测执行器回归；真实模型/Docker 的验证需实际可用环境。

面试表达模板：

> 这是我为了理解 Agent Harness 而实现的可运行原型，核心链路已经打通。复盘时我发现它离生产级还差几项：运行时状态隔离、结构化错误、压缩评测和安全策略。我能说明这些问题产生的原因以及下一版的改造方案。

---

## 9. 60 秒项目介绍模板

> XClaw 是我参考 pi-mono 思路实现的 Python 本地 Coding Agent。核心是一个异步 ReAct Runtime：模型决定调用文件、Git、Bash 或 Markdown Skill，ToolRegistry 校验参数，再把 observation 回填给模型。执行层把 view/edit/execute 权限预设与 Host/Docker 后端分开，文件工具绑定工作区策略，Git/Bash 统一提交进程请求。入口侧接入飞书 WebSocket，并按会话持久化历史与 KV 记忆；上下文层根据模型窗口和完整请求预算触发滚动压缩；Markdown Skill 使用 progressive disclosure，只在模型选择后加载完整流程。

---

## 10. 3 分钟项目介绍模板

> 我做这个项目的目的，是理解一个 Coding Agent 除了模型以外还需要哪些 Harness 能力。整体分成六层。
>
> 第一层是飞书和 CLI 入口。飞书官方 SDK 的 WebSocket 跑在独立线程，我把平台事件转换成统一 IMMessage，再桥接回 asyncio 主循环。第二层是 Server，它负责 Provider、Agent、会话和历史。私聊按用户、群聊按群创建 Agent，完成一轮后写入 JSON，重启后惰性加载。
>
> 第三层是 Agent Runtime。每轮先构建 system prompt、历史和当前输入，调用模型。如果模型返回工具调用，就通过 ToolRegistry 找到工具，解析 JSON 参数并执行；多个调用经依赖校验后仅并发安全读取，写入互斥；无效计划退回串行。结果按 assistant tool_calls 后跟 tool results 的协议顺序回填，然后继续下一轮。没有工具调用就结束，最大轮次用于避免无限循环。
>
> 第四层是模型适配。BaseProvider 统一 chat 和 stream 接口，内部用 ChatMessage、ChatResponse 和 ToolCall，当前通过 OpenAI-compatible 协议接多个服务。第五层是工具和执行环境，有文件、Git、Bash 和 Markdown Skill；Bash 可以切换 Local 或 Docker 后端。第六层是状态和上下文，我实现了会话历史、Working Memory、滚动摘要和 KV 提取原型。
>
> 复盘这个项目时，我认为几个关键问题比功能数量更重要：一是压缩后如何验证任务没有被踩坏；二是同轮工具并行如何判断读写冲突；三是 Local/Docker 到底提供什么安全保证；四是会话历史隔离不等于运行时全局状态隔离。压缩、KV、工具类型校验和会话锁已经接入；新增任务评测、执行记录与可选任务副本。实际质量提升应以真实模型对照结果为依据。我会把这些明确当作原型限制，而不是包装成生产能力。

---

## 11. 面试前建议完成的最小准备

### 必须处理

- 立即轮换配置文件中出现过的所有密钥。
- 用 `config.example.yaml` 和环境变量替代真实配置。
- 统一项目名 XClaw/XClaw。
- 把工具数量从 11 改为真实数量，或明确统计口径。
- 将“渐进式加载”准确描述为 Markdown Skill 的 progressive disclosure；不要声称普通工具 Schema 也按需加载。

### 最值得补的三个实现

1. 为已接入的上下文压缩选择匹配模型的 tokenizer，并运行长对话任务对照实验。
2. 工具类型推导已有修复；继续用结构化错误完善异常语义。
3. 私有 workspace 与 per-session lock 已实现；启用独立任务副本并验证冲突回写。

### 最值得准备的现场演示

选择一个很小的真实 bug：

1. 飞书发送修复请求。
2. Agent 搜索并读取相关代码。
3. 精确编辑文件。
4. Bash 运行测试。
5. 飞书收到修改摘要和测试结果。
6. 重启进程后追问“刚才修改了什么”，展示会话恢复。

演示时同时展示 trace：模型轮次、工具名、耗时、token、最终 diff。一个稳定的小闭环比展示大量未完成模块更有说服力。

## 12. 任务评测与工作区隔离

实际命令、指标口径和限制见 [任务评测](docs/EVALUATION.md) 与 [任务工作区](docs/TASK_WORKSPACES.md)。
内置任务自检只证明验收脚本有效；简历中的成功率、Token/延迟变化必须来自实际模型运行报告。
