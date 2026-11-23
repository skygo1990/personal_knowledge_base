import yaml


PROJECT_BODY = "\n".join(
    f"## {heading}\n内容\n"
    for heading in ("目标与范围", "当前状态", "下一步", "阻塞与未决问题", "验证依据")
)

LESSON_HEADINGS = {
    "decision": ("背景", "选择", "理由", "适用条件", "重审条件"),
    "experiment": ("问题", "尝试", "观察结果", "已验证与未验证", "局限"),
    "method": ("触发场景", "输入", "步骤", "输出", "示例"),
}


def make_markdown(record_id="proj-alpha", record_type="project", body=None, **overrides):
    metadata = {
        "id": record_id,
        "type": record_type,
        "title": "测试记录" if record_id == "proj-alpha" else "测试记录 " + record_id,
        "slug": record_id,
        "scope": [record_id] if record_type == "project" else ["global"],
        "status": "active",
        "updated_at": "2026-11-20T09:00:00+08:00",
    }
    if record_type == "project":
        metadata["summary"] = "测试项目目标"
    elif record_type == "core":
        metadata.update(kind="preference", views=["base"], strength="preference")
    elif record_type == "lesson":
        metadata["kind"] = "decision"
    elif record_type == "deliverable":
        metadata.update(format="markdown", audience=["parents"], purpose="测试用途", share_status="ready")
    metadata.update(overrides)
    if record_type == "core" and metadata.get("kind") != "preference":
        metadata.pop("strength", None)
    if body is None:
        if record_type == "project":
            body = PROJECT_BODY
        elif record_type == "lesson":
            body = "\n".join(f"## {heading}\n内容\n" for heading in LESSON_HEADINGS[metadata["kind"]])
        else:
            body = "完整正文\n"
    return "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + body
