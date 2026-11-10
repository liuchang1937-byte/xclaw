"""Replace a JSON file only after a complete, flushed write."""

import json
import os
import stat
import tempfile
from pathlib import Path


def atomic_write(path: str | Path, data: bytes) -> None:
    """Write beside the target, flush, then replace; preserve existing permission bits.

    Single-file atomic replacement, not a multi-file transaction or a guarantee
    against every filesystem/power failure. A hard kill may leave a temporary file.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_text(path: str | Path, content: str) -> None:
    atomic_write(path, content.encode("utf-8"))


def atomic_json(path: str | Path, data) -> None:
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
