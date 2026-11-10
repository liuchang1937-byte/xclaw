# MCP 客户端接入

XClaw 可以连接本地 stdio 或远程 Streamable HTTP MCP 服务，发现工具后注册到
Agent 的工具调用流程。服务模式和终端 Demo 均支持，多会话共享连接，权限与对话历史
仍由各 Agent 独立管理。未配置服务时不会创建连接或启动进程。

安装依赖：

```powershell
uv sync --group dev --extra office
```

## 本地 stdio

在 `config.yaml` 顶层添加：

```yaml
mcp_servers:
  - name: demo
    transport: stdio
    command: "E:/study/project/XClaw/.venv/Scripts/python.exe"
    args: ["E:/study/project/XClaw/examples/mcp_demo_server.py"]
    timeout: 30
    allowed_tools: [add, echo]
```

启动 `uv run xclaw --config config.yaml --demo`，输入 `/tools` 可查看发现的工具，
再让 Agent 使用 demo 的 add 工具计算 2 + 3。飞书服务使用同一配置，启动命令去掉 `--demo`，
在机器人对话发送 `/status` 可查看工具。

工具名称有 `mcp_` 前缀和稳定的哈希后缀，避免不同服务的名称冲突并兼容模型的名称限制。
`command` 是可执行文件，不是 shell 命令字符串；参数分别放在 `args`。
可选 `cwd` 设置子进程目录，`env` 显式传递子进程所需的环境变量。
相对路径以启动 XClaw 时的工作目录为基准，Windows 上优先使用完整路径。

## 远程 Streamable HTTP

```yaml
mcp_servers:
  - name: remote
    transport: streamable-http
    url: "https://your-mcp-server.example/mcp"
    headers:
      Authorization: "Bearer ${MCP_API_TOKEN}"
    timeout: 30
```

启动前在 PowerShell 设置 `$env:MCP_API_TOKEN`。`headers`、`env`、stdio 的
`command`、`args`、`cwd` 支持 `${VARIABLE}`，缺失变量会导致明确的连接失败。
认证方式是配置 HTTP 请求头；本版没有交互式 OAuth 登录。

如需本地验证 HTTP，先在另一终端运行：

```powershell
uv run python examples/mcp_demo_server.py --transport streamable-http --port 8001
```

将 `url` 配置为 `http://127.0.0.1:8001/mcp`，本地示例不需要认证头。

## 配置和边界

- `enabled: false` 禁用单个服务。`allowed_tools` 未设置则导入全部工具，空列表不导入工具。
- `timeout` 同时限制初始化/发现和单次调用。启动时任何启用的服务连接失败都会终止启动，
  关闭先前已连接的服务，避免工具悄悄缺失。
- 工具定义保留完整 JSON Schema，支持嵌套对象、数组、本地 `$defs` 和必填参数校验。
  参数校验不会主动获取远程 `$ref`。
- MCP 调用使用独立 `mcp` 能力，只在 `execute` 模式开放；`view`、`edit` 不启动连接，
  也不向模型暴露 MCP 工具。服务的只读声明不会自动提升本地权限。
- stdio 子进程运行在宿主机，外部 MCP Server 的行为不会受到 XClaw 的 Docker 或路径策略约束。
  配置自己信任的服务，并用 `allowed_tools` 限定需要的工具。
- 支持文本、嵌入文本资源、资源链接和结构化结果。图片/音频返回占位提示，二进制数据不会
  塞进文本上下文。MCP 错误结果会显式标记为失败。
- 本版接入 MCP Tools；不导入独立的 Resources、Prompts，不实现自动重连或工具变更订阅。
  连接断开后调用会返回错误，更新服务配置后重启 XClaw。

实现使用 [官方 Python SDK 1.x](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)，
依赖限制 `<2`，避免 SDK 主版本 API 变化造成未经验证的升级。
