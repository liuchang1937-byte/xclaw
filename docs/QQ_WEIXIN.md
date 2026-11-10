# QQ 与个人微信接入

每个 XClaw 服务进程选择一个 `im.platform`。要同时运行 QQ 和微信，可以从当前配置复制两份 YAML，分别设置各自的渠道，然后启动两个进程。新增渠道的会话历史按渠道和会话 ID 隔离。

## 个人微信

在用于微信的配置文件中设置 `im.platform: weixin`。然后运行下面的命令，用手机微信扫一扫，在手机上确认绑定。

```powershell
uv run xclaw --config config.weixin.yaml --weixin-login
uv run xclaw --config config.weixin.yaml
```

在微信的 ClawBot 对话里发 `/status`，收到状态回复后就可以发任务。适配器支持私聊文本，回复使用入站消息的 `context_token`。服务把 iLink 的同步游标与凭证保存在用户目录下的 `.xclaw/weixin.json`。可以用 `im.weixin_state_file` 指定其他路径，每个状态文件由一个运行实例使用。重新绑定前先停止正在轮询的实例。

## QQ 桥接

将以下字段合并到现有配置。QQ 开放平台直连仍通过 `im.platform: qq` 和默认的 `qq_mode: websocket` 选择。

```yaml
im:
  platform: qq
  qq_mode: bridge
  qq_bridge_host: 127.0.0.1
  qq_bridge_token: "替换为自己生成的随机密钥"
  qq_bridge_send_command: [".venv/Scripts/python.exe", "examples/qq_onebot_bridge.py", "send"]
server:
  port: 8080
```

`qq_bridge_send_command` 是程序和参数列表。XClaw 在项目目录执行它，并通过环境变量 `MICROCLAW_QQ_TARGET`、`MICROCLAW_QQ_TEXT`、`XCLAW_QQ_CHAT_TYPE` 和 `XCLAW_QQ_REPLY_TO` 提供目标、正文、会话类型和原消息 ID。退出码 `0` 表示发送成功。

### 使用 OneBot 11 转发

先启动 NapCat 或其他 OneBot 11 客户端并登录负责回复的 QQ 账号。用 NapCat 时，从启动日志打开 WebUI，默认地址为 `http://127.0.0.1:6099/webui/`。`3000` 是下面示例里另外启用的 OneBot API 端口。先在管理界面创建并启用 HTTP 服务器，再用它调用 `get_login_info` 验证连接。[NapCat WebUI 指南](https://napneko.github.io/config/basic) · [OneBot HTTP 连接指南](https://napneko.github.io/NapCatQQ-Desktop/docs/connections/)

准备支持 OneBot 11 的 QQ 客户端，在该客户端开启 HTTP API，并设置 HTTP POST 事件上报地址为 `http://127.0.0.1:8765/onebot/events`。选择消息段数组格式，并配置用于 `X-Signature` 的上报密钥。下例假设 OneBot API 监听 `127.0.0.1:3000`。在两个 PowerShell 窗口里都设置下面的环境变量。

```powershell
$env:ONEBOT_API_URL = 'http://127.0.0.1:3000'
$env:ONEBOT_API_TOKEN = '填写 OneBot HTTP API 的 token'
$env:ONEBOT_POST_SECRET = '填写 OneBot HTTP 上报签名的密钥'
$env:XCLAW_QQ_BRIDGE_TOKEN = '填写 YAML 中的 qq_bridge_token'
$env:XCLAW_QQ_ALLOWED_USERS = '填写允许派任务的 QQ 号，多个用逗号分隔'
```

在两个窗口分别运行 `uv run xclaw --config config.qq.yaml` 和 `uv run python examples/qq_onebot_bridge.py serve`。转发脚本接受允许名单用户的私聊，以及群聊里 `@` 该账号的文本消息。

已有其他转发程序时，可以直接向 `POST /qq/events` 提交请求，带 `X-QQ-Webhook-Token` 请求头。私聊提交 `user_id`、`text` 和用于去重的 `message_id`，群聊额外提交 `chat_id`。

```json
{"user_id":"12345","text":"/status","message_id":"98765"}
```

HTTP `202` 表示消息已进入内存队列。回复由发送命令异步交付。进程退出时，未处理的队列项会丢失。

参考 [MicroClaw QQ 桥接](https://github.com/microclaw/microclaw/blob/main/src/channels/qq.rs)、[OneBot 11 HTTP 上报](https://github.com/botuniverse/onebot-11/blob/master/communication/http-post.md) 和 [腾讯微信 iLink 协议](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol_zh_CN.md)。
