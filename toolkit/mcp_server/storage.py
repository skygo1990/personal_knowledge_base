from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unicodedata

from mcp_server.models import HubError


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_display(value):
    return "".join(
        f"[U+{ord(character):04X}]"
        if character not in "\n\t" and unicodedata.category(character) in {"Cc", "Cf"}
        else character
        for character in str(value)
    )


def checked_path(path):
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise HubError("INVALID_PATH", "本机状态路径不能包含符号链接")
    return path


def ensure_directory(path):
    path = checked_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def read_json(path, default=None):
    path = checked_path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            return json.load(source)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        raise HubError("INVALID_STATE", "本机状态文件损坏或不可读取") from None


def atomic_json(path, value, *, mode=0o600):
    path = checked_path(path)
    ensure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        os.fchmod(destination.fileno(), mode)
        json.dump(value, destination, ensure_ascii=False, allow_nan=False)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary_name, path)


def atomic_text(path, value, *, mode=0o600):
    path = checked_path(path)
    ensure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as destination:
        os.fchmod(destination.fileno(), mode)
        destination.write(value)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary_name, path)


@contextmanager
def file_lock(path):
    path = checked_path(path)
    ensure_directory(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as lock_file:
        if os.name == "nt":
            import msvcrt
            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
