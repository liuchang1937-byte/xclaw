# 四维评测与评测驱动优化

项目在既有任务验收和独立 Judge 上增加了四维工程评测。入口仍是
`python -m xclaw.evaluation.runner`，离线审计入口是
`python -m xclaw.evaluation.audit`（安装后也可用 `xclaw-eval-audit`）。
默认只评测最终方案 `summary_scheduled`（摘要＋依赖调度），四维报告用于验收该方案。

## 运行

```powershell
# 20 题的夹具自检：不读模型配置、不调用 API，不代表 Agent 效果
uv run python -m xclaw.evaluation.runner --dataset balanced --self-check

# 日常冒烟：最终方案 × 3 个核心用例，共 3 次任务尝试
uv run python -m xclaw.evaluation.runner --dataset balanced --mode smoke --config config.yaml

# 模型、Prompt、工具变更后的全量验收；本例是 Prompt 变更
# 最终方案 × 20 题 × 3 次重复，共 60 次任务尝试，需 Docker 和实际模型用量
uv run python -m xclaw.evaluation.runner --dataset balanced --change prompt --repeats 3 --config config.yaml

# 可在全量运行后自动比较一个已完成的基线
uv run python -m xclaw.evaluation.runner --dataset balanced --change tools --repeats 3 --baseline .xclaw/evaluations/BASELINE_ID
```

`--change` 可取 `model / prompt / tools / runtime / scheduling`。声明变更后，CLI 拒绝
`--mode smoke`、`--tasks` 子集或 `--split holdout` 代替全量验收。代码修改后的团队/CI
流程应调用这一入口；CLI 不会侦测其他进程对文件或供应商模型别名的修改。
全量指所选 suite 的整个 evaluation split。留出集需要显式另跑。

原有 24 题和历史实验保持兼容：默认 `--dataset legacy`；20 题需显式选择 `balanced`。
两者的数据集哈希不同，不能直接计算成功率提升。预算可用 `--max-rounds`、
`--max-tokens`、`--context-window`、`--timeout` 指定；`max-tokens` 是单次输出上限，
轮次上限限制任务循环，它们不是美元预算或整个任务的 Token 硬上限。

## 四维指标和分母

新增 `dimensions.json`；原有 `summary.json` 保留尝试级口径。`report.md` 展示四维摘要。
每个 `result.json` 增加 `assessment`、`failure_attribution` 和版本来源。

| 维度 | 指标 | 判定与分母 |
|---|---|---|
| 效果 | success_rate | 各任务重复运行成功率的平均；每个任务等权，失败保留 |
| 效果 | attempt_success_rate / all_repeats_pass_rate | 成功尝试/全部尝试；所有重复均成功的任务/任务总数 |
| 效果 | answer_accuracy | 仅 correctness 下的答案规则通过比例；不是通用语义正确率 |
| 效果 | user_satisfaction | 独立用户、人工或产品观测的 1–5 分均值，无反馈则为空 |
| 效率 | steps / tool_calls | 主模型请求次数/实际启动工具数；主请求含客户端重试，辅助请求单独计量 |
| 效率 | reported_tokens / latency_seconds | 全部模型请求的已报告 Token、任务执行耗时；独立验收耗时另存 |
| 鲁棒性 | exception_recovered | fault_recovery 用例中观察到模型/工具异常后，最终独立验收和硬规则全部通过 |
| 鲁棒性 | adversarial_passed / long_task_completed | 标注为对抗/长任务的任务最终通过率 |
| 过程 | tool_selection_accuracy | 按标注逐调用对齐，正确槽位数/max(期望调用数,实际调用数) |
| 过程 | invalid_step_ratio | 出错、跳过、或标注选错工具的调用并集/全部工具提议；不会重复扣同一次调用 |
| 过程 | repeated_action_ratio | 重复的工具名+规范化参数次数/实际启动调用数；使用任务内私有指纹 |

除任务成功率外，每项指标保存 `value / observed_attempts / total_attempts / coverage`，
也是先在任务内平均，再跨任务平均。鲁棒性覆盖率同时反映适用用例比例；`observed_faults`
保留实际故障数。不符合适用条件不是失败，也不能记作通过。
无工具、缺 Trace、没有逐调用标注等情形返回 `null`；不会以 0 成本或 100% 正确补齐。
Token 缺失时仍展示已知下界，`usage_complete=false`，不能用于成本门禁。

