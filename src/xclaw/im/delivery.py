"""显式收集本轮交付文件，不从模型正文猜路径，也不扫描工作区自动发送。"""

from __future__ import annotations

import re
import shutil
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory

from xclaw.core.execution import Capability
from xclaw.core.tool import Tool, ToolRegistry
from xclaw.im.adapter import IMReply

MAX_FILE_BYTES = 30_000_000
_pending: ContextVar[list[Path] | None] = ContextVar("xclaw_delivery", default=None)


@contextmanager
def collect_deliveries():
    files: list[Path] = []
    token = _pending.set(files)
    try:
        yield files
    finally:
        _pending.reset(token)


def validate_file(path: Path) -> None:
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE_BYTES:
        raise ValueError("交付文件必须存在、非空且不超过 30 MB")
    if path.suffix.lower() == ".xlsx":
        try:
            with zipfile.ZipFile(path) as archive:
                if sum(item.file_size for item in archive.infolist()) > 100_000_000:
                    raise ValueError("工作簿展开后超过 100 MB")
                names = set(archive.namelist())
                if not {"[Content_Types].xml", "xl/workbook.xml"} <= names or not any(
                    name.startswith("xl/worksheets/") and name.endswith(".xml") for name in names
                ):
                    raise ValueError("缺少工作簿或工作表")
        except (zipfile.BadZipFile, ValueError) as exc:
            raise ValueError("交付文件不是有效的 .xlsx 工作簿") from exc


def stage_reply_files(text: str, files: list[Path]) -> IMReply:
    """Copy finished files while their workspace is locked, before uploading them."""
    staging = TemporaryDirectory(prefix="xclaw-delivery-")
    paths = []
    try:
        for index, source in enumerate(files):
            target = Path(staging.name) / str(index) / source.name
            target.parent.mkdir()
            shutil.copyfile(source, target)
            paths.append(target)
    except BaseException:
        staging.cleanup()
        raise
    return IMReply(text=text, files=paths, staging=staging)


def requires_excel_file(text: str) -> bool:
    return bool(
        re.search(
            r"(创建|生成|制作|做|整理|导出|写).{0,30}(excel|xlsx|表格)|"
            r"(excel|xlsx|表格).{0,30}(发给|发送|给我|导出)",
            text.lower(),
        )
    )


def register_delivery_tool(registry: ToolRegistry) -> None:
    def deliver_file(path: str) -> str:
        files = _pending.get()
        if files is None:
            raise ValueError("文件交付仅能在当前 IM 请求内使用")
        resolved = Path(path).resolve()
        validate_file(resolved)
        if resolved not in files:
            files.append(resolved)
        return f"最终附件已准备: {resolved.name}。本轮任务完成后会统一发送，请勿声称已发送。"

    registry.add_tool(
        Tool(
            "deliver_file",
            "将工作区内已经生成并检查完成的最终文件作为本轮回复附件。"
            "创建 Excel 后必须调用此工具交付实际 .xlsx，不能仅回复路径、代码或 Markdown 表格。",
            deliver_file,
            capabilities=frozenset({Capability.FILE_READ}),
            path_parameters=("path",),
        )
    )
