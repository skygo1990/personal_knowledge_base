from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tempfile

from mcp_server.models import HubError, Record
from mcp_server.records import load_snapshot, parse_record, validate_records
from mcp_server.storage import atomic_json, checked_path, ensure_directory, file_lock, read_json, utc_now


COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


def check_commit(commit):
    if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
        raise HubError("SNAPSHOT_UNAVAILABLE", "快照提交标识无效")
    return commit


class GitRepository:
    def __init__(self, config):
        self.config = config
        self.path = checked_path(config.repo_path)
        self.credential_options = []
        if config.git_credential_helper == "github_cli":
            executable = shutil.which("gh")
            if not executable:
                raise HubError("INVALID_CONFIG", "GitHub CLI 凭据助手已启用，但服务 PATH 中没有 gh；不会退回其他凭据助手")
            helper = "!" + shlex.quote(str(Path(executable).absolute())) + " auth git-credential"
            self.credential_options = ["-c", "credential.helper=", "-c", "credential.useHttpPath=true",
                                       "-c", "credential." + config.remote_url + ".helper=" + helper,
                                       "-c", "http.followRedirects=false"]

    def run(self, *arguments, input=None, environment=None, allow_failure=False):
        process_environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        process_environment.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
        process_environment.update(environment or {})
        command = ["git", "--git-dir", str(self.path), "-c", "core.hooksPath=" + os.devnull,
                   "-c", "commit.gpgSign=false", *self.credential_options, *arguments]
        try:
            result = subprocess.run(command, input=input, capture_output=True, env=process_environment, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            raise HubError("SYNC_FAILED", "Git 操作不可用或超时", retryable=True) from None
        if result.returncode and not allow_failure:
            output = (result.stdout + result.stderr).decode("utf-8", errors="replace").casefold()
            message = "Git 操作失败；请检查远端连接与同步身份的权限"
            if any(marker in output for marker in ("could not read username", "could not read password", "authentication failed", "invalid username or token")):
                message = "Git 认证失败或缺少可用凭据；请核对知识库服务的 Git 认证，不等同于 GitHub CLI 登录状态"
            elif any(marker in output for marker in ("gh006", "gh013", "protected branch", "repository rule violations")):
                message = "Git 分支规则拒绝推送；须按远端既有规则处理，不得修改保护或强制推送"
            elif ("permission" in output and "denied" in output) or "write access" in output or "error: 403" in output:
                message = "Git 写入权限不足；请核对同步身份对固定仓库的写入权限"
            elif any(marker in output for marker in ("failed to connect", "could not resolve host", "connection timed out", "connection reset")):
                message = "Git 连接失败；请恢复远端连接后重试原批次"
            raise HubError("SYNC_FAILED", message, retryable=True)
        return result

    def initialize(self):
        ensure_directory(self.path.parent)
        if not self.path.exists():
            self.run("init", "--bare", str(self.path))
        if self.run("rev-parse", "--is-bare-repository").stdout.strip() != b"true":
            raise HubError("INVALID_STATE", "运行目录必须为专用 bare Git 对象库")

    def fetch(self):
        self.initialize()
        self.run("fetch", "--no-tags", "--no-recurse-submodules", "--", self.config.remote_url,
                 "refs/heads/" + self.config.branch)
        commit = self.run("rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.decode().strip()
        check_commit(commit)
        batches = set()
        for entry in self.run("rev-list", commit).stdout.decode().splitlines():
            message = self.run("show", "-s", "--format=%B", entry).stdout.decode("utf-8", errors="replace")
            for line in message.splitlines():
                if line.startswith("Batch-ID: "):
                    batch_id = line.removeprefix("Batch-ID: ")
                    if batch_id in batches:
                        raise HubError("INVALID_STATE", "远端存在重复 Batch-ID，停止同步和新写入")
                    batches.add(batch_id)
        return commit

    def is_ancestor(self, before, after):
        result = self.run("merge-base", "--is-ancestor", check_commit(before), check_commit(after), allow_failure=True)
        if result.returncode not in (0, 1):
            raise HubError("SYNC_FAILED", "无法核实远端提交连续性")
        return result.returncode == 0

    def records(self, commit):
        check_commit(commit)
        entries = self.run("ls-tree", "-r", "-z", "--full-tree", commit, "--", "knowledge").stdout
        records = {}
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            try:
                descriptor, raw_path = entry.split(b"\t", 1)
                mode, object_type, object_id = descriptor.decode("ascii").split()
                relative_path = raw_path.decode("utf-8")
                path = PurePosixPath(relative_path)
                if mode != "100644" or object_type != "blob" or path.is_absolute() or ".." in path.parts:
                    raise ValueError
                markdown = self.run("cat-file", "blob", object_id).stdout.decode("utf-8")
            except (UnicodeError, ValueError):
                raise HubError("INVALID_RECORD", "知识树存在非法文件、编码或符号链接") from None
            record = parse_record(markdown, relative_path)
            if record.id in records:
                raise HubError("INVALID_RECORD", "知识树包含重复 ID")
            records[record.id] = record
        validate_records(records)
        return records


@dataclass
class Snapshot:
    commit: str
    records: dict[str, Record]
    root: Path
    last_synced_at: str | None
    sync_status: str
    warnings: list[str]


class SnapshotStore:
    def __init__(self, config):
        self.config = config
        self.repository = GitRepository(config)
        self.snapshots = config.data_root / "snapshots"
        self.state_path = config.data_root / "sync-state" / "current.json"

    def get(self, snapshot_commit=None):
        state = read_json(self.state_path, {})
        commit = state.get("snapshot_commit") if snapshot_commit is None else snapshot_commit
        if not commit:
            raise HubError("SNAPSHOT_UNAVAILABLE", "没有成功同步的快照；当前知识不可用", retryable=True)
        check_commit(commit)
        root = checked_path(self.snapshots / commit)
        if not root.is_dir():
            raise HubError("SNAPSHOT_UNAVAILABLE", "指定快照不存在，不会替换为其他快照")
        warnings = list(state.get("warnings", []))
        if commit != state.get("snapshot_commit"):
            warnings.append("正在读取固定的历史快照")
        return Snapshot(commit, load_snapshot(root), root, state.get("last_synced_at"),
                        state.get("sync_status", "stale"), warnings)

    def refresh(self):
        with file_lock(self.config.data_root / "sync-state" / "refresh.lock"):
            previous = read_json(self.state_path, {})
            try:
                commit = self.repository.fetch()
                if previous.get("snapshot_commit") and not self.repository.is_ancestor(previous["snapshot_commit"], commit):
                    raise HubError("SYNC_FAILED", "远端历史不连续，停止自动更新")
                records = self.repository.records(commit)
                ensure_directory(self.snapshots)
                final_root = checked_path(self.snapshots / commit)
                if not final_root.exists():
                    with tempfile.TemporaryDirectory(prefix=".export-", dir=self.snapshots) as temporary_name:
                        temporary = Path(temporary_name)
                        for record in records.values():
                            destination = temporary / record.relative_path
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            destination.write_text(record.markdown, encoding="utf-8", newline="\n")
                        load_snapshot(temporary)
                        temporary.rename(final_root)
                load_snapshot(final_root)
                atomic_json(self.state_path, {"snapshot_commit": commit, "last_synced_at": utc_now(),
                                              "sync_status": "fresh", "warnings": []})
            except (HubError, OSError) as error:
                message = error.message if isinstance(error, HubError) else "快照文件操作失败"
                atomic_json(self.state_path, {**previous, "sync_status": "stale" if previous.get("snapshot_commit") else "unavailable",
                                              "warnings": [message]})
                if not previous.get("snapshot_commit"):
                    raise HubError("SNAPSHOT_UNAVAILABLE", "同步失败且没有可用快照", retryable=True) from None
            return self.get()
