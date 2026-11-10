# AgentBench FC 官方环境冒烟测试（2026-11-02）

## 范围

本轮选择官方 `os-std` 的固定索引 0、1、2，各 1 次。Controller 实际返回 144 个可用索引。本轮是首次环境与协议冒烟，不是全量成绩，不推断跨任务泛化或 P95。

使用 XClaw AIClient 调用配置中的 `deepseek-v4-pro`，temperature=0，摘要上下文 32768，单轮输出最多 4096 token，客户端上限 32 轮、超时 300 秒；官方 OS Worker 的 8 轮限制照常生效。任务并发设为 1。

该入口模式为 `xclaw_client_external_execution`：工具由官方环境执行、成功由官方检查器与 reward 判定。它没有经过本地 CodingAgent 工具栈和依赖调度器，不能与产品的 `summary_scheduled` 成功率合并，也不能用来说明依赖图调度的净收益。

## 部署记录

- AgentBench 提交：`d1e4a10db08c87075c78972e48ecc182be03e2d5`。
- 官方 OS Worker 源码、配置和数据直接从该 checkout 构建；最终使用兼容 Dockerfile 固定 agentrl-worker=0.4.0、aiodocker=0.24.0、aiohttp=3.11.18，不修改任务/评分代码。
- Docker Desktop Linux 引擎：29.5.3。启动时遗留的两个零字节 socket 目录已改名备份；未重置 Docker 数据。
- 使用 `configs/agentbench-fc.compose.yaml` 提供隔离桥接网络，覆盖 Controller 服务名、Redis 服务名、Worker 并发和启动包装，保持官方工具、题目、评分和轮数配置。
- 本次只需默认 OS 镜像；它按官方 `default` Dockerfile 构建，基础镜像是官方文件引用的 `docker.1ms.run/ubuntu`。浮动基础镜像会影响跨时间比较，实际镜像标识和运行依赖另存快照。
- 初始环境保存为 `environment-20261102.json`，修复后环境保存为 `environment-20261102-compat.json`，另在成功运行目录内归档 `environment.json` 及其哈希。快照记录 Controller、Redis、Worker 和默认 OS 镜像标识、Worker 依赖版本以及配置哈希，不宣称与公开榜单的部署环境完全一致。

## 结果

修复环境后的独立运行已完成，官方检查器对三个样本均返回 `finish=true,status=completed,reward=1,score=1`：

| 官方索引 | 实际任务 | 官方 reward | 成功 | 模型交互轮数 | 耗时秒 | 总 token |
|---|---|---:|---|---:|---:|---:|
| 0 | 从股票日志统计 Alice 卖出次数 | 1 | 是 | 3 | 7.654 | 3,769 |
| 1 | 从股票日志统计 Bob 买入次数 | 1 | 是 | 4 | 9.322 | 5,385 |
| 2 | 从股票日志统计 Alice 买入股票总数量 | 1 | 是 | 3 | 10.091 | 3,945 |

本轮已评分样本成功率 **3/3，100%**，评分覆盖率 3/3，无模型错误，无缺失判定；平均耗时 **9.022s**，平均交互轮数 **3.33**，平均 token **4,366.33**，总 token **13,099**。用量完整，trace 写入未失败。

耗时包含样本容器初始化、模型请求、官方工具执行与验收，排除首次下载、镜像构建和服务部署。全部外部工具调用提议共 10 次；本地 telemetry.tool_calls=0 是因为动作在官方环境执行，不能据此称“没有使用工具”。

三个样本都来自股票日志统计这一类，且每题只有一次运行；这只能证明 OS 环境、模型交互和官方验收链路已跑通，不能推断 144 题 OS 全量或 AgentBench 各环境整体成功率，也不报告 P95。虽启用了摘要上下文管理，这些短轨迹未触发压缩（只有 10 次 main 请求，没有 summary 请求），因而不能证明摘要压缩的效果。依赖调度器未参与本轮。

## 首轮环境故障及修复

首轮还保留了 3 次环境初始化失败：使用官方未固定依赖安装到 aiodocker=0.27.0、aiohttp=3.14.5，Worker 向 exec 流传入整数 timeout，而运行库要求 ClientTimeout，出现 `AttributeError: 'int' object has no attribute 'connect'`。样本在进入模型前结束，3 次均为 0 模型调用、0 token，reward 未知，不能算模型答错。

修复只固定运行依赖为 aiodocker=0.24.0、aiohttp=3.11.18，并创建独立运行目录，没有删除、覆盖或混合初始错误记录。适配器另外增加“环境返回空观测”的专门分类及回归测试，避免后续将此类故障误记为模型错误；协议测试 **11 passed**，Ruff 检查通过。

本次部署过程共发生 **6 次样本启动：3 次环境故障，3 次修复后正式评分成功**。上面的 3/3 是修复后独立实验的分数，不表示首次部署零故障。

## 原始产物

- 修复后正式运行：`.xclaw/evaluations/agentbench-fc/dfe6d904d5d74901a4d2c7a8a8921e09`。
- 首轮故障运行：`.xclaw/evaluations/agentbench-fc/4be90fdda3b44428b8a6937766f1aa56`。
- 正式运行中保存 experiment/results/summary、逐题 result、完整模型/环境 trace、环境快照、Worker 与 Controller 日志。
- 官方服务维持可用，三个任务容器已由官方 Worker 自动清理。

## 复现

在镜像已构建的 XClaw 项目根目录：

```powershell
docker compose -f configs/agentbench-fc.compose.yaml up -d
.venv/Scripts/python.exe examples/agentbench_fc.py --check --task os-std
.venv/Scripts/python.exe -u examples/agentbench_fc.py --task os-std --indices 0 1 2 --repeats 1 --max-tokens 4096 --context-window 32768 --config config.yaml --environment-version .xclaw/evaluations/agentbench-fc/environment-20261102-compat.json
```

通过需要官方最终 reward=1；完成状态不自动等于成功。缺失 reward 和环境错误单独列出，模型自述不参与判定。官方任务可能在初始化中生成随机数据，因此同一索引的环境内容不保证逐次完全一致；实际观测保存在 trace 中。
