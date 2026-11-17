# 执行策略与后端

当前主链路是：`ToolRegistry → ExecutionPolicy → SandboxRouter → HostBackend / DockerSandbox`。
`view / edit / execute` 是权限预设，执行后端只有宿主机和 Docker 两种。

| 模式 | 文件读取 / Skill / 固定 Git 查看 | 文件创建、编辑 | Git 提交、切换分支 / Bash |
| --- | --- | --- | --- |
| view | 允许 | 拒绝 | 拒绝 |
| edit | 允许 | 允许 | 拒绝 |
| execute | 允许 | 允许 | 允许，在 Docker 中执行 |

删除文件需要另行设置 `allow_file_delete: true`，查看模式始终不能删除。
默认模式为 `execute`，保持编码任务能够执行测试；默认不开放独立文件删除工具。
查看和编辑模式使用只读的 Docker 工作区挂载；编辑模式的文件修改由 HostBackend 完成。

## 配置

下面是一个允许编辑源码和测试、禁止任意命令执行的示例。合并到现有配置的 `agent` 段：

```yaml
agent:
  workspace_dir: .
  execution_mode: edit
  allow_file_delete: false
  execution_timeout: 120
  execution_output_limit: 8000
  read_paths: ["**"]
  write_paths: ["src/**", "tests/**"]
  denied_paths: [".env", ".env.*", "*.pem", "*.key", ".ssh/**", ".aws/**"]
  docker_image: xclaw-sandbox:latest
```

路径相对 Agent 当前工作区，采用 glob 匹配。递归搜索和目录列表也过滤受限目标。
符号链接解析后的目标必须仍在工作区内。文件工具不能直接改写 `.git` 元数据。
服务和 CLI Demo 都使用这些执行策略配置。配置文件需要重启服务才生效。

## 聊天用户工作区

聊天服务按平台和发送者的稳定 ID 分配持久目录。私聊跨消息复用同一目录，
群聊按平台、群和发送者分配各自的目录。用户无需选择路径。

- 用户目录默认在当前服务账号的 `~/.xclaw/workspaces` 下，可用 `agent.user_workspace_dir` 指定。
  它与服务的 `agent.workspace_dir` 使用分开的目录树。每个用户目录下只有 `work/` 供 Agent 操作。
- 上传附件、生成文件、读取和发送附件都限定在当前工作区。发送前暂存最终文件，避免发送时文件被后续任务替换。
- Bash、Git、Skill 脚本只挂载当前工作区，以及管理员配置的独立只读 Skill 目录。
  私有工作区的命令结束后会移除容器。若上次清理中断，会核对容器归属后再恢复该工作区。
  聊天用户的 Agent 不挂载服务级 MCP 工具。CLI 和配置文件中的管理员任务仍使用服务工作区。
- 旧共享目录及旧会话记录保存在原处。历史和文件若经核实归属，可由管理员按需迁移。
  聊天定时任务继续按创建者和会话管理。当前的链接抓取、报告和提醒无需文件工作区。

## 工具注册

新工具用 `Capability` 声明具体能力：`PROMPT / FILE_READ / FILE_WRITE / FILE_DELETE / GIT_READ / PROCESS`。
模型只能提供工具参数，不能指定执行后端、模式或新增能力。
未声明能力的工具不会暴露给模型，经过路由器执行时也会被拒绝。

Python 工具实现和请求构建器属于可信应用代码。这套策略不能隔离恶意 Python 插件。
旧工具显式声明的 `RiskLevel` 会转换为兼容能力；`BaseSandbox / LocalSandbox` 保留旧 API，
但 CodingAgent 的主链路不再使用它们。

文件和提示词工具由宿主机执行。进程工具必须提供可信请求构建器，返回 `RunProcess`
或 `ProcessPlan`。高风险工具没有构建器时拒绝执行，Docker 后端不会调用宿主机 Python 函数。
工具参数先做对象、类型和函数签名检查，布尔值与整数参数使用真实类型注解生成 Schema。
进程工作目录按读取路径检查，不作为文件写入目标匹配 write_paths；进程副作用由 PROCESS
权限控制。否则只允许写源码文件时会误拒绝从工作区根目录运行测试。结构化文件写入仍受
write_paths 约束；任意 Bash 中的文件写入不能靠这些 glob 限制，需使用任务副本与验收检查。

