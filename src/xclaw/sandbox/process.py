"""有界输出收集；持续排空管道，避免输出过多导致内存增长或进程死锁。"""

from __future__ import annotations

import asyncio

from xclaw.sandbox.base import SandboxResult


async def capture_process(
    argv: list[str],
    *,
    timeout: float,
    output_limit: int,
    cwd: str | None = None,
) -> SandboxResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )

    async def read(stream):
        buffer = bytearray()
        truncated = False
        while chunk := await stream.read(4096):
            remaining = max(0, output_limit - len(buffer))
            buffer.extend(chunk[:remaining])
            truncated |= len(chunk) > remaining
        return buffer.decode("utf-8", errors="replace"), truncated

    async def collect():
        stdout, stderr, code = await asyncio.gather(
            read(proc.stdout), read(proc.stderr), proc.wait()
        )
        return SandboxResult(
            exit_code=code,
            stdout=stdout[0],
            stderr=stderr[0],
            truncated=stdout[1] or stderr[1],
        )

    task = asyncio.create_task(collect())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            result = SandboxResult(exit_code=-1)
        if isinstance(exc, asyncio.CancelledError):
            raise
        result.exit_code = -1
        result.timed_out = True
        return result
