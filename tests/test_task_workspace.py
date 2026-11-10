import json
import os
import subprocess

import pytest

from xclaw.core.workspace import TaskWorkspace, WorkspaceConflictError
from xclaw.persistence import atomic_json


def snapshot(tmp_path, *, git=False):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("original\n")
    return source, TaskWorkspace.create(source, tmp_path / "tasks", initialize_git=git)


def test_snapshot_preserves_uncommitted_contents_and_excludes_credentials(tmp_path):
    source, task = snapshot(tmp_path)
    (source / ".env").write_text("private")
    (source / "config.yaml").write_text("private")
    (source / ".git").write_text("gitdir: ../somewhere")
    (source / "app.py").write_text("uncommitted\n")
    task = TaskWorkspace.create(source, source / ".xclaw" / "tasks")
    assert (task.work / "app.py").read_text() == "uncommitted\n"
    assert not (task.work / ".env").exists()
    assert not (task.work / "config.yaml").exists()
    assert (task.work / ".git").is_dir()
    assert not (task.work / ".xclaw").exists()
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=task.work, capture_output=True, check=True
    )
    assert not result.stdout


def test_diff_and_apply_do_not_touch_source_until_requested(tmp_path):
    source, task = snapshot(tmp_path)
    (task.work / "app.py").write_text("modified\n")
    (task.work / "new.py").write_text("new\n")
    patch = task.export_diff().read_text()
    assert "-original" in patch and "+modified" in patch and "/dev/null" in patch
    assert (source / "app.py").read_text() == "original\n"
    assert task.apply() == ["app.py", "new.py"]
    assert (source / "new.py").read_text() == "new\n"


def test_conflict_preflight_prevents_all_partial_writes(tmp_path):
    source, task = snapshot(tmp_path)
    (task.work / "app.py").write_text("task version")
    (task.work / "aaa.py").write_text("new file")
    (source / "app.py").write_text("concurrent edit")
    with pytest.raises(WorkspaceConflictError, match="已变更"):
        task.apply()
    assert not (source / "aaa.py").exists()
    assert (source / "app.py").read_text() == "concurrent edit"
    assert not (source / ".xclaw-apply.lock").exists()


def test_two_task_copies_conflict_on_second_writeback(tmp_path):
    source, first = snapshot(tmp_path)
    second = TaskWorkspace.create(source, tmp_path / "tasks", initialize_git=False)
    (first.work / "app.py").write_text("one")
    (second.work / "app.py").write_text("two")
    first.apply()
    with pytest.raises(WorkspaceConflictError):
        second.apply()
    assert (source / "app.py").read_text() == "one"


def test_deletion_is_explicit_and_apply_rolls_back_on_write_failure(tmp_path, monkeypatch):
    from xclaw.core import workspace

    source, task = snapshot(tmp_path)
    (task.work / "app.py").unlink()
    with pytest.raises(WorkspaceConflictError, match="删除"):
        task.apply()
    assert task.apply(allow_delete=True) == ["app.py"]
    (source / "app.py").write_text("original\n")
    task = TaskWorkspace.create(source, tmp_path / "tasks", initialize_git=False)
    (task.work / "app.py").write_text("modified")
    (task.work / "zzz.py").write_text("new")
    original_write = workspace._write

    def fail_once(path, data):
        if path.name == "zzz.py":
            raise OSError("injected")
        original_write(path, data)

    monkeypatch.setattr(workspace, "_write", fail_once)
    with pytest.raises(OSError):
        task.apply()
    assert (source / "app.py").read_text() == "original\n"
    assert not (source / "zzz.py").exists()


def test_symlink_target_is_not_written(tmp_path):
    source, task = snapshot(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("outside")
    (task.work / "app.py").write_text("modified")
    (source / "app.py").unlink()
    try:
        (source / "app.py").symlink_to(outside)
    except OSError:
        pytest.skip("系统不允许创建符号链接")
    with pytest.raises(WorkspaceConflictError, match="链接"):
        task.apply()
    assert outside.read_text() == "outside"


def test_atomic_json_failure_leaves_previous_file_and_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "history.json"
    atomic_json(target, ["original"])

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        atomic_json(target, ["new"])
    assert json.loads(target.read_text()) == ["original"]
    assert list(tmp_path.iterdir()) == [target]
