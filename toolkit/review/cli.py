import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from mcp_server.config import load_config
from mcp_server.delivery import render_review
from mcp_server.models import HubError
from mcp_server.security import validate_runtime
from mcp_server.storage import safe_display
from review.batch import ReviewService


def render_preview(preview):
    output = render_review(preview)
    if "delivery" in preview:
        delivery = preview["delivery"]
        output += "\n人读审核文件：" + delivery["human_path"] + "\n交付标识：" + delivery["delivery_id"] + "\n"
    return safe_display(output)


class HumanTerminal:
    def __init__(self, source=None, destination=None):
        self.source = source or sys.stdin
        self.destination = destination or sys.stdout

    def ask(self, display, phrase):
        if not self.source.isatty() or not self.destination.isatty():
            raise HubError("UNAUTHORIZED", "批准、拒绝、编辑或取消只能在本人交互终端执行")
        self.destination.write(safe_display(display) + "\n请输入完整短语：" + phrase + "\n> ")
        self.destination.flush()
        return self.source.readline().rstrip("\r\n")

    def confirm(self, preview, phrase):
        return self.ask(render_preview(preview), phrase)

    def share(self, record):
        display = "单独确认允许对外提供当前正文。\n" + record.markdown + "\n不确认则以 internal 入库，不会发布。"
        phrase = "允许外发 " + record.id + " " + ",".join(record.metadata["audience"])
        return self.ask(display, phrase) == phrase

    def cancel(self, batch, phrase):
        return self.ask("取消批次，不删除 Git 历史：" + batch["batch_id"], phrase)

    def reject(self, paths, phrase):
        return self.ask("仅清理以下两个明确文件的正文和依据：\n" + "\n".join(str(path) for path in paths), phrase)


def parser():
    result = argparse.ArgumentParser(description="个人知识库本人人工审核；不自动发布")
    result.add_argument("--config", default=os.environ.get("HUB_CONFIG"))
    commands = result.add_subparsers(dest="command", required=True)
    for command in ("list", "status"):
        commands.add_parser(command)
    for command in ("preview", "approve"):
        subcommand = commands.add_parser(command)
        selection = subcommand.add_mutually_exclusive_group(required=True)
        selection.add_argument("--proposal", action="append")
        selection.add_argument("--delivery")
    for command in ("edit", "reject"):
        subcommand = commands.add_parser(command)
        subcommand.add_argument("--proposal", action="append", required=True)
    for command in ("retry", "cancel"):
        commands.add_parser(command).add_argument("--batch", required=True)
    commands.add_parser("history").add_argument("--record", required=True)
    return result


def main(arguments=None):
    options = parser().parse_args(arguments)
    try:
        if not options.config:
            raise HubError("INVALID_CONFIG", "请使用 --config 或 HUB_CONFIG 指定配置")
        config = load_config(Path(options.config), "reviewer")
        validate_runtime(config)
        if config.security_mode == "single_user":
            print(safe_display("同账户简化模式：无操作系统级防绕过保障。批准后会提交并尝试推送；公开仓库将公开选中内容。\n远端：" + config.remote_url), file=sys.stderr)
        service = ReviewService(config)
        terminal = HumanTerminal()
        command = options.command
        if command == "preview":
            preview = service.preview_delivery(options.delivery) if options.delivery else service.preview(options.proposal)
            print(render_preview(preview))
            return 0
        if command == "approve":
            if options.delivery:
                result = service.approve_delivery(options.delivery, terminal.confirm, terminal.share)
            else:
                result = service.approve(options.proposal, terminal.confirm, terminal.share)
            result = {key: result.get(key) for key in ("batch_id", "state", "candidate")}
        elif command == "retry":
            result = service.retry(options.batch, terminal.confirm, terminal.share)
            result = {key: result.get(key) for key in ("batch_id", "state", "candidate")}
        elif command == "cancel":
            result = service.cancel(options.batch, terminal.cancel)
            result = {"batch_id": result["batch_id"], "state": result["state"]}
        elif command == "reject":
            if len(options.proposal) != 1:
                raise HubError("INVALID_RECORD", "拒绝时请逐个指定提案")
            result = service.reject(options.proposal[0], terminal.reject)
        elif command == "edit":
            if len(options.proposal) != 1:
                raise HubError("INVALID_RECORD", "编辑时请逐个指定提案")
            candidate = service.draft(options.proposal[0])
            display = "受保护待审稿（引用、预算等仍须在批准时验证）：\n" + candidate["proposed_markdown"] + "\n本机依据：\n" + candidate["basis"]
            if terminal.ask(display, "编辑 " + options.proposal[0]) != "编辑 " + options.proposal[0]:
                raise HubError("UNAUTHORIZED", "未确认编辑")
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", dir=service.private, encoding="utf-8") as draft:
                draft.write(candidate["proposed_markdown"])
                draft.flush()
                subprocess.run([*shlex.split(os.environ.get("EDITOR", "vi")), draft.name], check=True)
                result = service.edit(options.proposal[0], Path(draft.name).read_text(encoding="utf-8"))
            try:
                print(render_preview(service.preview(options.proposal)))
            except HubError as error:
                print(safe_display("改稿已保存；仍需修正或同批解决：" + error.message))
        elif command == "history":
            result = service.history(options.record)
        else:
            result = getattr(service, command)()
        print(safe_display(json.dumps(result, ensure_ascii=False, indent=2)))
        return 0
    except HubError as error:
        print(safe_display(error.code + "：" + error.message), file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.SubprocessError):
        print("本机操作失败；已批准批次请用 status / retry 检查，勿重复提交。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
