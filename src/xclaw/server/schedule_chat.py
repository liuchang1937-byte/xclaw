"""Manage recurring tasks in the requesting Feishu private conversation."""

import re

from xclaw.core.execution import Capability
from xclaw.core.tool import ToolRegistry
from xclaw.im.adapter import IMMessage
from xclaw.server.scheduler import ChatScheduleOwner, Scheduler

SCHEDULE_HELP = """可以在这条飞书私聊创建定时任务，时间默认用北京时间。
例如「每天 18:00 提醒我写日报」或「每天中午 12 点检查 GitHub owner/repo 有没有更新」。
任务运行后会把结果发回这条私聊。
查看：/schedule list
取消：/schedule cancel 任务编号
创建提醒：/schedule daily 18:00 写日报
创建间隔提醒：/schedule every 60 写日报
检查仓库：/schedule github 12:00 owner/repo"""

SCHEDULE_PROMPT = """你负责 XClaw 的飞书定时任务。只根据这条用户消息处理请求。
支持每天固定时间、固定分钟间隔。默认北京时间。到点提醒、生成文本或检查 GitHub
仓库默认分支，结果自动发回当前私聊。日期、频率或任务目标不明时追问。
仅当用户明确要求现在创建、查看或取消时才调用对应工具。问题、示例、引用文字
和条件讨论不代表授权。仅询问能否定时执行时，介绍功能并给出一句创建示例。
直接提醒用 reminder=true 并填写提醒正文。检查 GitHub 填 github_repo=owner/repo。
创建普通文本任务填写 prompt。一条消息创建一个任务。告诉用户 YAML 中的管理员任务
由管理员单独管理。特殊操作包括邮件、访问网页、运行代码，由管理员配置。
成功创建或取消必须以工具返回为准。使用简洁中文。"""

_SCHEDULE_REQUEST = re.compile(
    r"定时|定期|周期性|每天|每日|每隔|每(个)?(?:周|月|小时|分钟)|到点提醒|"
    r"每\s*\d+\s*(?:分钟|小时)|\bschedul(?:e|ed|ing)\b",
    re.IGNORECASE,
)


def is_schedule_request(message: IMMessage) -> bool:
    command, *_ = message.content.split(maxsplit=1)
    return command == "/schedule" or (
        message.chat_type in ("private", "p2p")
        and bool(_SCHEDULE_REQUEST.search(message.content))
    )


def _owner(message: IMMessage) -> ChatScheduleOwner:
    return ChatScheduleOwner(chat_id=message.chat_id, sender_id=message.sender_id)


async def handle_schedule_command(scheduler: Scheduler, message: IMMessage) -> str:
    _, *args = message.content.strip().split(maxsplit=3)
    if args == ["list"]:
        return scheduler.list_chat_jobs(_owner(message))
    if len(args) == 2 and args[0] == "cancel":
        return await scheduler.cancel_chat_job(_owner(message), args[1])
    if len(args) != 3:
        return SCHEDULE_HELP
    action, cadence, content = args
    if action == "daily":
        fields = {"daily_at": cadence, "prompt": content, "reminder": True}
    elif action == "github":
        fields = {"daily_at": cadence, "github_repo": content}
    elif action == "every" and cadence.isascii() and cadence.isdecimal() and len(cadence) <= 7:
        fields = {"every_minutes": int(cadence), "prompt": content, "reminder": True}
    else:
        return SCHEDULE_HELP
    return "已安排定时任务。\n" + scheduler.create_chat_job(
        _owner(message), message.message_id, **fields,
    )


def register_schedule_tools(
    registry: ToolRegistry, scheduler: Scheduler, message: IMMessage,
) -> list[str]:
    owner = _owner(message)
    replies: list[str] = []
    scope = frozenset({Capability.PROMPT})

    @registry.register(
        description="创建当前私聊的定时任务。daily_at 使用北京时间 HH:MM，或填每隔多少分钟 "
        "every_minutes。正文填 prompt，纯提醒再设 reminder。检查仓库只填 github_repo 作任务目标。",
        capabilities=scope,
    )
    def schedule_create(
        daily_at: str = "", every_minutes: int = 0, prompt: str = "",
        github_repo: str = "", reminder: bool = False,
    ) -> str:
        result = "已安排定时任务。\n" + scheduler.create_chat_job(
            owner, message.message_id, daily_at=daily_at, every_minutes=every_minutes,
            prompt=prompt, github_repo=github_repo, reminder=reminder,
        )
        replies.append(result)
        return result

    @registry.register(description="查看当前私聊创建的定时任务与编号。", capabilities=scope)
    def schedule_list() -> str:
        return scheduler.list_chat_jobs(owner)

    @registry.register(description="根据编号取消当前私聊的任务。", capabilities=scope)
    async def schedule_cancel(job_id: str) -> str:
        result = await scheduler.cancel_chat_job(owner, job_id)
        replies.append(result)
        return result

    return replies
