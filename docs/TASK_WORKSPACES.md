# 独立任务工作区与显式回写

## 服务与 Demo

在现有配置的 agent 段合并以下选项并重启：

```yaml
agent:
  isolate_workspace: true
  task_workspace_dir: .xclaw/tasks
  trace_dir: logs/traces
```

飞书/QQ 服务和 CLI Demo 每次普通请求创建一份独立副本，工具绑定副本目录，Docker 仅挂载该副本。
同一会话仍串行执行，历史和 KV 记忆沿用会话状态；不同任务不会直接修改原工作区。
成功后输出任务目录与 `changes.diff`。失败任务保留现场；不自动删除、不自动合并。
文件附件仍从本次副本交付。`/status`、`/reset` 等管理命令不创建任务副本。
默认 `isolate_workspace: false`，既有直接编辑流程保持兼容。

新一轮任务基于原工作区重新创建。因此，上一轮尚未回写的改动不会自动进入下一轮副本，
虽然对话历史可能提及它；继续修改前应先查看并回写需要的改动。
MCP 服务仍有其自己的权限和副作用，任务副本只隔离文件工具和容器挂载的代码工作区。

## 手动使用

```powershell
uv run python -m xclaw.core.workspace create E:/path/to/repository --root .xclaw/tasks
```

输出目录包含：

```text
manifest.json   # 原目录、文件内容哈希、模式、排除规则
baseline/       # 创建任务时的完整文件内容
work/           # Agent 实际工作目录
```

可以把 Agent 的 `workspace_dir` 指向 `work/`。修改完成后：

```powershell
uv run python -m xclaw.core.workspace diff .xclaw/tasks/<id>
uv run python -m xclaw.core.workspace apply .xclaw/tasks/<id>
```

apply 是明确的回写操作，可先查看 changes.diff 与 changes.json。
对所有变更文件预先比较原工作区与基线哈希；任何冲突均拒绝整批回写。
新文件若已被他人创建也会冲突。删除默认拒绝，确需删除时显式传 `--allow-delete`。
文件写入使用临时文件与原子替换；处理中途失败时尝试还原已经修改的文件。
XClaw 回写使用原目录中的排他锁协调，但不能阻止外部编辑器或其他程序写入；
回写期间应暂停其他写入。多文件回写不是跨进程文件系统事务，断电可能留下部分变更。
进程崩溃遗留的 `.xclaw-apply.lock` 需确认没有正在回写的进程后手工移除。

## 快照范围与边界

复制的是工作目录实际内容，因此包含尚未提交的源码修改。默认排除 `.git`、依赖/缓存、
`.xclaw`、日志、会话、`.env*`、密钥文件、`config.yaml/config.yml` 等。
服务同时加入配置中的 denied_paths。符号链接和 Windows reparse point 不复制；
任务副本或回写目标出现链接时拒绝处理，避免跟随链接写出边界。
默认快照限制 10000 文件/100MB，超出时明确报错。

副本重新初始化为一个独立 Git 仓库，首次提交内容即工作目录快照。
原仓库的 `.git` 元数据、hooks、remote 和 Git worktree 的外部指针不进入副本。
副本可使用 Git 查看/提交，但不包含原提交历史。`create --no-git` 可以禁用初始化。
生成 diff 包括未换行文本标记；二进制变化在 changes.json 中记录，diff 仅作变化提示。
回写处理文件内容，保留已存在文件的权限；不重放 Git 提交历史、重命名语义或空目录。

会话历史和 KV 现在都采用原子 JSON 替换，损坏历史在服务恢复时记录错误并使用新历史。
两份状态文件不构成联合事务。工作区副本和会话历史本身不提供任务续执行；
轮次状态恢复由独立的 [Checkpoint 机制](CHECKPOINTS.md) 提供，重启后需显式恢复。
