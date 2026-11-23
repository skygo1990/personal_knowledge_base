from pathlib import Path
import subprocess

from mcp_server.config import Config
from mcp_server.records import parse_record
from tests.helpers import make_markdown


def git(directory, *arguments, input=None):
    result = subprocess.run(["git", "-C", str(directory), *arguments], input=input, capture_output=True, check=True)
    return result.stdout.decode().strip()


def remote_fixture(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
    subprocess.run(["git", "init", "-b", "shared", str(work)], capture_output=True, check=True)
    git(work, "config", "user.name", "Test")
    git(work, "config", "user.email", "test@example.invalid")
    git(work, "remote", "add", "origin", str(remote))
    publish(work, make_markdown())
    return remote, work


def publish(work, markdown=None, *, raw_path=None, raw=None):
    if markdown is not None:
        record = parse_record(markdown)
        destination = work / record.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(markdown, encoding="utf-8")
    if raw_path:
        destination = work / raw_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(raw, encoding="utf-8")
    git(work, "add", "--all")
    git(work, "commit", "-m", "docs: 测试提交")
    git(work, "push", "origin", "HEAD:refs/heads/shared")
    return git(work, "rev-parse", "HEAD")


def config_for(tmp_path, remote, role="reader", device="device-a"):
    return Config(role, device, str(remote), "shared", tmp_path / f"{device}-{role}",
                  tmp_path / f"{device}-proposals", tmp_path / f"{device}-status")
