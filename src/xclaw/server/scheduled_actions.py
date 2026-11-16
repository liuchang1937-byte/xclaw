"""Read configured sources and deliver completed scheduled task results."""

import asyncio
import os
import re
import smtplib
import socket
import ssl
import subprocess
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser
from ipaddress import IPv6Address, ip_address, ip_network

import httpx

from xclaw.server.schedule_config import ScheduledEmail, ScheduledJob, SMTPConfig

_MAX_SOURCE_BYTES = 2_000_000
_TOTAL_SOURCE_CHARS = 12_000
_IPV6_UNICAST = ip_network("2000::/3")
_IPV6_TRANSITION = (ip_network("2001::/32"), ip_network("2002::/16"))


class DeliveryUncertainError(RuntimeError):
    """The SMTP connection ended before the delivery result was confirmed."""


def fetch_github_head(repo: str, timeout: int) -> str:
    """Read the default branch's commit without fetching the repository."""
    remote = f"https://github.com/{repo}"
    try:
        result = subprocess.run(
            ["git", "-c", "credential.helper=", "ls-remote", "--exit-code", remote, "HEAD"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ValueError("检查 GitHub 提交需要安装 Git 并加入 PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"读取 GitHub 仓库 {repo} 超时") from exc
    if result.returncode:
        raise ValueError(
            f"无法读取 GitHub 仓库 {repo} 的默认分支（Git 退出码 {result.returncode}）"
        )
    head = re.fullmatch(r"([0-9a-f]{40}|[0-9a-f]{64})\tHEAD", result.stdout.strip())
    if head is None:
        raise ValueError(f"GitHub 仓库 {repo} 未返回有效的提交编号")
    return f"{remote}/commit/{head[1]}"


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden: str | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.hidden is None and tag in {"script", "style", "noscript", "template"}:
            self.hidden = tag

    def handle_endtag(self, tag):
        if self.hidden == tag:
            self.hidden = None

    def handle_data(self, data):
        if self.hidden is None and data.strip():
            self.parts.append(data.strip())


async def _public_source(url: httpx.URL) -> httpx.URL:
    """Resolve once and connect to a validated address, even if DNS changes later."""
    try:
        info = await asyncio.get_running_loop().getaddrinfo(
            url.raw_host.decode("ascii"), url.port, type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError(f"无法解析网页域名 {url.host}") from exc
    addresses = {ip_address(item[4][0]) for item in info}
    if not addresses or any(
        not address.is_global or address.is_multicast or address.is_reserved
        or (
            isinstance(address, IPv6Address)
            and (address not in _IPV6_UNICAST or any(address in net for net in _IPV6_TRANSITION))
        )
        for address in addresses
    ):
        raise ValueError("私聊定时任务只支持公网网页或数据接口")
    address = min(addresses, key=lambda item: (item.version, int(item)))
    return url.copy_with(host=address.compressed)


async def fetch_sources(
    job: ScheduledJob, *, public_only: bool = False,
) -> list[dict[str, str | bool]]:
    if not job.urls:
        return []
    results = []
    limit = _TOTAL_SOURCE_CHARS // len(job.urls)
    options = {"trust_env": False, "limits": httpx.Limits(max_keepalive_connections=0)}
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=False, **(options if public_only else {}),
    ) as client:
        for source in job.urls:
            url = httpx.URL(str(source))
            if public_only:
                target = await _public_source(url)
                request_args = {
                    "headers": {"Host": url.netloc.decode("ascii")},
                    "extensions": {"sni_hostname": url.raw_host.decode("ascii")},
                }
            else:
                target, request_args = url, {}
            async with client.stream("GET", target, **request_args) as response:
                response.raise_for_status()
                kind = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                if not (
                    kind.startswith("text/") or kind in {"application/json", "application/xml"}
                    or kind.endswith(("+xml", "+json"))
                ):
                    raise ValueError("资料网址须返回 HTML、文本、JSON 或 XML")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(data) + len(chunk) > _MAX_SOURCE_BYTES:
                        raise ValueError("资料网页超过 2 MB")
                    data.extend(chunk)
                try:
                    content = data.decode(response.encoding or "utf-8", errors="replace")
                except LookupError:
                    content = data.decode("utf-8", errors="replace")
            if kind in {"text/html", "application/xhtml+xml"}:
                parser = _PageText()
                parser.feed(content)
                parser.close()
                content = "\n".join(parser.parts)
            content = content.strip()
            if not content:
                raise ValueError("资料网址没有可读取的文字")
            results.append({
                "url": str(source), "text": content[:limit], "truncated": len(content) > limit,
            })
    return results


def send_scheduled_email(config: SMTPConfig, target: ScheduledEmail, body: str) -> None:
    password = os.environ.get(config.password_env) if config.username else None
    if config.username and not password:
        raise ValueError(f"未设置 SMTP 密码环境变量 {config.password_env}")

    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = ", ".join(target.to)
    message["Subject"] = target.subject
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid(domain=config.sender.rsplit("@", 1)[-1])
    message.set_content(body)

    context = ssl.create_default_context()
    port = config.port or (465 if config.security == "ssl" else 587)
    client = (
        smtplib.SMTP_SSL(config.host, port, timeout=15, context=context)
        if config.security == "ssl"
        else smtplib.SMTP(config.host, port, timeout=15)
    )
    try:
        if config.security == "starttls":
            client.starttls(context=context)
        if config.username:
            client.login(config.username, password)
        try:
            refused = client.send_message(message, from_addr=config.sender, to_addrs=target.to)
        except smtplib.SMTPServerDisconnected as exc:
            raise DeliveryUncertainError("SMTP 连接断开，邮件投递结果未知") from exc
        except smtplib.SMTPException:
            raise
        except OSError as exc:
            raise DeliveryUncertainError("SMTP 通信中断，邮件投递结果未知") from exc
    finally:
        client.close()
    if refused:
        raise RuntimeError(f"SMTP 拒绝了 {len(refused)} 个收件人，其余地址已被服务器接受")
