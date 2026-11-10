"""
文件操作工具集 - Coding Agent 的文件系统能力

提供给 Agent 的文件操作：
- 读取文件
- 写入文件（创建 / 修改）
- 搜索文件（按名称 / 内容）
- 列出目录
- 删除文件

所有操作都返回结构化的文本结果，方便 LLM 理解。
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Callable

from loguru import logger

from xclaw.core.concurrency import run_blocking
from xclaw.core.execution import Capability
from xclaw.core.tool import RiskLevel, Tool, ToolRegistry
from xclaw.persistence import atomic_text
from xclaw.sandbox.policy import ExecutionPolicy

# ─── 工作区限制 ─────────────────────────────────────
# 设置后，所有文件操作都会被限制在工作区根目录内，防止 Agent 越界访问

_path_validator: ContextVar[Callable[[Path], None] | None] = ContextVar(
    "file_tool_path_validator",
    default=None,
)


def set_workspace_root(path: str | None) -> None:
    """设置工作区根目录（设置后文件操作限制在该目录内）

    Args:
        path: 工作区根目录路径，None 表示解除限制
    """
    # 兼容独立调用；Agent 注册时绑定私有策略，不使用此入口。
    if path is None:
        _path_validator.set(None)
        return
    root = Path(path).expanduser().resolve()
    _path_validator.set(lambda value: ExecutionPolicy().check_path(value, root))


def _check_path(file_path: Path) -> str | None:
    """校验路径是否在工作区内

    Args:
        file_path: 要校验的路径

    Returns:
        str | None: 错误信息（None 表示通过）
    """
    validator = _path_validator.get()
    if validator is None:
        return None
    try:
        validator(file_path)
        return None
    except (ValueError, PermissionError) as exc:
        return f"❌ {exc}"


# ─── 文件读取工具 ──────────────────────────────────


async def read_file(path: str, start_line: int = 1, end_line: int = -1) -> str:
    return await run_blocking(_read_file, path, start_line, end_line)


def _read_file(path: str, start_line: int = 1, end_line: int = -1) -> str:
    """读取文件内容

    Args:
        path: 文件路径（相对于工作目录或绝对路径）
        start_line: 起始行号（从 1 开始）
        end_line: 结束行号（-1 表示读取到文件末尾）

    Returns:
        str: 文件内容（带行号）
    """
    file_path = Path(path).expanduser().resolve()

    workspace_err = _check_path(file_path)
    if workspace_err:
        return workspace_err

    if not file_path.exists():
        return f"❌ 文件不存在: {file_path}"

    if file_path.is_dir():
        return f"❌ 路径是目录而非文件: {file_path}"

    try:
        # 读取时尝试多种编码
        content = None
        for encoding in ["utf-8", "gbk", "latin-1"]:
            try:
                content = file_path.read_text(encoding=encoding)
                break
            except UnicodeDecodeError:
                continue

        if content is None:
            return f"❌ 无法解码文件内容: {file_path}"

        lines = content.split("\n")

        # 调整行号
        total_lines = len(lines)
        if end_line == -1:
            end_line = total_lines
        start_line = max(1, start_line)
        end_line = min(total_lines, end_line)

        selected_lines = lines[start_line - 1 : end_line]

        # 格式化输出（带行号）
        result_lines = [f"📄 文件: {file_path} (行 {start_line}-{end_line} / 共 {total_lines} 行)"]
        for i, line in enumerate(selected_lines, start=start_line):
            result_lines.append(f"{i:>6} | {line}")

        return "\n".join(result_lines)

    except Exception as e:
        return f"❌ 读取文件失败: {e}"


async def write_file(path: str, content: str) -> str:
    """写入文件（创建或覆盖）

    Args:
        path: 文件路径
        content: 要写入的内容

    Returns:
        str: 操作结果
    """
    file_path = Path(path).expanduser().resolve()

    workspace_err = _check_path(file_path)
    if workspace_err:
        return workspace_err

    try:
        # 确保父目录存在
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # 写入文件
        atomic_text(file_path, content)

        file_size = file_path.stat().st_size
        return (
            f"✅ 文件已写入: {file_path}\n"
            f"   大小: {file_size} 字节\n"
            f"   行数: {content.count(chr(10)) + 1}"
        )

    except Exception as e:
        return f"❌ 写入文件失败: {e}"


async def list_directory(path: str = ".", pattern: str = "*") -> str:
    return await run_blocking(_list_directory, path, pattern)


def _list_directory(path: str = ".", pattern: str = "*") -> str:
    """列出目录内容

    Args:
        path: 目录路径
        pattern: 文件名过滤模式（如 "*.py"）

    Returns:
        str: 目录内容列表
    """
    dir_path = Path(path).expanduser().resolve()

    workspace_err = _check_path(dir_path)
    if workspace_err:
        return workspace_err

    if not dir_path.exists():
        return f"❌ 目录不存在: {dir_path}"

    if not dir_path.is_dir():
        return f"❌ 路径不是目录: {dir_path}"

    try:
        items = sorted(item for item in dir_path.glob(pattern) if not _check_path(item))

        dirs = [i for i in items if i.is_dir()]
        files = [i for i in items if i.is_file()]

        result_lines = [f"📁 目录: {dir_path}"]
        result_lines.append(f"   子目录: {len(dirs)} 个, 文件: {len(files)} 个")
        result_lines.append("")

        if dirs:
            result_lines.append("📂 子目录:")
            for d in dirs:
                result_lines.append(f"   {d.name}/")
            result_lines.append("")

        if files:
            result_lines.append("📄 文件:")
            for f in files:
                size = f.stat().st_size
                size_str = _format_size(size)
                result_lines.append(f"   {f.name} ({size_str})")

        return "\n".join(result_lines)

    except Exception as e:
        return f"❌ 列出目录失败: {e}"


async def search_in_files(
    directory: str, query: str, file_pattern: str = "*", case_sensitive: bool = False
) -> str:
    return await run_blocking(_search_in_files, directory, query, file_pattern, case_sensitive)


def _search_in_files(
    directory: str, query: str, file_pattern: str = "*", case_sensitive: bool = False
) -> str:
    """在文件中搜索内容

    Args:
        directory: 搜索目录
        query: 搜索内容（支持正则表达式）
        file_pattern: 文件过滤模式（如 "*.py"）
        case_sensitive: 是否区分大小写

    Returns:
        str: 搜索结果
    """
    dir_path = Path(directory).expanduser().resolve()

    workspace_err = _check_path(dir_path)
    if workspace_err:
        return workspace_err

    if not dir_path.exists():
        return f"❌ 目录不存在: {dir_path}"

    try:
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(query, flags)

        results = []
        for file_path in dir_path.rglob(file_pattern):
            if _check_path(file_path):
                continue
            if file_path.is_file():
                try:
                    content = file_path.read_text(encoding="utf-8")
                    for i, line in enumerate(content.split("\n"), 1):
                        if pattern.search(line):
                            results.append(
                                {
                                    "file": str(file_path.relative_to(dir_path)),
                                    "line": i,
                                    "content": line.strip()[:120],
                                }
                            )
                except (UnicodeDecodeError, PermissionError):
                    continue

        if not results:
            return f"🔍 在 {dir_path} 中未找到匹配 '{query}' 的内容"

        result_lines = [f"🔍 搜索 '{query}' 在 {dir_path} 中:", f"   找到 {len(results)} 处匹配\n"]
        for r in results[:50]:  # 最多显示 50 条
            result_lines.append(f"   {r['file']}:{r['line']} | {r['content']}")

        if len(results) > 50:
            result_lines.append(f"   ... 还有 {len(results) - 50} 条结果")

        return "\n".join(result_lines)

    except Exception as e:
        return f"❌ 搜索失败: {e}"


async def delete_file(path: str) -> str:
    """删除文件

    ⚠️ 危险操作，删除后不可恢复。为安全起见，拒绝删除 .git 目录内的文件。

    Args:
        path: 要删除的文件路径

    Returns:
        str: 操作结果
    """
    file_path = Path(path).expanduser().resolve()

    workspace_err = _check_path(file_path)
    if workspace_err:
        return workspace_err

    # 保护版本控制目录，防止误删 .git 元数据
    if ".git" in file_path.parts:
        return f"❌ 拒绝删除 .git 目录内的文件: {file_path}"

    if not file_path.exists():
        return f"❌ 文件不存在: {file_path}"

    if file_path.is_dir():
        return f"❌ 请使用专门的目录删除方法: {file_path}"

    try:
        file_path.unlink()
        return f"✅ 文件已删除: {file_path}"
    except Exception as e:
        return f"❌ 删除文件失败: {e}"


async def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """精确编辑文件：用 new_string 替换 old_string

    相比 write_file 的整体覆盖，edit_file 只替换指定的内容，
    适合"改一行 / 改一个函数"的场景，避免重写整个文件。

    Args:
        path: 文件路径
        old_string: 要被替换的内容（必须能唯一定位）
        new_string: 替换后的新内容
        replace_all: 是否替换所有匹配（默认 False，要求唯一匹配）

    Returns:
        str: 操作结果
    """
    file_path = Path(path).expanduser().resolve()

    workspace_err = _check_path(file_path)
    if workspace_err:
        return workspace_err

    if not file_path.exists():
        return f"❌ 文件不存在: {file_path}"

    if file_path.is_dir():
        return f"❌ 路径是目录而非文件: {file_path}"

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"❌ 文件不是 UTF-8 编码，无法编辑: {file_path}"
    except Exception as e:
        return f"❌ 读取文件失败: {e}"

    if not old_string:
        return "❌ old_string 不能为空"

    count = content.count(old_string)
    if count == 0:
        return f"❌ 未找到要替换的内容: {old_string[:80]}"

    if count > 1 and not replace_all:
        return (
            f"❌ 目标内容出现 {count} 次，无法唯一定位。"
            f"请提供更精确的上下文，或设置 replace_all=true"
        )

    new_content = content.replace(old_string, new_string)
    try:
        atomic_text(file_path, new_content)
    except Exception as e:
        return f"❌ 写入文件失败: {e}"

    return f"✅ 文件已编辑: {file_path}\n   替换 {count} 处"


def _format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def register_file_tools(
    registry: ToolRegistry,
    *,
    workspace: str | None = None,
    policy: ExecutionPolicy | None = None,
) -> None:
    """将所有文件工具注册到指定的工具注册中心

    Args:
        registry: 工具注册中心实例
    """
    tools = [
        Tool(
            name="read_file",
            description=(
                "读取文件内容。可以指定行号范围，返回带行号的文件内容。"
                "适用于查看代码、配置文件、日志等。"
            ),
            func=read_file,
            parallel_safe=True,
            risk_level=RiskLevel.READ_ONLY,
            path_parameters=("path",),
        ),
        Tool(
            name="write_file",
            description=(
                "写入文件内容。如果文件不存在则创建，如果存在则覆盖。自动创建不存在的父目录。"
            ),
            func=write_file,
            risk_level=RiskLevel.WORKSPACE_WRITE,
            path_parameters=("path",),
        ),
        Tool(
            name="edit_file",
            description=(
                "精确编辑文件：用 new_string 替换 old_string。"
                "old_string 必须在文件中唯一（否则需设置 replace_all=true）。"
                "适合小范围修改，避免整体覆盖。"
            ),
            func=edit_file,
            risk_level=RiskLevel.WORKSPACE_WRITE,
            path_parameters=("path",),
        ),
        Tool(
            name="list_directory",
            description=(
                "列出目录中的文件和子目录。支持文件名模式过滤（如 '*.py'）。"
                "显示文件大小和数量统计。"
            ),
            func=list_directory,
            parallel_safe=True,
            risk_level=RiskLevel.READ_ONLY,
            path_parameters=("path",),
        ),
        Tool(
            name="search_in_files",
            description=(
                "在文件中搜索内容。支持正则表达式，可指定文件类型过滤。"
                "返回匹配的文件名、行号和内容。"
            ),
            func=search_in_files,
            parallel_safe=True,
            risk_level=RiskLevel.READ_ONLY,
            path_parameters=("directory",),
        ),
        Tool(
            name="delete_file",
            description="删除指定文件。⚠️ 请谨慎使用，删除后不可恢复。",
            func=delete_file,
            risk_level=RiskLevel.WORKSPACE_WRITE,
            path_parameters=("path",),
        ),
    ]

    for tool in tools:
        capabilities = {Capability.FILE_READ}
        if tool.risk_level == RiskLevel.WORKSPACE_WRITE:
            capabilities.add(Capability.FILE_WRITE)
        if tool.name == "delete_file":
            capabilities.add(Capability.FILE_DELETE)
        tool.capabilities = frozenset(capabilities)
        router = registry._sandbox_router
        bound_policy = policy or (router.policy if router else ExecutionPolicy())
        root = Path(workspace or (str(router.workspace) if router else ".")).resolve()

        def bind(func, write, root=root, bound_policy=bound_policy):
            @wraps(func)
            async def scoped_tool(**kwargs):
                token = _path_validator.set(
                    lambda path: bound_policy.check_path(path, root, write=write)
                )
                try:
                    return await func(**kwargs)
                finally:
                    _path_validator.reset(token)

            return scoped_tool

        tool.func = bind(tool.func, Capability.FILE_WRITE in capabilities)
        registry.add_tool(tool)

    logger.info(f"已注册 {len(tools)} 个文件操作工具")
