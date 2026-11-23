from copy import deepcopy
import difflib
import hashlib
import html
import json
import re

from mcp_server.models import HubError
from mcp_server.records import parse_record
from mcp_server.storage import atomic_json, atomic_text, checked_path, read_json, safe_display


def review_item(proposal, current, original, commit, markdown=None):
    record = parse_record(markdown or proposal["proposed_markdown"])
    before = current.markdown if current else ""
    return {
        "proposal_id": proposal["proposal_id"], "target_id": record.id,
        "operation": proposal["operation"], "proposal_base": proposal["base_commit"],
        "base_changed": proposal["base_commit"] != commit,
        "original_markdown": original.markdown if original else "", "current_markdown": before,
        "proposed_markdown": record.markdown, "old_path": current.relative_path if current else None,
        "relative_path": record.relative_path, "basis": proposal["basis"],
        "diff": "".join(difflib.unified_diff(before.splitlines(True), record.markdown.splitlines(True),
                                           fromfile="远端当前内容", tofile="拟批准内容")),
    }


def content_hash(value):
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


def review_digest(preview):
    items = []
    for item in sorted(preview["items"], key=lambda entry: entry["proposal_id"]):
        record = parse_record(item["proposed_markdown"])
        items.append({
            "proposal_id": item["proposal_id"], "target_id": item["target_id"],
            "operation": item["operation"], "proposal_base": item["proposal_base"],
            "basis": item["basis"], "relative_path": item["relative_path"],
            "metadata": {key: value for key, value in record.metadata.items() if key != "updated_at"},
            "body": record.body,
        })
    return content_hash({"base_commit": preview["base_commit"], "remote_url": preview["remote_url"],
                         "branch": preview["branch"], "items": items})


def literal(value, language="text"):
    text = safe_display(str(value))
    fence = "`" * max(3, 1 + max((len(match) for match in re.findall(r"`+", text)), default=0))
    return fence + language + "\n" + text + ("" if text.endswith("\n") else "\n") + fence


def label(value):
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(safe_display(str(value))))


def render_review(preview, format_version=2):
    items = preview["items"]
    errors = preview.get("validation_errors", [])
    warnings = preview.get("warnings", [])
    operations = {"create": "新增", "replace": "更新", "archive": "归档"}
    kinds = {"core": "核心背景／偏好／目标", "project": "项目记录", "lesson": "经验记录", "deliverable": "完整文档"}
    views = {"base": "每次加载", "coding": "编码场景额外加载", "thinking": "分析／写作场景额外加载"}
    records = {item["target_id"]: parse_record(item["proposed_markdown"]) for item in items}
    parts = ["# 知识库入库审核", "", "## 本次变更", "",
             f"本次包含 {len(items)} 条记录。状态：待本人审核；确认前不会提交或推送。",
             "这份材料与 Agent 版来自同一份候选，正文不另行摘要或改写。",
             "校验结果：" + ("存在阻塞，修正后重新生成审核材料。" if errors else "所选记录联合结构、引用和预算校验通过；不代表已经批准。"),
             "", "## 需要你确认", "",
             "- 正文是否准确，适用范围和使用时机是否合适。",
             "- 关联依赖是否齐全，删除／替换／归档是否符合预期。",
             "- 批准会提交并尝试推送；若目标为公开仓库，正文、元数据和文件名均会公开。",
             "- internal 仅表示无外发许可，不会让公开 Git 仓库中的文件变成私有。",
             "- 本材料仅供本机审核，包含内部依据，不是可外发成品。",
             "- 正文、元数据、依赖、目标或远端基线变化须重新审核；updated_at 在人工审核时自动刷新。",
             "", "目标仓库：" + label(preview.get("remote_url", "未提供")),
             "目标分支：" + label(preview.get("branch", "未提供"))]
    if warnings or errors:
        parts.extend(["", "## 提示与阻塞", literal("\n".join([*warnings, *errors]))])
    for index, item in enumerate(items, 1):
        record = records[item["target_id"]]
        metadata = record.metadata
        scope = "所有项目" if metadata["scope"] == ["global"] else "、".join(metadata["scope"])
        dependencies = [*metadata["related_ids"], *metadata["supersedes"]]
        usage = "；".join(views[view] for view in metadata.get("views", [])) or "按项目或关键词需要时读取"
        location = "目标位置：" + label(record.relative_path)
        if format_version == 2:
            category = {"core": "核心资料", "project": "项目记录", "lesson": "经验方法", "deliverable": "完整文档"}[record.type]
            if record.type == "core":
                category = {"preference": "核心偏好", "background": "个人背景", "goal": "个人目标"}[metadata["kind"]]
            location = ("知识分类：" + category + "\n\n文档名称：`" + record.relative_path.rsplit("/", 1)[-1]
                        + "`\n\n目标位置：`" + record.relative_path + "`")
            if item["old_path"] and item["old_path"] != record.relative_path:
                location += "\n\n原位置：`" + item["old_path"] + "`（批准后迁移，记录 ID 不变）"
        parts.extend(["", f"## {index}．{label(metadata['title'])}", "",
                      "操作：" + operations[item["operation"]] + "；类型：" + kinds[record.type],
                      "适用范围：" + label(scope) + "；使用时机：" + usage,
                      location,
                      "关联依赖：" + ("、".join(label(records[reference].metadata["title"] if reference in records else reference)
                                             for reference in dependencies) or "无"),
                      "", "### 为什么保存（本机依据，不写入正式正文）", literal(item["basis"])])
        if record.type == "deliverable":
            parts.extend(["用途：" + label(metadata["purpose"]), "外发许可：" + label(metadata["share_status"]),
                          "受众：" + label("、".join(metadata["audience"]) or "未指定；不授予外发许可")])
        if item["base_changed"]:
            parts.append("注意：远端基线已变化，请对照附录重新审核。")
        parts.extend(["", "### 完整拟保存正文", "", literal(record.body, "markdown"), "",
                      "### 变更说明", "新增记录，无旧正文。" if not item["current_markdown"] else "完整差异见附录，包含正文和元数据变更。"])
    parts.extend(["", "## 技术附录（核对用）", "", "本次远端基线：" + label(preview["base_commit"])])
    for item in items:
        record = records[item["target_id"]]
        parts.extend(["", "### " + label(record.metadata["title"]),
                      "提案：" + label(item["proposal_id"]), "记录：" + label(record.id),
                      "提案原基线：" + label(item["proposal_base"]),
                      "拟批准全文（包含全部元数据，Agent 版）：", literal(record.markdown, "markdown"),
                      "差异：", literal(item["diff"] or "无差异", "diff")])
        if item["current_markdown"]:
            parts.extend(["远端当前全文：", literal(item["current_markdown"], "markdown")])
        if item["original_markdown"] and item["original_markdown"] != item["current_markdown"]:
            parts.extend(["提案原基线全文：", literal(item["original_markdown"], "markdown")])
    return "\n\n".join(part for part in parts if part != "") + "\n"


