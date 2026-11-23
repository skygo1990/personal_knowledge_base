import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib
from urllib.parse import urlsplit
import uuid

from ai_context_hub import __version__
from ai_context_hub.resources import startup_rules
from mcp_server.config import load_config
from mcp_server.models import HubError
from mcp_server.security import trusted_ancestors, validate_runtime
from mcp_server.storage import atomic_text, checked_path, ensure_directory, file_lock


START = "<!-- ai-context-hub:start -->"
END = "<!-- ai-context-hub:end -->"


def settings_text(values):
    return "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in values.items()) + "\n"


def validate_target(remote, branch):
    if not isinstance(remote, str) or not remote or remote.startswith("-") or "::" in remote or any(ord(char) < 32 for char in remote):
        raise HubError("INVALID_CONFIG", "请提供安全的 Git 仓库地址")
    parsed = urlsplit(remote)
    if "://" in remote:
        if parsed.scheme not in ("https", "ssh", "file") or parsed.query or parsed.fragment or parsed.password or (parsed.scheme == "https" and parsed.username):
            raise HubError("INVALID_CONFIG", "仓库地址不能携带凭据、查询参数或不安全协议")
    elif not Path(remote).is_absolute() and not re.fullmatch(r"[^/@\s]+@[^/:\s]+:.+", remote):
        raise HubError("INVALID_CONFIG", "本地仓库须使用绝对路径")
    if not shutil.which("git"):
        raise HubError("INVALID_CONFIG", "请先安装 Git；安装器不会修改系统软件")
    result = subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True)
    if result.returncode or branch.startswith("-"):
        raise HubError("INVALID_CONFIG", "分支名无效")


def private_directory(path):
    path = checked_path(path)
    ensure_directory(path)
    trusted_ancestors(path)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise HubError("UNAUTHORIZED", "安装目录必须由当前用户独占；不会自动放宽或重设已有权限")


def read_optional(path):
    path = checked_path(path)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def client_changes(codex_home, home, replace_client):
    config_path = checked_path(codex_home / "config.toml")
    rules_path = checked_path(codex_home / "AGENTS.md")
    original = read_optional(config_path)
    try:
        config = tomllib.loads(original)
    except ValueError:
        raise HubError("INVALID_CONFIG", "现有 Codex TOML 无效；不会覆盖") from None
    path_parts = [str(Path(shutil.which(name)).parent) for name in ("git", "gh") if shutil.which(name)]
    path_parts.extend(os.environ.get("PATH", os.defpath).split(os.pathsep))
    desired = {"command": sys.executable, "args": ["-m", "ai_context_hub", "serve", "--home", str(home)],
               "env": {"PATH": os.pathsep.join(dict.fromkeys(path_parts))}}
    current = config.get("mcp_servers", {}).get("personal_knowledge_base")
    updated = original
    if current != desired:
        if current is not None and not replace_client:
            raise HubError("INVALID_CONFIG", "已有同名 MCP；明确使用 --replace-client 才替换该服务，其他配置不变")
        if current is not None:
            lines = original.splitlines(keepends=True)
            kept = []
            skipping = False
            matched = False
            for line in lines:
                header = re.match(r"^\[([^\[\]\n]+)\]\s*(?:#.*)?$", line.strip())
                if header:
                    name = header.group(1)
                    skipping = name == "mcp_servers.personal_knowledge_base" or name.startswith("mcp_servers.personal_knowledge_base.")
                    matched = matched or skipping
                elif line.lstrip().startswith("["):
                    skipping = False
                if not skipping:
                    kept.append(line)
            if not matched:
                raise HubError("INVALID_CONFIG", "同名 MCP 使用特殊 TOML 写法；请先手动处理，不自动猜测修改")
            updated = "".join(kept)
        updated = updated.rstrip() + "\n\n[mcp_servers.personal_knowledge_base]\n"
        updated += settings_text({"command": desired["command"], "args": desired["args"]})
        updated += "\n[mcp_servers.personal_knowledge_base.env]\n" + settings_text(desired["env"])
        expected = {**config, "mcp_servers": {**config.get("mcp_servers", {}), "personal_knowledge_base": desired}}
        if tomllib.loads(updated) != expected:
            raise HubError("INVALID_CONFIG", "客户端配置合并校验失败；未写入")
    rules = read_optional(rules_path)
    block = (START + "\n## 个人知识库 MCP\n"
             "新会话先调用 personal_knowledge_base.get_core_context(view=\"base\", refresh=true)，按任务读取视角，引用来源并说明同步失败。\n"
             f"完整规则：`{home / 'STARTUP.md'}`。无工具时说明不可用，不用终端绕过 MCP。\n"
             "同账户模式不防同身份绕过。先交付 REVIEW.md，由本人批准该份材料及推送目标；只有服务显式启用且宿主允许时才经专用 MCP 执行。禁止审核 CLI 及代输确认，启用同意不等于内容批准。公开仓库会公开正文与元数据。\n" + END)
    if START in rules or END in rules:
        if rules.count(START) != 1 or rules.count(END) != 1 or rules.index(START) > rules.index(END):
            raise HubError("INVALID_CONFIG", "已有知识库启动规则标记不完整；不会覆盖")
        updated_rules = rules[:rules.index(START)] + block + rules[rules.index(END) + len(END):]
    else:
        updated_rules = rules.rstrip() + "\n\n" + block + "\n"
    return [(config_path, original, updated), (rules_path, rules, updated_rules)]


