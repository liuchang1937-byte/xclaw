# 飞书交付 Excel 文件

在飞书向 XClaw 提出“创建 Excel 并发给我”时，Agent 用可信的 `create_excel` 工具创建
简单数据表；复杂编辑通过 excel 技能和预置脚本完成。生成并检查 `.xlsx` 后调用
`deliver_file`。附件在本轮任务结束后统一上传并发送到原会话，
不会从回复文本猜测路径或扫描工作区发送其他文件。

## 运行准备

```powershell
cd E:\study\project\XClaw
uv sync --extra office --group dev
uv run xclaw --config config.yaml
```

普通数据表的 `create_excel` 使用本地 openpyxl，不需要 Docker，允许 `edit` 和 `execute`
模式。表头、格式、筛选与冻结行自动设置；字符串按文本保存，保留编号中的前导零。
默认不覆盖已有输出，创建后重新打开工作簿验证尺寸，再交付附件。

复杂操作的技能脚本需要 `execute` 模式和运行中的 Docker Desktop。首次使用前构建镜像：

```powershell
docker build -f Dockerfile.sandbox -t xclaw-sandbox:latest .
```

技能脚本的输出目录与宿主机工作区共享；飞书上传读取的是已生成的实际文件。

配置应用机器人和长连接：

```yaml
agent:
  execution_mode: execute
  workspace_dir: "E:/study/project/XClaw"
im:
  platform: feishu
  feishu_mode: websocket
  feishu_app_id: "你的 App ID"
  feishu_app_secret: "你的 App Secret"
```

保留原配置中的 Provider 等其他字段，不要把上述片段当作完整配置覆盖。
目前 IM 配置文件中的 `${VARIABLE}` 不会自动展开；这里填写实际凭据，或通过现有纯环境变量
配置入口启动。本节配置为应用机器人，Webhook 模式不支持文件交付。

飞书开放平台需启用机器人、选择长连接、订阅 `im.message.receive_v1`，并开通：

- `im:message.p2p_msg:readonly`：接收用户发给机器人的单聊消息。
- `im:message.group_at_msg:readonly`：接收群里 @ 机器人的消息。
- `im:message:send_as_bot`：以应用身份发送回复。
- `im:resource`：上传文件资源。

权限变更后创建版本并发布，使自己的账号在应用可用范围内。
参考 [文件上传](https://open.feishu.cn/document/server-docs/im-v1/file/create)、
[消息发送](https://open.feishu.cn/document/server-docs/im-v1/message/create) 和
[消息接收](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)。

如果使用项目已有 PM2 配置，代码更新后执行 `pm2 restart xclaw-agent`。
机器人已有对话中如保存过 DSML 异常文本，可先发送 `/reset` 再提出文件请求。

## 处理和验证

完整、独立且符合已提供工具 Schema 的 DSML 调用会恢复成标准工具调用。含示例正文、
残缺标签、未知工具或无效参数的响应会纠正重试；连续失败给出错误，避免把协议文本当成
最终答复。DeepSeek 的 `reasoning_content` 保留在 API 回传链路，不发给飞书用户。

`deliver_file` 只允许当前请求使用，文件必须存在、非空、在允许的工作区路径内，且不超过
上传大小上限。`.xlsx` 还会检查工作簿与工作表 ZIP 结构。本轮要求创建 Excel 却仅返回
文本时，会继续要求 Agent 生成并提交实际附件；达到轮次限制仍没有附件则报告任务未完成。

上传使用 `POST /im/v1/files`，成功后用返回的 `file_key` 发送 `msg_type=file` 消息。
有原消息 ID 时通过回复接口发回原会话。上传或消息发送失败时报告“文件已生成，但发送失败”，
不会声称已交付成功。同一会话的任务串行执行，附件队列按请求隔离。

HTTP 400/403 中的飞书业务错误会先解析，再生成错误提示。`99991672` 会明确提示需要
开通 `im:resource` 等对应权限；日志保留 HTTP 状态、业务错误码和 Log ID，凭据会脱敏。
网页上的 403 表示当前网页登录账号不能访问应用管理页；需要核对登录账号、企业和
`config.yaml` 中的 App ID。能在客户端手动上传文件不代表应用机器人已取得上传权限。

## 收到用户发送的 Excel

飞书 file 消息中的文件名、file_key 和原消息 ID 会被保留。通过
`GET /im/v1/messages/{message_id}/resources/{file_key}?type=file` 下载到工作区的
`inputs/feishu/` 子目录，再用本地 `inspect_excel` 读取实际工作表与单元格。
普通表格读取无需 Docker。权限不足时直接反馈下载错误，不把 `[文件消息]` 当作文件内容
让模型猜测。附件名称按本地文件名规则清洗，目标路径受工作区策略约束，下载使用临时文件，
大小限制为 30 MB，XLSX 解压后的总大小限制为 100 MB；失败不会覆盖已有文件。

测试覆盖从输入到 DSML 恢复、真实 `.xlsx` 创建、附件排队和飞书 multipart 上传/文件消息的
完整流程；飞书请求使用模拟 HTTP 接口。真实应用权限与消息可达性需在应用发布后验证。