Git 使用 argv 而不是拼接 shell 字符串；所有操作在容器内运行，禁用 hooks 和 fsmonitor。
固定 Git 查看能力还有独立的命令白名单，diff 禁用 external diff 和 textconv。
提交中的 `add → commit` 顺序执行，前一步失败后不会继续提交。
同一 Router 内的文件操作和整个进程计划串行执行，避免写入与测试交错。

## 生命周期与结果

容器惰性启动，启动锁避免重复创建，进程锁避免同一后端并发运行命令。
启动中断后的清理先核对容器归属标签，不删除同名的其他容器。
容器配置限制 CPU、内存和 PID，禁网、移除 capabilities 并开启 no-new-privileges。
Docker 不可用时返回执行错误，不降级到宿主机。

Ubuntu 服务可以通过 Docker 的 rootless 用户服务执行命令。XClaw 的 systemd 服务通过
`DOCKER_HOST=unix:///run/user/<服务用户的 UID>/docker.sock` 连接本机 socket；
用 `loginctl enable-linger` 让对应用户的 Docker 服务随系统启动。
私有工作区在 rootless 容器里用映射到服务账号的 UID，生成的文件仍归服务账号所有。
参见 [Docker rootless 安装说明](https://docs.docker.com/engine/security/rootless/)。

输出边读取边限制缓冲区，超出部分持续排空。上限分别作用于 stdout 和 stderr，单位为 UTF-8
字节，截断处可能出现替换字符。模型收到输出被截断的标记。
`SandboxResult` 包含 `exit_code / stdout / stderr / timed_out / truncated`。

超时或取消会销毁该后端的容器，终止其中的进程；后续调用重新创建容器。
清理失败时保留容器标识、禁止复用，并明确报告无法确认进程已停止。
`stop()` 取消正在执行的调用，释放容器，并拒绝新调用。
调用方取消运行中的 Agent 时会触发这条清理路径；`Agent.stop()` 的布尔标志本身不等于任务取消。

## 当前边界

服务与 Demo 可启用 `agent.isolate_workspace` 创建独立任务副本并导出 diff，回写需要显式命令。
聊天任务的来源目录是当前用户的工作区。
在执行模式中，任意代码可以访问、修改或删除整个挂载工作区。
`read_paths / write_paths / denied_paths / allow_file_delete` 约束结构化文件工具，不能约束任意
shell 内部的文件访问。因此，禁用删除工具不代表 Bash 无法删除文件；目录中也不应放置执行
任务不需要的服务凭据。对未知代码应指定独立、最小化的工作目录。
容器中的环境与宿主机不同，依赖需要预先放进镜像；禁网模式下不能现场联网安装依赖。
Git worktree 的 `.git` 文件若指向挂载范围之外，容器内的 Git 无法访问相应元数据。

独立任务副本保留当前未提交源码，排除凭据并重建 Git 基线；回写检查冲突，失败任务保留现场。
配置、命令和事务边界见 [任务工作区](TASK_WORKSPACES.md)。

## 验证

普通测试无需 Docker daemon：`python -m pytest -q`。
覆盖模式拒绝、路径限制、多工作区、Git argv、计划失败、输出限制、启动并发、超时和取消。
Docker 生命周期使用模拟 CLI 验证，同时用真实 Python 子进程验证输出收集与超时。

真实 Docker 测试需要先构建镜像，然后显式启用：

```powershell
docker build -f Dockerfile.sandbox -t xclaw-sandbox:latest .
$env:XCLAW_DOCKER_TESTS = "1"
python -m pytest tests/test_docker_backend.py -q
```

可以用 `--build-arg PYTHON_IMAGE=<镜像地址或 digest>` 指定 Debian 系的 Python 3.11 基础镜像。
构建时也可用 `DEBIAN_MIRROR` 和 `PIP_INDEX_URL` 构建参数指定依赖镜像。

未安装 Docker 时，真实容器测试会跳过，不能将模拟测试通过视为真实容器验证通过。
