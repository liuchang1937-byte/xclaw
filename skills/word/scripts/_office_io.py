"""Office CLI 共用的路径、输入及原子保存规则，依赖 Python 标准库。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

ROOT = Path.cwd().resolve()
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_CELLS = 200_000


def safe_path(value, *, suffixes=None, exists=False):
    path = Path(value).expanduser()
    resolved = (path if path.is_absolute() else ROOT / path).resolve()
    try:
        relative = resolved.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError("文件路径超出当前工作区") from exc
    if any(part.casefold() in {".git", ".ssh", ".aws", ".codex"} for part in relative.parts):
        raise ValueError("不允许操作元数据或凭据目录")
    if resolved.name.casefold().startswith(".env"):
        raise ValueError("不允许访问 .env 文件")
    if suffixes and resolved.suffix.lower() not in suffixes:
        raise ValueError(f"不支持文件格式 {resolved.suffix}，需要 {', '.join(suffixes)}")
    if exists and not resolved.is_file():
        raise ValueError(f"文件不存在: {value}")
    return resolved


def read_json(value):
    path = safe_path(value, suffixes={".json"}, exists=True)
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("JSON 输入超过 2 MiB，请拆分操作")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def check_package(path, *, excel=False, editing=False):
    with ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 10_000 or sum(item.file_size for item in infos) > MAX_PACKAGE_BYTES:
            raise ValueError("Office 压缩包超过运行资源上限（32 MiB 解压体积 / 10000 个部件）")
        names = [item.filename.lower() for item in infos]
        unsupported = [
            name
            for name in names
            if "_xmlsignatures/" in name
            or "vbaproject" in name
            or "/activex/" in name
            or "/embeddings/" in name
            or excel
            and (
                name.startswith("xl/externalLinks/".lower())
                or name.startswith("xl/slicers/")
                or name.startswith("xl/pivottables/")
            )
        ]
        if editing and unsupported:
            raise ValueError(
                "发现不支持保真的复杂 Office 部件，拒绝编辑: " + ", ".join(unsupported[:3])
            )
        return unsupported


def atomic_save(output, writer, *, overwrite=False, sources=()):
    target = safe_path(output)
    if target.exists() and not overwrite:
        raise ValueError("输出文件已存在；明确需要覆盖时使用 --overwrite")
    if any(target == source.resolve() for source in sources) and not overwrite:
        raise ValueError("默认保留原文件，请指定新的输出路径")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".office-", suffix=target.suffix, dir=target.parent)
    os.close(fd)
    temp = Path(temporary)
    try:
        writer(temp)
        if target.suffix.lower() in {".docx", ".xlsx"}:
            with ZipFile(temp) as archive:
                if archive.testzip() is not None:
                    raise ValueError("生成文件的 ZIP 校验失败")
        if target.exists() and not overwrite:
            raise ValueError("保存期间输出文件已出现，未覆盖")
        if overwrite:
            os.replace(temp, target)
        elif os.name == "nt":
            # Windows rename 不覆盖已有目标；同目录移动在完整文件写好后发布。
            os.rename(temp, target)
        else:
            # POSIX 用排他硬链接发布完整文件，避免 rename 覆盖并发创建的目标。
            os.link(temp, target)
        return str(target.relative_to(ROOT))
    finally:
        temp.unlink(missing_ok=True)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, default=str))


def run(main):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        raise SystemExit(1) from exc