无效步数是可观测的工具决策指标，不代表全部思考轮次的语义浪费；重复读取也可能合理。
默认只形成诊断信号，除非任务显式把过程约束设为硬规则，否则不自动否决最终成功。
工具执行成功率和工具组覆盖率继续保留，二者均不能替代工具选择准确率。

## 用例设计与数据隔离

`balanced` v1 是产品回归示例集：12 基础、4 边界、2 对抗、2 回归，正好为 60/20/10/10。
边界覆盖缺参、冲突、超长历史和可恢复的缺失文件；对抗覆盖源码注入和误导性示例；
回归题由仓库的余额不足重试、截断摘要保护测试抽取最小复现，`badcase` 保存来源。
这些最小题不替代原来的真实模块单元测试，更不代表真实流量验收。

任务按 `text / tool / end_to_end / mixed` 分层，并分别输出统计。内置文本题使用
独立确定性回复验收，工具题显式约束先读取再修改，其余任务用独立代码验收。
混合题同时检查代码目标、最终回复和安全约束，防止代码通过掩盖回答缺失。

自定义清单沿用原来的 fixture 和 validator 格式，可追加：

```json
{
  "suite": {"id": "my-product", "version": "2026.10", "kind": "product_regression"},
  "tasks": [{
    "id": "historical_badcase_001",
    "category": "repository_bug",
    "prompt": "先 read_file 读取 app.py，再修改该文件修复空输入；完成后回复 FIXED。",
    "fixture": "fixtures/empty-input",
    "validator": "validators/empty-input.py",
    "allowed_write_paths": ["app.py"],
    "evaluation": {
      "scenario": "regression", "task_type": "mixed", "split": "evaluation",
      "smoke": true, "critical": true, "badcase": "ISSUE-123",
      "expected_tool_sequence": [["read_file"], ["write_file", "edit_file"]],
      "rules": [
        {"id": "reply", "kind": "answer_contains", "text": "FIXED", "dimension": "completeness"},
        {"id": "path", "kind": "tool_argument", "call_index": 0,
         "arguments": {"path": "app.py"}, "dimension": "process"},
        {"id": "rounds", "kind": "max_steps", "limit": 4,
         "dimension": "efficiency", "hard": false}
      ]
    }
  }]
}
```

规则还支持 `answer_excludes` 和 `tool_sequence`。后者要求实际工具序列与规则严格对齐，
每个位置允许一组等价工具；仅用于确有顺序契约的任务，不应强迫所有 Coding 任务走同一路径。
参数规则比较指定字段的 JSON 值；它不能证明工具返回的业务事实正确。业务事实、调用终态、
占座释放、补偿或转人工上下文，应由外部 validator 根据固定环境状态独立验收。
validator 可从 `Path(__file__).with_name('response.txt')` 读取最终回复，文件不暴露给 Agent。

suite.kind 还支持 `benchmark`。Benchmark 固定任务、环境、验收脚本和版本；产品回归集
跟随业务版本增加 Badcase。两类必须用不同 suite.id 和产物目录；门禁拒绝混合版本。
`split=holdout` 默认不运行，需显式 `--split holdout`；调优过程不要查看其答案、规则或中间分数。
修复线上问题时新增 regression 任务并填写 badcase，确认旧实现失败、正确实现通过，再升级
suite.version。扩题后分别查看共同题与新增题；严格发布门禁需要重新建立完整基线。
清单哈希和工作区隔离不等于权限隔离或防泄漏认证，留出题仍需在组织权限上独立保管。

## Rubric、独立评审和反馈

确定性规则按 `correctness / evidence / completeness / safety / efficiency / process`
六项归类。分级锚点：5=该维规则全部通过；3=软规则部分通过；1=硬规则失败或全部不通过；
缺规则或证据不足为空。每项分数保留规则 ID。规则默认 `hard=true`，失败推翻最终成功，
硬规则证据缺失归为 `evaluation_unavailable`，不会默认放行。
规则评分只说明所覆盖契约，不能代替开放式质量判断；安全关键词检查也不是完整注入检测。

既有 [质量评测](QUALITY_EVALUATION.md) 继续提供带证据引用的独立 Judge、
重复审查和人工校准。模型高分不能覆盖独立验收失败。对两条均成功的轨迹，可以导出盲评包：

```powershell
uv run python -m xclaw.evaluation.audit review-pairs BEFORE AFTER REVIEW_OUTPUT --baseline-variant summary_serial --candidate-variant summary_scheduled
```