def bundle_path(config, delivery_id, *, shared=False):
    if not isinstance(delivery_id, str) or not re.fullmatch(r"delivery-[0-9a-f]{64}", delivery_id):
        raise HubError("INVALID_ARGUMENT", "交付标识无效")
    parent = config.proposal_dir
    if config.role == "reviewer" and not shared:
        parent = config.data_root / "review-state" / "private"
    return checked_path(parent / "deliveries" / delivery_id)


def agent_relative_path(record, preview, format_version):
    if format_version == 1:
        return record.id + ".md"
    if sum(item["relative_path"] == record.relative_path for item in preview["items"]) > 1:
        return "conflicts/" + record.id + "/" + record.relative_path
    return record.relative_path


def export_bundle(config, preview):
    preview = deepcopy(preview)
    preview["items"].sort(key=lambda item: item["proposal_id"])
    manifest = {"format_version": 2, "preview": preview, "review_digest": review_digest(preview)}
    delivery_id = "delivery-" + content_hash(manifest)
    root = bundle_path(config, delivery_id)
    directory_mode = 0o700 if config.role == "reviewer" else 0o750
    file_mode = 0o600 if config.role == "reviewer" else 0o640
    for directory in (root.parent, root, root / "agent"):
        checked_path(directory).mkdir(mode=directory_mode, parents=True, exist_ok=True)
    paths = []
    for item in preview["items"]:
        record = parse_record(item["proposed_markdown"])
        destination = root / "agent" / agent_relative_path(record, preview, 2)
        parent = root / "agent"
        for component in destination.relative_to(parent).parts[:-1]:
            parent = checked_path(parent / component)
            parent.mkdir(mode=directory_mode, exist_ok=True)
        atomic_text(destination, record.markdown, mode=file_mode)
        paths.append(str(destination))
    human_path = root / "REVIEW.md"
    atomic_text(human_path, render_review(preview), mode=file_mode)
    atomic_json(root / "manifest.json", manifest, mode=file_mode)
    return {"delivery_id": delivery_id, "human_path": str(human_path), "agent_paths": paths,
            "review_digest": manifest["review_digest"], "snapshot_commit": preview["base_commit"],
            "proposal_ids": [item["proposal_id"] for item in preview["items"]],
            "validation_passed": not preview.get("validation_errors"), "state": "pending",
            "warnings": preview.get("warnings", []), "validation_errors": preview.get("validation_errors", [])}


def load_bundle(config, delivery_id):
    root = bundle_path(config, delivery_id)
    if config.role == "reviewer" and not root.exists():
        root = bundle_path(config, delivery_id, shared=True)
    try:
        manifest = read_json(root / "manifest.json")
        if not isinstance(manifest, dict) or "delivery-" + content_hash(manifest) != delivery_id:
            raise ValueError
        if set(manifest) != {"format_version", "preview", "review_digest"} or type(manifest["format_version"]) is not int or manifest["format_version"] not in (1, 2):
            raise ValueError
        preview = manifest["preview"]
        if review_digest(preview) != manifest["review_digest"]:
            raise ValueError
        if checked_path(root / "REVIEW.md").read_text(encoding="utf-8") != render_review(preview, manifest["format_version"]):
            raise ValueError
        for item in preview["items"]:
            record = parse_record(item["proposed_markdown"])
            relative_path = agent_relative_path(record, preview, manifest["format_version"])
            if checked_path(root / "agent" / relative_path).read_text(encoding="utf-8") != record.markdown:
                raise ValueError
        return manifest
    except (OSError, ValueError, TypeError, KeyError, HubError):
        raise HubError("REVIEW_CHANGED", "交付材料缺失、被修改或无效；请重新生成并审核") from None
