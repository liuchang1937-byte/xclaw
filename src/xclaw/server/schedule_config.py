"""Configuration for recurring tasks and their delivery targets."""

from datetime import datetime, time, timedelta, timezone
from email.errors import HeaderParseError
from email.headerregistry import Address
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
GitHubRepo = Annotated[str, StringConstraints(
    strip_whitespace=True,
    to_lower=True,
    pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]*[A-Za-z0-9_-][A-Za-z0-9_.-]*$",
)]


def _check_address(value: str) -> str:
    try:
        address = Address(addr_spec=value)
    except HeaderParseError as exc:
        raise ValueError("请配置完整的邮箱地址") from exc
    if not address.username or not address.domain:
        raise ValueError("请配置完整的邮箱地址")
    return address.addr_spec


class SMTPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: NonEmpty
    port: int | None = Field(default=None, ge=1, le=65535)
    security: Literal["ssl", "starttls"] = "ssl"
    sender: NonEmpty
    username: NonEmpty | None = None
    password_env: NonEmpty = "XCLAW_SMTP_PASSWORD"

    _sender_address = field_validator("sender")(_check_address)


class ScheduledEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: list[NonEmpty] = Field(min_length=1)
    subject: NonEmpty = Field(max_length=200, pattern=r"^[^\r\n]+$")

    @field_validator("to")
    @classmethod
    def check_recipients(cls, addresses: list[str]) -> list[str]:
        return list(dict.fromkeys(_check_address(address) for address in addresses))


class ScheduledJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    prompt: NonEmpty | None = None
    enabled: bool = True
    every_seconds: int | None = Field(default=None, ge=1, le=315_360_000)
    daily_at: str | None = Field(default=None, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    utc_offset: str = Field(default="+00:00", pattern=r"^[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    timeout_seconds: int = Field(default=300, ge=1)
    urls: list[HttpUrl] = Field(default_factory=list, max_length=5)
    github_repo: GitHubRepo | None = None
    use_agent_tools: bool = False
    email: ScheduledEmail | None = None
    feishu_chat_id: str | None = Field(default=None, pattern=r"^oc_[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def check_schedule(self):
        if (self.every_seconds is None) == (self.daily_at is None):
            raise ValueError("every_seconds 和 daily_at 必须且只能配置一个")
        if self.github_repo:
            if self.prompt or self.urls or self.use_agent_tools:
                raise ValueError("GitHub 提交检查仅需 github_repo，无需配置 prompt、urls 或工具")
        elif self.prompt is None:
            raise ValueError("Agent 定时任务需要配置 prompt")
        if any(url.username or url.password for url in self.urls):
            raise ValueError("资料网址不能包含用户凭证")
        if self.urls and self.use_agent_tools:
            raise ValueError("读取网页的任务仅使用网页资料生成结果，请移除 use_agent_tools")
        return self

    @property
    def cadence(self) -> str:
        if self.every_seconds is not None:
            return f"every:{self.every_seconds}"
        return f"daily:{self.daily_at}{self.utc_offset}"

    def next_after(self, now: datetime, previous: datetime | None = None) -> datetime:
        if self.every_seconds is not None:
            interval = timedelta(seconds=self.every_seconds)
            anchor = previous or now
            return anchor + (max(0, (now - anchor) // interval) + 1) * interval
        hours, minutes = map(int, self.utc_offset[1:].split(":"))
        delta = timedelta(hours=hours, minutes=minutes)
        zone = timezone(-delta if self.utc_offset.startswith("-") else delta)
        local = now.astimezone(zone)
        target = datetime.combine(local.date(), time.fromisoformat(self.daily_at), tzinfo=zone)
        if target <= local:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc)


class SchedulerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_dir: NonEmpty = ".xclaw/scheduled"
    smtp: SMTPConfig | None = None
    jobs: list[ScheduledJob] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_jobs(self):
        if len({job.id for job in self.jobs}) != len(self.jobs):
            raise ValueError("定时任务 ID 不能重复")
        if self.smtp is None and any(job.enabled and job.email for job in self.jobs):
            raise ValueError("发送定时邮件需要配置 scheduler.smtp")
        return self
