# AgentBench FC 接入

项目增加 `examples/agentbench_fc.py`，对接官方 AgentBench FC / AgentRL Controller 的 HTTP 协议。公开 Benchmark 独立写入 `.xclaw/evaluations/agentbench-fc/`，不混入产品合成回归集。

## 评测边界

当前模式为 `xclaw_client_external_execution`：XClaw 的 AIClient 负责模型调用、摘要压缩、重试和 token/耗时记录；官方 Controller/Worker 提供任务描述、工具定义、执行动作和最终 reward。每个样本创建独立 AIClient，避免跨样本历史泄漏。模型凭据仅由本地 config 读取，不传入 Worker。

它评测“XClaw 模型客户端和上下文管理在 AgentBench 环境中的交互表现”。它不执行本地 CodingAgent 的文件/Bash 工具，也不经过本地依赖调度器，因此不能视为完整 `summary_scheduled` 产品方案的成绩。manifest 明确记录 `dependency_scheduler_evaluated=false`。

官方当前 OS Worker 要求每轮一个工具，而且只处理第一个调用。不能通过修改官方执行循环来制造并行收益并继续声称标准 AgentBench 成绩。依赖排序、多个工具并行和副作用互斥，继续使用项目专项压力集和完整产品任务验证。

## 核对的官方版本