`review.json` 隐去方案/模型身份，以稳定散列安排 A/B 位置，保留任务要求、产物、工具事实、
规则和效率证据；`private_key.json` 是解盲映射，不交给评审员。评审时成对比较结果及过程，
重复标注并用既有人工校准检查分歧。证据不足的事实应回到独立工具终态核验。

真实反馈单独导入，不修改原始结果：

```json
[{"task_id":"clamp","variant":"summary_serial","attempt":1,
  "score":4,"source":"user","evidence":"feedback-record-123"}]
```

```powershell
uv run python -m xclaw.evaluation.audit feedback RUN_DIRECTORY labels.json
```

输出 `feedback.json`，source 只允许 `user / human / product_signal`，拒绝 Agent 自评分。
线上无人审核时，应另外接入影子流量、分层抽检、重试/追问/转人工/完成率等行为信号，
用留出流量确认评测指标与实际体验相关。当前实现提供离线反馈入口，没有接管生产流量。

## Trace 与复现

评测默认开启正文捕获，增加 `model.request / model.response / tool.arguments / tool.result`，
并保留开始/结束、状态、模型、工具定义哈希、参数指纹、错误分类、用量、耗时和最终评测判定。
单条正文超过 256000 字符会标记 `truncated=true` 且 payload 为空，依赖该证据的规则返回未知。
服务和 Demo 仍默认只记录元数据。评测可用 `--metadata-only` 关闭正文捕获；参数规则可能
因此不可判定。自定义 fixture 应先清除敏感内容；评测正文产物按输入数据权限保管。

experiment 和结果记录任务/评测契约哈希、suite 版本、模型标识、主/辅助 Prompt、
工具定义及实现、运行时代码、评分器来源、Python、平台和不可变 Docker 镜像 ID。
Prompt 常量的版本单独计算，避免只改 Prompt 时误判为运行时变化。模型标识可能是供应商
可变别名；需要严格跨期比较时应配置固定模型版本，记录日期并保留供应商侧版本证明。

```powershell
uv run python -m xclaw.evaluation.audit replay RUN_DIRECTORY
```

离线读取固定产物并用确定性规则重算，输出 `replay.json`，不调用模型、不执行候选代码，
不重新证明历史独立验收。契约/评分器版本不匹配会拒绝；最终回复或 Trace 缺失会显式降低
覆盖率。需要重新证明业务终态时，重新运行固定 fixture 与独立验收。

## 门禁与优化闭环

```powershell
uv run python -m xclaw.evaluation.audit gate BEFORE AFTER --change tools --min-success 0.85 --max-cost-increase 0.20 --max-latency-increase 0.15

# 在相同任务、模型和预算下验收最终方案的 Prompt 升级
uv run python -m xclaw.evaluation.audit gate BEFORE AFTER --change prompt
```

输出 `gate.json`，失败退出 2。默认要求任务成功率至少 80%、没有成功率下降、
没有以前通过的尝试回退、关键任务全部通过；成本/延迟阈值需显式设置。
关键任务即使基线失败，也必须在候选中通过。门禁还检查全量完成、实际尝试覆盖计划、
任务/评分契约、环境、预算、并发度和实现版本；缺失元数据不能算可比。
只有显式 `--change` 指定的控制变量允许变化。评分器、镜像、Python 等变化需建立新基线。
重复运行需足够样本；这些门禁是产品规则，不宣称具有统计显著性。

`dimensions.json` 包含按场景、任务类型、split 分层的结果和 `optimization_candidates`，
成功但出现无效/重复动作的任务也会进入候选清单。失败按 planning、tool_call、parameter、
execution、timeout、evaluator 分类，同时保留原始 status 和失败规则。分类表示观察到的
失败阶段，`root_cause_confirmed=false`，验收失败不能自动证明“模型规划错误”。

调优记录建议写清四件事：

1. **发现问题**：固定版本下哪类任务、哪项指标下降，分母和覆盖率是多少。
2. **归因分析**：定位 Trace 中的错误工具、参数、重复决策或评测器故障；提出可证伪假设。
3. **优化方案**：例如区分工具描述的适用场景与输入输出，明确本次唯一控制变量。
4. **效果验证**：全量配对重跑，查看成功率、成本、延迟、步数、过程与关键场景回归；
   门禁失败则继续定位，线上变更按项目发布流程灰度或回滚。

这里没有预填“65%→88%”等提升结果。应以真实报告为准。当前项目没有 Sub-agent 执行层，
因此没有伪造路由准确率、交接完整性或独立贡献；以后接入时需要路由/交接真值和子任务 Trace，
在相同任务、模型和预算下分别评估单 Agent 与多 Agent。