def initialize(home, remote, branch="main", *, codex_home=None, accepted=False,
               conversation_approval=None, git_helper=None, replace_client=False):
    if not accepted:
        raise HubError("UNAUTHORIZED", "须先确认安装范围；非交互使用 --accept-install，不代表批准知识")
    if os.name != "posix":
        raise HubError("UNAUTHORIZED", "当前安装器支持 macOS/Linux；原生 Windows 尚未适配")
    validate_target(remote, branch)
    home = checked_path(Path(home).expanduser())
    if git_helper not in (None, "system", "github_cli") or (git_helper == "github_cli" and not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*", remote)):
        raise HubError("INVALID_CONFIG", "GitHub CLI 认证仅支持固定的 GitHub HTTPS 仓库")
    if git_helper == "github_cli" and not shutil.which("gh"):
        raise HubError("INVALID_CONFIG", "已选择 GitHub CLI 认证，但本机没有 gh；不会自动创建或复制凭据")
    changes = client_changes(checked_path(Path(codex_home).expanduser()), home, replace_client) if codex_home else []
    private_directory(home)
    with file_lock(home / "install.lock"):
        reader_path, reviewer_path = home / "reader.toml", home / "reviewer.toml"
        if reader_path.exists() or reviewer_path.exists():
            if not reader_path.exists() or not reviewer_path.exists():
                raise HubError("INVALID_CONFIG", "已有部分配置；停止安装以保护原数据")
            reader = load_config(reader_path, "reader")
            reviewer = load_config(reviewer_path, "reviewer")
            for config in (reader, reviewer):
                validate_runtime(config)
                if config.remote_url != remote or config.branch != branch or config.security_mode != "single_user":
                    raise HubError("INVALID_CONFIG", "已有配置目标或安全模式不同；不会更换仓库或覆盖")
            if (reader.transport != "stdio" or (conversation_approval is not None and reader.allow_conversation_approval != conversation_approval)
                    or (git_helper is not None and reviewer.git_credential_helper != git_helper)):
                raise HubError("INVALID_CONFIG", "已有审批或认证配置不同；初始化不会自动修改权限")
        else:
            for name in ("reader-private", "reviewer-private", "proposals", "review-status"):
                private_directory(home / name)
            common = {"security_mode": "single_user", "device_id": "device-" + uuid.uuid4().hex[:12],
                      "remote_url": remote, "branch": branch, "proposal_dir": "proposals", "review_status_dir": "review-status"}
            reader_values = {**common, "role": "reader", "data_root": "reader-private", "transport": "stdio"}
            if conversation_approval:
                reader_values.update(allow_conversation_approval=True, reviewer_config="reviewer.toml")
            reviewer_values = {**common, "role": "reviewer", "data_root": "reviewer-private", "git_credential_helper": git_helper or "system"}
            atomic_text(reviewer_path, settings_text(reviewer_values))
            atomic_text(reader_path, settings_text(reader_values))
            reader = load_config(reader_path, "reader")
            validate_runtime(reader)
            validate_runtime(load_config(reviewer_path, "reviewer"))
        if reader.allow_conversation_approval:
            from mcp_server.approvals import ConversationReview
            ConversationReview(reader)
        atomic_text(home / "STARTUP.md", startup_rules())
        for path, original, updated in changes:
            if original == updated:
                continue
            ensure_directory(path.parent)
            trusted_ancestors(path)
            if read_optional(path) != original:
                raise HubError("INVALID_STATE", "安装期间客户端配置已被修改；请重试，不覆盖其他进程改动")
            if path.exists():
                private_directory(home / "backups")
                atomic_text(home / "backups" / (path.name + "." + str(time.time_ns())), original)
            atomic_text(path, updated)
        return {"ok": True, "version": __version__, "home": str(home), "client": "codex" if codex_home else "none",
                "conversation_approval": reader.allow_conversation_approval, "remote": remote, "branch": branch,
                "warning": "同账户不提供操作系统级防绕过；安装不批准任何知识，公开仓库推送将公开内容。"}


def doctor(home, refresh=False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home = checked_path(Path(home).expanduser())
    config = load_config(home / "reader.toml", "reader")
    validate_runtime(config)

    async def probe():
        parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "ai_context_hub", "serve", "--home", str(home)], env={"PATH": os.environ.get("PATH", os.defpath)})
        async with stdio_client(parameters) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing, read_timeout_seconds=timedelta(seconds=90)) as session:
                await session.initialize()
                tools = [tool.name for tool in (await session.list_tools()).tools]
                result = {"ok": True, "version": __version__, "tool_count": len(tools), "tools": tools, "refresh_requested": refresh}
                if refresh:
                    core = (await session.call_tool("get_core_context", {"refresh": True})).structuredContent
                    result.update(ok=bool(core.get("ok") and core.get("sync_status") == "fresh"),
                                  sync_status=core.get("sync_status", "unavailable"), snapshot_commit=core.get("snapshot_commit"),
                                  last_synced_at=core.get("last_synced_at"), project_count=core.get("data", {}).get("total"),
                                  warnings=core.get("warnings", []), error=core.get("error"))
                return result
    return asyncio.run(asyncio.wait_for(probe(), timeout=120))