- [AgentBench FC](https://github.com/THUDM/AgentBench/tree/d1e4a10db08c87075c78972e48ecc182be03e2d5)：`d1e4a10db08c87075c78972e48ecc182be03e2d5`。
- [AgentRL Controller 客户端协议](https://github.com/THUDM/AgentRL/blob/6a73409d31ba695d383b978a8ad3ef400d90c054/eval/src/agentrl/eval/session/controller.py)：`6a73409d31ba695d383b978a8ad3ef400d90c054`。
- [官方计分规则](https://github.com/THUDM/AgentRL/blob/6a73409d31ba695d383b978a8ad3ef400d90c054/eval/src/agentrl/eval/session/metric.py)：成功率使用 reward 是否等于 1；COMPLETED 本身不等于成功。

源码已下载到 `.xclaw/benchmarks/AgentBench` 和 `.xclaw/benchmarks/AgentRL`，未安装它们的依赖到 XClaw 虚拟环境。适配器只依赖项目已有的 httpx、Pydantic 和模型客户端。

FC 和旧版 v0.2 的协议不同。本适配器不兼容旧版 AgentClient.inference(history) 文本接口。

## 启动官方 OS 环境

以官方仓库的 Linux 网络部署方式为基准；Windows 建议在支持 Docker 的 Linux/WSL 环境部署服务。本地 Python 客户端通过 Controller 地址连接，可与服务不在同一台机器。宿主网络、Docker socket、Redis 地址需与部署位置一致，不应照搬为 Windows 原生网络配置。

2026-11-02 已恢复 Docker Desktop Linux 引擎，并增加下述隔离桥接网络部署，避免依赖 Linux 宿主网络。官方任务实测结果单独保存，不以本地协议测试代替分数。

本项目的 Docker Desktop 部署入口（在 XClaw 根目录）：

```powershell
docker compose -f configs/agentbench-fc.compose.yaml build os-worker
docker compose -f configs/agentbench-fc.compose.yaml up -d
```

此前仍需按下文构建实际题目需要的 `local-os/*` 镜像。桥接配置只修改服务发现地址、Redis 地址、Worker 并发和启动包装；官方题目、工具与评分代码保持原样。Controller 仅映射到本机 `127.0.0.1:5020`。启动包装直接使用 Python 模块，避免 Windows checkout 的 CRLF shebang 导致 shell 入口失败。

本次真实部署还发现 Worker 与浮动最新版 aiodocker 的 exec timeout 类型不兼容，因此
`configs/agentbench-worker.compat.Dockerfile` 固定 agentrl-worker=0.4.0、aiodocker=0.24.0、aiohttp=3.11.18。
已完成官方 OS 索引 0、1、2 的首轮冒烟，三个 reward 均为 1；完整分母、初始环境故障、用量和局限见
[2026-11-02 官方环境实测](AGENTBENCH_SMOKE_20261102.md)。

准备环境的官方命令，工作目录为 AgentBench checkout：

```bash
git checkout d1e4a10db08c87075c78972e48ecc182be03e2d5
docker build -t local-os/default -f data/os_interaction/res/dockerfiles/default data/os_interaction/res/dockerfiles
docker build -t local-os/packages -f data/os_interaction/res/dockerfiles/packages data/os_interaction/res/dockerfiles
docker build -t local-os/ubuntu -f data/os_interaction/res/dockerfiles/ubuntu data/os_interaction/res/dockerfiles
docker compose -f extra/docker-compose.yml up -d controller redis os_interaction-std
```

先仅启动 OS，避免拉起全套 WebShop、Freebase 等额外环境。真实评测前保存 Controller、Worker、Redis、OS 镜像的 digest 和 Worker 实际配置；官方 Dockerfile 使用的 `agentrl-worker` 和镜像标签也需要固定。Git 提交相同不代表运行中的镜像和依赖已固定。本适配器 `environment_verified=false`，不会把手写的版本备注当作自动验证通过。

## 检查连接和小样本运行

在 XClaw 项目根目录运行，无需将官方源码放入 Python 导入路径：

```powershell
# 仅查询官方样本索引，不调用模型、不启动样本
.venv/Scripts/python.exe examples/agentbench_fc.py --check --task os-std

# 连通后，默认按官方索引列表选择前3题，各运行1次
.venv/Scripts/python.exe examples/agentbench_fc.py --task os-std --limit 3 --repeats 1 --config config.yaml

# 对比时使用相同的明确索引、模型、预算和服务环境
.venv/Scripts/python.exe examples/agentbench_fc.py --task os-std --indices <官方索引1> <官方索引2> --repeats 3 --max-tokens 2048 --context-window 8192 --environment-version '<部署镜像digest和配置版本>'
```

`--controller` 默认 `http://127.0.0.1:5020/api/`。`--split` 默认 unspecified，仅记录所选择的真实 split，不能通过传入 test 把任意题目变成标准测试集。`--limit` 小样本不是全量 Benchmark；其他 FC 文本任务可通过 `--task` 指定，但本次协议核对重点为 OS，其他环境需要单独进行真实 smoke test。

默认每轮输出 4096 token、上下文 32768、客户端最多 32 轮、样本超时 300 秒、任务并发 1。官方 Worker 的自身轮数限制照常生效。模型规划的动作由官方环境执行，适配器不在本地执行模型生成的 shell。

## 评分与产物

- `experiment.json`：Benchmark 名称、提交、选题、重复次数、模型和预算、适配模式、依赖版本、适配器哈希与环境版本备注。
- `results.json`：保留所有尝试，包括超时、输出截断、环境故障和缺失 reward。
- `summary.json`：所有尝试成功比例、已评分尝试成功率、未知/错误数、评分覆盖是否完整。基础设施错误不从总分母消失；未知 reward 不伪装成已判失败。评分不完整时，所有尝试的成功比例只能作为下界。
- 每次的 `result.json`：原始官方最终结果、reward、轮数、外部工具调用提议数、耗时与模型用量。
- `traces/*.jsonl`：模型与环境交互记录。官方环境执行的工具不计入本地 telemetry.tool_calls；外部提议数单独记录，不能据此推断工具调用成功率或过程正确率。

`finish=true,status=completed,reward=0` 记失败；reward 缺失为未知；0到1之间的部分 reward 保留原值，但不计完整成功。样本结束以官方环境为准，模型自称完成不计成功。模型输出截断、空响应、客户端轮数或超时中断会请求取消该样本。适配器不重试有副作用的 interact 请求。

## 本地协议测试

```powershell
.venv/Scripts/python.exe -m pytest tests/test_agentbench_fc.py -q
```

测试使用 httpx.MockTransport 和固定模型响应，覆盖真实 HTTP 请求格式、工具参数转发、reward判定、消息增量/完整历史重置、非文本输入拒绝、输出截断、超时、轮数限制和取消。此类测试证明适配协议，不是 AgentBench 实测成绩。
