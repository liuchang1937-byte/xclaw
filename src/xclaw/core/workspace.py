"""Independent task copies and conflict-checked, explicit writeback.

Only work/ is mounted for agent execution. baseline/ and manifest.json remain outside it.
Git metadata is reconstructed locally so worktree .git pointers cannot escape the mount.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from fnmatch import fnmatchcase
from pathlib import Path

from xclaw.persistence import atomic_json

EXCLUDED = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".uv-cache",
    ".xclaw",
    ".idea",
    ".office-build",
    "logs",
    "sessions",
    ".aws",
    ".ssh",
    ".codex",
    ".agents",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "config.yaml",
    "config.yml",
    ".xclaw-apply.lock",
)


class WorkspaceConflictError(RuntimeError):
    pass


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _blocked(relative: str, patterns=EXCLUDED) -> bool:
    parts = Path(relative).parts
    candidates = [relative, *parts, *("/".join(parts[i:]) for i in range(len(parts)))]
    return any(fnmatchcase(c.casefold(), p.casefold()) for c in candidates for p in patterns)


def _files(
    root: Path, patterns=EXCLUDED, omit: Path | None = None, *, reject_links=False
) -> dict[str, Path]:
    result = {}
    for directory, folders, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        if reject_links:
            for name in folders + names:
                path = parent / name
                relative = path.relative_to(root).as_posix()
                if not _blocked(relative, patterns) and _linked(path):
                    raise WorkspaceConflictError(f"任务副本包含链接，拒绝处理: {relative}")
        folders[:] = [
            name
            for name in folders
            if not _linked(parent / name)
            and not _blocked((parent / name).relative_to(root).as_posix(), patterns)
            and (omit is None or (parent / name).resolve() != omit)
        ]
        for name in names:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if not _blocked(relative, patterns) and not _linked(path) and path.is_file():
                result[relative] = path
    return result


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".xclaw-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class TaskWorkspace:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.work = self.directory / "work"
        self.baseline = self.directory / "baseline"
        self.manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        self.source = Path(self.manifest["source"]).resolve()
        self.patterns = tuple(self.manifest["excluded"])
        if not self.work.is_dir() or _linked(self.work) or _linked(self.baseline):
            raise ValueError("任务副本和基线必须是普通目录")
        for relative in self.manifest["files"]:
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("非法快照路径")

    @classmethod
    def create(
        cls,
        source: str | Path,
        task_root: str | Path,
        *,
        excluded=(),
        initialize_git=True,
        max_bytes=100_000_000,
        max_files=10000,
    ) -> TaskWorkspace:
        source = Path(source).resolve(strict=True)
        root = Path(task_root).resolve()
        if not source.is_dir():
            raise ValueError("任务来源必须是目录")
        if source == root or source.is_relative_to(root):
            raise ValueError("任务目录不能包含来源目录")
        root.mkdir(parents=True, exist_ok=True)
        directory = root / uuid.uuid4().hex
        work, baseline = directory / "work", directory / "baseline"
        work.mkdir(parents=True)
        baseline.mkdir()
        patterns = tuple(dict.fromkeys((*EXCLUDED, *excluded)))
        entries, total = {}, 0
        try:
            inventory = _files(source, patterns, omit=root)
            if len(inventory) > max_files:
                raise ValueError("来源文件数量超过快照上限")
            for relative, path in sorted(inventory.items()):
                size = path.stat().st_size
                if total + size > max_bytes:
                    raise ValueError("来源文件大小超过快照上限")
                data = path.read_bytes()
                total += len(data)
                if total > max_bytes:
                    raise ValueError("来源文件大小超过快照上限")
                for destination in (work / relative, baseline / relative):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                    shutil.copymode(path, destination)
                entries[relative] = {
                    "sha256": _digest(data),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
            atomic_json(
                directory / "manifest.json",
                {
                    "version": 1,
                    "source": str(source),
                    "excluded": patterns,
                    "files": entries,
                    "git_initialized": initialize_git,
                    "max_bytes": max_bytes,
                    "max_files": max_files,
                },
            )
            if initialize_git:
                # Include uncommitted source contents, without hooks, remotes or secrets.
                env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
                for key in list(env):
                    if key.startswith("GIT_") and key not in {
                        "GIT_CONFIG_NOSYSTEM",
                        "GIT_CONFIG_GLOBAL",
                    }:
                        env.pop(key)
                prefix = [
                    "git",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "user.name=XClaw",
                    "-c",
                    "user.email=xclaw@localhost",
                ]
                for args in (
                    ["init", "-q"],
                    ["add", "-f", "--", "."],
                    ["commit", "-q", "--allow-empty", "-m", "Task baseline"],
                ):
                    subprocess.run(
                        prefix + args,
                        cwd=work,
                        env=env,
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
            return cls(directory)
        except BaseException:
            # Keep partial snapshots for diagnosis; never touch source files.
            atomic_json(directory / "creation_failed.json", {"status": "failed"})
            raise

    def changes(self) -> list[dict]:
        current = _files(self.work, self.patterns, reject_links=True)
        if len(current) > self.manifest.get("max_files", 10000):
            raise WorkspaceConflictError("任务副本文件数量超过上限")
        if sum(path.stat().st_size for path in current.values()) > self.manifest.get(
            "max_bytes", 100_000_000
        ):
            raise WorkspaceConflictError("任务副本文件大小超过上限")
        changes = []
        for relative in sorted(set(current) | set(self.manifest["files"])):
            path = current.get(relative)
            old = self.manifest["files"].get(relative)
            new_hash = _digest(path.read_bytes()) if path else None
            if new_hash != (old["sha256"] if old else None):
                changes.append(
                    {
                        "path": relative,
                        "before": old["sha256"] if old else None,
                        "after": new_hash,
                        "kind": "delete" if not path else "modify" if old else "add",
                    }
                )
        return changes

    def export_diff(self) -> Path:
        parts = []
        for change in self.changes():
            relative = change["path"]
            before = (self.baseline / relative).read_bytes() if change["before"] else b""
            after = (self.work / relative).read_bytes() if change["after"] else b""
            try:
                if b"\0" in before + after:
                    raise UnicodeError()
                for line in difflib.unified_diff(
                    before.decode("utf-8").splitlines(keepends=True),
                    after.decode("utf-8").splitlines(keepends=True),
                    fromfile=f"a/{relative}" if change["before"] else "/dev/null",
                    tofile=f"b/{relative}" if change["after"] else "/dev/null",
                ):
                    parts.append(
                        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                    )
            except UnicodeError:
                parts.append(f"Binary file changed: {relative}\n")
        path = self.directory / "changes.diff"
        path.write_text("".join(parts), encoding="utf-8", newline="\n")
        atomic_json(self.directory / "changes.json", self.changes())
        return path

    def apply(self, *, allow_delete=False) -> list[str]:
        """Preflight every target, then replace files; rollback a partial failure.

        The lock coordinates XClaw writers. External editors must be paused during apply.
        """
        changes = self.changes()
        lock = self.source / ".xclaw-apply.lock"
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        backups, updated = {}, []
        try:
            os.close(fd)
            payloads = {}
            for change in changes:
                relative = change["path"]
                target = self.source / relative
                if _blocked(relative, self.patterns):
                    raise WorkspaceConflictError(f"禁止回写路径: {relative}")
                for part in (target, *target.parents):
                    if part.exists() or part.is_symlink():
                        if _linked(part):
                            raise WorkspaceConflictError(f"回写路径包含链接: {relative}")
                    if part == self.source:
                        break
                if not target.resolve().is_relative_to(self.source):
                    raise WorkspaceConflictError(f"回写路径越界: {relative}")
                if target.exists() and not target.is_file():
                    raise WorkspaceConflictError(f"回写目标不是文件: {relative}")
                original = target.read_bytes() if target.exists() else None
                if (_digest(original) if original is not None else None) != change["before"]:
                    raise WorkspaceConflictError(f"原工作区已变更，拒绝回写: {relative}")
                if change["kind"] == "delete" and not allow_delete:
                    raise WorkspaceConflictError(f"删除需要显式 allow_delete: {relative}")
                backups[relative] = (
                    original,
                    stat.S_IMODE(target.stat().st_mode) if target.exists() else None,
                )
                payloads[relative] = (
                    (self.work / relative).read_bytes() if change["after"] else None
                )
            try:
                for change in changes:
                    relative = change["path"]
                    target, data = self.source / relative, payloads[relative]
                    updated.append(relative)
                    if data is None:
                        target.unlink()
                    else:
                        _write(target, data)
                        mode = backups[relative][1]
                        if mode is not None:
                            target.chmod(mode)
                atomic_json(self.directory / "applied.json", {"files": updated})
            except BaseException:
                for relative in reversed(updated):
                    original, mode = backups[relative]
                    target = self.source / relative
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        _write(target, original)
                        target.chmod(mode)
                raise
            return updated
        finally:
            lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="创建任务副本、查看 diff 或显式回写")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("source")
    create.add_argument("--root", default=".xclaw/tasks")
    create.add_argument("--no-git", action="store_true")
    for name in ("diff", "apply"):
        command = commands.add_parser(name)
        command.add_argument("task_directory")
        if name == "apply":
            command.add_argument("--allow-delete", action="store_true")
    args = parser.parse_args()
    if args.command == "create":
        task = TaskWorkspace.create(args.source, args.root, initialize_git=not args.no_git)
        print(task.directory)
    elif args.command == "diff":
        print(TaskWorkspace(args.task_directory).export_diff())
    else:
        print(json.dumps(TaskWorkspace(args.task_directory).apply(allow_delete=args.allow_delete)))


if __name__ == "__main__":
    main()
