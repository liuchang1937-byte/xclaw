# 定时任务

## 从飞书私聊创建

使用飞书应用机器人的 WebSocket 长连接时，可以直接发「每天 18:00 提醒我写日报」，
或者「每天中午 12 点检查 GitHub 的 owner/repo 有没有更新」。XClaw 会回复任务编号
和下次执行时间，到点把结果发回原私聊。时间默认用北京时间。

- `/schedule list` 查看本私聊创建的任务。
- `/schedule daily 18:00 写日报` 创建每日提醒。
- `/schedule every 60 写日报` 创建每小时提醒。
- `/schedule github 12:00 owner/repo` 创建仓库检查。
- `/schedule cancel <任务编号>` 取消本私聊的任务。

私聊创建的任务支持提醒、生成文本和检查 GitHub 提交。网页资料、邮件和 Agent 工具任务由管理员通过 YAML 配置。

## 从 YAML 配置

在现有 `config.yaml` 加入 `scheduler`，保留原有模型配置。下面的任务每天北京时间 09:00 读取配置中的页面，生成简报并发给指定邮箱。把示例网址、SMTP 服务和邮箱换成自己的设置。

```yaml
scheduler:
  smtp:
    host: smtp.example.com
    port: 587
    security: starttls
    sender: reports@example.com
    username: reports@example.com
    password_env: XCLAW_SMTP_PASSWORD
  jobs:
    - id: daily-brief
      daily_at: "09:00"
      utc_offset: "+08:00"
      prompt: 整理资料中的主要更新，写一份中文简报，附上来源链接。
      urls:
        - https://example.com/news
      email:
        to: [reader@example.com]
        subject: 每日资料简报
```

在启动服务的 PowerShell 中输入邮箱服务商提供的 SMTP 密码或授权码，再启动 XClaw。`Read-Host -MaskInput` 适用于 PowerShell 7。

```powershell
$env:XCLAW_SMTP_PASSWORD = Read-Host 'SMTP 密码或授权码' -MaskInput
uv run xclaw --config config.yaml
```

服务持续运行时会自动触发任务。若只运行定时任务，可以设置 `im.platform: none`。`--demo` 用于终端交互，定时任务由正常服务模式启动。

- `every_seconds` 可替代 `daily_at`，例如 `3600` 表示每小时运行。每天固定时间任务的 `utc_offset` 默认是 `+00:00`，按固定 UTC 偏移运行。
- 省略 `email` 可先验证资料抓取和生成结果。将 `every_seconds` 设为 `15`，启动后查看任务状态文件。省略 `urls` 可定时生成文本，例如提醒邮件。
- `urls` 可以填最多五个直接返回内容的 HTTP(S) 地址。支持 HTML、文本、JSON 和 XML（含 RSS）。采用 HTTP 抓取，依赖浏览器执行 JavaScript 的页面需要提供直接可读的页面、数据接口或 RSS 地址。出现重定向时填写最终地址。只要有配置的来源读取失败，本轮就记录失败。
- 定时任务默认只使用任务提示和指定网页作为输入。需要调用 XClaw 文件、命令或 MCP 工具的任务可配置 `use_agent_tools: true`，沿用 `agent` 的工具权限和工作区隔离设置。`urls` 任务仅处理网页资料和文本结果。
- SMTP 支持 `ssl` 和 `starttls`，默认端口分别为 `465` 和 `587`。有 `username` 时从 `password_env` 指定的环境变量读密码。服务把 Agent 的最终回答作为纯文本邮件正文，收件人和主题来自任务配置。

每个任务的状态默认写入 `.xclaw/scheduled/job-<id>.json`，可通过 `scheduler.state_dir` 改位置。文件记录下次执行时间、最近一轮的状态和结果，时间戳使用 UTC。一个状态目录里的同名任务一次由一个进程运行。重启时，已经到期的计划触发一次，跳过其余错过的时段。中断的任务记录为 `interrupted`，发送结果无法确认时记录为 `delivery_unknown`，后续按正常计划执行。

任务配置在服务启动时读取。修改配置后重启服务，设置 `enabled: false` 可以停用某个任务。合并到 QQ、微信或飞书的多个配置时，同一个定时任务配置在负责执行它的服务实例里。

## 检查 GitHub 仓库更新

在 `scheduler.jobs` 下加入以下任务，每天北京时间 12 点查询指定公开仓库的默认分支。运行 XClaw 的环境需要能访问 GitHub，并在 PATH 中安装 Git。

```yaml
    - id: xclaw-github-updates
      daily_at: "12:00"
      utc_offset: "+08:00"
      github_repo: liuchang1937-byte/xclaw
```

首次检查记录当前提交。此后每次与上次成功检查的提交比较，把有无更新和提交链接写入状态文件的 `last_run.result`。连接失败时在 `last_run.error` 记录错误，保留上次成功读取的提交。查询使用 `git ls-remote`，无需配置模型提示词。
