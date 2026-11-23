from dataclasses import dataclass
from pathlib import Path
import os
import re
import subprocess
import tomllib
from urllib.parse import urlsplit

from mcp_server.models import HubError
from mcp_server.storage import checked_path
from mcp_server.security import trusted_ancestors


@dataclass(frozen=True)
class Config:
    role: str
    device_id: str
    remote_url: str
    branch: str
    data_root: Path
    proposal_dir: Path
    review_status_dir: Path
    token_file: Path | None = None
    listen_host: str = "127.0.0.1"
    listen_port: int = 8765
    allowed_origins: tuple[str, ...] = ()
    security_mode: str = "isolated"
    transport: str = "http"
    allow_conversation_approval: bool = False
    reviewer_config: Path | None = None
    git_credential_helper: str = "system"

    @property
    def repo_path(self):
        return self.data_root / ("reader-repo.git" if self.role == "reader" else "writer-repo.git")


def load_config(config_path: Path, expected_role: str) -> Config:
    config_path = checked_path(Path(config_path).expanduser()).resolve()
    trusted_ancestors(config_path)
    try:
        info = config_path.stat()
        if info.st_mode & 0o022 or info.st_uid not in {0, os.getuid()}:
            raise HubError("UNAUTHORIZED", "配置文件须由当前身份或系统所有，且不能由其他身份修改")
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HubError("INVALID_CONFIG", "无法读取有效的本地 TOML 配置") from None
    required = {"role", "device_id", "remote_url", "branch", "data_root", "proposal_dir", "review_status_dir"}
    network_fields = {"token_file", "listen_host", "listen_port", "allowed_origins"}
    reader_fields = network_fields | {"transport", "allow_conversation_approval", "reviewer_config"}
    if not required <= data.keys() or data.keys() - required - reader_fields - {"security_mode", "git_credential_helper"}:
        raise HubError("INVALID_CONFIG", "配置缺少必填字段或包含未知字段")
    if data["role"] != expected_role or expected_role not in {"reader", "reviewer"}:
        raise HubError("UNAUTHORIZED", "配置角色与运行入口不匹配")
    if expected_role == "reviewer" and data.keys() & reader_fields:
        raise HubError("INVALID_CONFIG", "审核端不能包含读取服务的网络配置")
    security_mode = data.get("security_mode", "isolated")
    transport = data.get("transport", "http")
    if security_mode not in ("isolated", "single_user") or transport not in ("http", "stdio"):
        raise HubError("INVALID_CONFIG", "安全模式或传输方式无效")
    if transport == "stdio" and (security_mode != "single_user" or data.keys() & network_fields):
        raise HubError("INVALID_CONFIG", "stdio 仅供显式同账户模式使用，且不能混用 HTTP 配置")
    allow_approval = data.get("allow_conversation_approval", False)
    if type(allow_approval) is not bool:
        raise HubError("INVALID_CONFIG", "对话批准开关必须是布尔值")
    if allow_approval and (security_mode != "single_user" or transport != "stdio" or "reviewer_config" not in data):
        raise HubError("INVALID_CONFIG", "对话批准须显式使用 single_user、stdio 和指定审核配置")
    if not allow_approval and "reviewer_config" in data:
        raise HubError("INVALID_CONFIG", "关闭对话批准时不可配置审核入口")
    if any(not isinstance(data[key], str) or not data[key] for key in required):
        raise HubError("INVALID_CONFIG", "必填配置必须是非空字符串")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", data["device_id"]):
        raise HubError("INVALID_CONFIG", "设备标识格式不正确")
    remote = data["remote_url"]
    if any(ord(character) < 32 for character in remote) or remote.startswith("-") or "::" in remote:
        raise HubError("INVALID_CONFIG", "远端地址格式不安全")
    parsed = urlsplit(remote)
    if "://" in remote:
        if parsed.scheme not in {"https", "ssh", "file"} or parsed.query or parsed.fragment or parsed.password:
            raise HubError("INVALID_CONFIG", "远端地址包含不允许的协议或认证信息")
        if parsed.scheme == "https" and parsed.username:
            raise HubError("INVALID_CONFIG", "远端 URL 不能内嵌凭据")
    elif not re.fullmatch(r"[^/@\s]+@[^/:\s]+:.+", remote):
        remote = str((config_path.parent / remote).resolve())
    git_helper = data.get("git_credential_helper", "system")
    if git_helper not in ("system", "github_cli"):
        raise HubError("INVALID_CONFIG", "Git 凭据助手仅允许 system 或 github_cli")
    if git_helper == "github_cli" and (security_mode != "single_user" or not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*", remote)):
        raise HubError("INVALID_CONFIG", "GitHub CLI 凭据助手仅供同账户模式的固定 GitHub HTTPS 仓库使用")
    checked = subprocess.run(["git", "check-ref-format", "--branch", data["branch"]], capture_output=True)
    if checked.returncode or data["branch"].startswith("-"):
        raise HubError("INVALID_CONFIG", "共享分支名不合法")

    def local_path(field):
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise HubError("INVALID_CONFIG", "本地路径配置缺失")
        return checked_path(config_path.parent / Path(value).expanduser()).resolve()

    root = local_path("data_root")
    proposals = local_path("proposal_dir")
    status = local_path("review_status_dir")
    code_root = Path(__file__).resolve().parent.parent
    if root == config_path.parent or root == code_root or root in code_root.parents or code_root in root.parents:
        raise HubError("INVALID_CONFIG", "运行数据不能与配置根目录或程序目录混用")
    if any(root == shared or root in shared.parents or shared in root.parents for shared in (proposals, status)):
        raise HubError("INVALID_CONFIG", "共享目录不能位于角色私有数据目录内")
    if proposals == status or proposals in status.parents or status in proposals.parents:
        raise HubError("INVALID_CONFIG", "提案与可信状态投影目录不能重叠")
    host = data.get("listen_host", "127.0.0.1")
    port = data.get("listen_port", 8765)
    origins = data.get("allowed_origins", [])
    if host != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535:
        raise HubError("INVALID_CONFIG", "读取服务只能使用合法的本机监听地址")
    if not isinstance(origins, list) or any(not isinstance(origin, str) or "*" in origin or urlsplit(origin).scheme not in {"http", "https"} for origin in origins):
        raise HubError("INVALID_CONFIG", "Origin 白名单必须是明确的 HTTP 来源")
    return Config(expected_role, data["device_id"], remote, data["branch"], root, proposals, status,
                  local_path("token_file") if expected_role == "reader" and transport == "http" else None,
                  host, port, tuple(origins), security_mode, transport, allow_approval,
                  local_path("reviewer_config") if allow_approval else None, git_helper)
