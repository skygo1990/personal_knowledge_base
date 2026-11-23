import argparse
import json
import os
from pathlib import Path
import sys

from ai_context_hub import __version__
from ai_context_hub.setup import doctor, initialize, validate_target
from mcp_server.models import HubError
from mcp_server.storage import safe_display


def main(arguments=None):
    parser = argparse.ArgumentParser(description="个人知识库安装、接入与自检；不自动批准知识")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "doctor", "serve", "review"):
        command = commands.add_parser(name)
        command.add_argument("--home", type=Path, default=Path(os.environ.get("PKB_HOME", str(Path.home() / ".local/share/ai-context-hub"))))
        if name == "init":
            command.add_argument("--remote", required=True)
            command.add_argument("--branch", default="main")
            command.add_argument("--client", choices=("codex", "none"), default="codex")
            command.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
            command.add_argument("--accept-install", action="store_true")
            command.add_argument("--conversation-approval", action="store_true", default=None)
            command.add_argument("--github-cli", action="store_true", default=None)
            command.add_argument("--replace-client", action="store_true")
        elif name == "doctor":
            command.add_argument("--refresh", action="store_true")
        elif name == "review":
            command.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(arguments)
    try:
        if options.command == "serve":
            from mcp_server.server import main as serve
            return serve(["--config", str(options.home / "reader.toml")])
        if options.command == "review":
            from review.cli import main as review
            return review(["--config", str(options.home / "reviewer.toml"), *options.arguments])
        if options.command == "init":
            validate_target(options.remote, options.branch)
            accepted = options.accept_install
            if not accepted:
                print(safe_display(f"将创建本机目录 {options.home}，接入 {options.client}，目标 {options.remote} / {options.branch}。\n同账户不防绕过，公开仓库会公开内容；安装不批准任何记录。"))
                if not sys.stdin.isatty():
                    raise HubError("UNAUTHORIZED", "非交互安装须显式传入 --accept-install")
                accepted = input("确认安装？输入 yes：").strip() == "yes"
            result = initialize(options.home, options.remote, options.branch,
                                codex_home=options.codex_home if options.client == "codex" else None,
                                accepted=accepted, conversation_approval=options.conversation_approval,
                                git_helper="github_cli" if options.github_cli else None, replace_client=options.replace_client)
        else:
            result = doctor(options.home, options.refresh)
        print(safe_display(json.dumps(result, ensure_ascii=False, indent=2)))
        return 0 if result["ok"] else 2
    except HubError as error:
        print(safe_display(error.code + "：" + error.message), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("本机安装或配置不可用；未批准或推送知识，请检查目录与依赖。", file=sys.stderr)
        return 2
    except (ExceptionGroup, TimeoutError):
        print("MCP 自检未完成：请检查运行配置、依赖或连接；未批准或推送知识。", file=sys.stderr)
        return 2
