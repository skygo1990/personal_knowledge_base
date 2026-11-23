import math
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import yaml

from mcp_server.models import HubError, Record


ID_PATTERN = re.compile(r"[a-z0-9-]{3,80}\Z")
TIME_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z")
CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
TYPE_DIRECTORIES = {
    "core": "L0_core", "project": "L1_projects", "lesson": "L2_lessons", "deliverable": "deliverables"
}
COMMON_FIELDS = {
    "id", "type", "kind", "title", "scope", "status", "updated_at", "tags", "aliases",
    "related_ids", "project_id", "supersedes", "evidence_summary", "slug",
}
TYPE_FIELDS = {
    "core": {"views", "strength"},
    "project": {"summary", "constraints"},
    "lesson": set(),
    "deliverable": {"format", "audience", "purpose", "share_status", "publication_events"},
}
REQUIRED_HEADINGS = {
    "project": {"目标与范围", "当前状态", "下一步", "阻塞与未决问题", "验证依据"},
    "decision": {"背景", "选择", "理由", "适用条件", "重审条件"},
    "experiment": {"问题", "尝试", "观察结果", "已验证与未验证", "局限"},
    "method": {"触发场景", "输入", "步骤", "输出", "示例"},
}


def _invalid(message: str, code: str = "INVALID_RECORD") -> None:
    raise HubError(code, message) from None


class _StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            _invalid("YAML 不允许别名引用。")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            _invalid("YAML 字段结构无效。")
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in mapping:
                _invalid("YAML 字段必须是唯一字符串。")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _check_text(value: str) -> None:
    if CONTROL_PATTERN.search(value) or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _invalid("内容包含禁止的控制字符或无效 Unicode。")


def _check_nested(value) -> None:
    if isinstance(value, str):
        _check_text(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _invalid("元数据字段名必须是字符串。")
            _check_text(key)
            _check_nested(item)
    elif isinstance(value, list):
        for item in value:
            _check_nested(item)


def _string(value, field: str, *, multiline=False, empty=False, code="INVALID_RECORD") -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        _invalid(f"{field} 必须是有效字符串。", code)
    _check_text(value)
    if not multiline and ("\n" in value or "\r" in value or "\u2028" in value or "\u2029" in value):
        _invalid(f"{field} 必须为单行。", code)
    return value


def _identifier(value, field="id", code="INVALID_RECORD") -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        _invalid(f"{field} 必须为 3～80 位小写字母、数字或连字符。", code)
    return value


def _slug(value) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 80
            or not re.fullmatch(r"[a-z0-9\u4e00-\u9fff]+(?:-[a-z0-9\u4e00-\u9fff]+)*", value)
            or re.fullmatch(r"(?:[a-z]+-)?[0-9a-f]{32,64}", value)):
        _invalid("slug 须为有语义的中文或小写英文名称，可含数字和单连字符，最长 80 字；不能用随机 ID、路径或扩展名。")
    return value


def same_topic(left, right) -> bool:
    if left.id == right.id or left.status != "active" or right.status != "active" or left.type != right.type:
        return False
    if left.type != "project" and set(left.metadata["scope"]) != set(right.metadata["scope"]):
        return False
    if left.metadata.get("kind") != right.metadata.get("kind"):
        return False
    left_title = " ".join(unicodedata.normalize("NFKC", left.metadata["title"]).casefold().split())
    right_title = " ".join(unicodedata.normalize("NFKC", right.metadata["title"]).casefold().split())
    return left_title == right_title or bool(left.metadata.get("slug") == right.metadata.get("slug")
                                             and left.metadata.get("slug"))


def _enum(value, choices, field: str, code="INVALID_RECORD") -> str:
    if not isinstance(value, str) or value not in choices:
        _invalid(f"{field} 的取值无效。", code)
    return value


def _strings(value, field: str, *, nonempty=False, identifiers=False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        _invalid(f"{field} 必须为有效列表。")
    for item in value:
        if identifiers:
            _identifier(item, field)
        else:
            _string(item, field)
    if len(value) != len(set(value)):
        _invalid(f"{field} 不允许重复条目。")
    return value


def _timestamp(value, field: str, code="INVALID_RECORD") -> datetime:
    if not isinstance(value, str) or not TIME_PATTERN.fullmatch(value):
        _invalid(f"{field} 必须为带时区的 RFC 3339 字符串。", code)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _invalid(f"{field} 的日期或时区无效。", code)


def _validate_constraints(constraints) -> None:
    code = "CONSTRAINT_CONFLICT"
    if not isinstance(constraints, list):
        _invalid("constraints 必须为列表。", code)
    by_id = {}
    baselines = {}
    exceptions = []
    for constraint in constraints:
        if not isinstance(constraint, dict):
            _invalid("约束必须为字段对象。", code)
        kind = _enum(constraint.get("kind"), {"baseline", "exception"}, "constraint.kind", code)
        fields = {"id", "key", "kind", "value"}
        if kind == "exception":
            fields |= {"overrides", "valid_from", "valid_until"}
        if set(constraint) != fields:
            _invalid("约束字段缺失或包含未知字段。", code)
        constraint_id = _identifier(constraint["id"], "constraint.id", code)
        key = _string(constraint["key"], "constraint.key", code=code)
        if constraint_id in by_id:
            _invalid("本项目约束 ID 重复。", code)
        by_id[constraint_id] = constraint
        value = constraint["value"]
        if value is not None and type(value) not in (str, int, float, bool):
            _invalid("约束值必须是简单标量。", code)
        if isinstance(value, float) and not math.isfinite(value):
            _invalid("约束数值必须是有限数。", code)
        if kind == "baseline":
            if key in baselines:
                _invalid("同项目同 key 只能有一个常规约束。", code)
            baselines[key] = constraint_id
        else:
            _identifier(constraint["overrides"], "constraint.overrides", code)
            start = _timestamp(constraint["valid_from"], "constraint.valid_from", code)
            end = _timestamp(constraint["valid_until"], "constraint.valid_until", code)
            if start >= end:
                _invalid("例外时间区间必须开始早于结束。", code)
            exceptions.append((key, start, end, constraint))
    intervals = {}
    for key, start, end, constraint in exceptions:
        if baselines.get(key) != constraint["overrides"]:
            _invalid("例外必须引用本项目相同 key 的常规约束。", code)
        intervals.setdefault(key, []).append((start, end))
    for periods in intervals.values():
        periods.sort()
        for previous, current in zip(periods, periods[1:]):
            if previous[1] > current[0]:
                _invalid("同一约束的例外时间区间重叠。", code)


def _validate_publications(events) -> None:
    if not isinstance(events, list):
        _invalid("publication_events 必须为列表。")
    seen = set()
    required = {"id", "published_at", "channel", "audience", "result"}
    for event in events:
        if not isinstance(event, dict) or not required <= set(event) or set(event) - required - {"source_commit"}:
            _invalid("发布事件字段缺失或包含未知字段。")
        event_id = _identifier(event["id"], "publication.id")
        if event_id in seen:
            _invalid("发布事件 ID 重复。")
        seen.add(event_id)
        _timestamp(event["published_at"], "publication.published_at")
        _string(event["channel"], "publication.channel")
        _strings(event["audience"], "publication.audience", nonempty=True)
        _string(event["result"], "publication.result", multiline=True)
        if "source_commit" in event:
            commit = event["source_commit"]
            if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
                _invalid("publication.source_commit 必须为完整 Git 提交 ID。")


def _validate_metadata(metadata: dict) -> None:
    if not isinstance(metadata, dict):
        _invalid("YAML front matter 必须为字段对象。")
    _check_nested(metadata)
    record_type = _enum(metadata.get("type"), TYPE_DIRECTORIES, "type")
    if set(metadata) - COMMON_FIELDS - TYPE_FIELDS[record_type]:
        _invalid("元数据包含未知字段或不适用于此类型的字段。")
    record_id = _identifier(metadata.get("id"))
    if "slug" in metadata:
        _slug(metadata["slug"])
    _enum(metadata.get("status"), {"active", "archived", "superseded"}, "status")
    _string(metadata.get("title"), "title")
    _timestamp(metadata.get("updated_at"), "updated_at")
    scope = _strings(metadata.get("scope"), "scope", nonempty=True, identifiers=True)
    if "global" in scope and len(scope) != 1:
        _invalid("scope 不能混写 global 与项目 ID。")
    if record_type == "project" and scope != [record_id]:
        _invalid("项目 scope 必须且只能是自身 ID。")
    for field in ("tags", "aliases", "related_ids", "supersedes"):
        metadata.setdefault(field, [])
        _strings(metadata[field], field, identifiers=field in {"related_ids", "supersedes"})
    if record_id in metadata["supersedes"]:
        _invalid("supersedes 不能自引用。")
    if "project_id" in metadata:
        if record_type not in {"lesson", "deliverable"}:
            _invalid("project_id 只适用于经验和成品。")
        _identifier(metadata["project_id"], "project_id")
    if "evidence_summary" in metadata:
        _string(metadata["evidence_summary"], "evidence_summary", multiline=True, empty=True)
    if record_type == "core":
        kind = _enum(metadata.get("kind"), {"background", "preference", "goal"}, "kind")
        for view in _strings(metadata.get("views"), "views", nonempty=True):
            _enum(view, {"base", "coding", "thinking"}, "views")
        if kind == "preference":
            _enum(metadata.get("strength"), {"preference", "constraint"}, "strength")
        elif "strength" in metadata:
            _invalid("strength 只适用于 preference。")
    elif record_type == "lesson":
        _enum(metadata.get("kind"), {"decision", "experiment", "method"}, "kind")
    elif "kind" in metadata:
        _invalid("kind 只适用于 core 和 lesson。")
    if record_type == "project":
        _string(metadata.get("summary"), "summary")
        metadata.setdefault("constraints", [])
        _validate_constraints(metadata["constraints"])
    elif record_type == "deliverable":
        _enum(metadata.get("format"), {"markdown"}, "format")
        _string(metadata.get("purpose"), "purpose")
        _enum(metadata.get("share_status"), {"internal", "ready", "withdrawn"}, "share_status")
        _strings(metadata.get("audience"), "audience", nonempty=metadata["share_status"] == "ready")
        metadata.setdefault("publication_events", [])
        _validate_publications(metadata["publication_events"])


def _headings(body: str) -> set[str]:
    headings = set()
    fence = None
    in_comment = False
    previous = ""
    for line in body.splitlines():
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                fence = None
            continue
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if not in_comment and fence_match:
            fence = fence_match.group(1)
            previous = ""
            continue
        visible = ""
        remaining = line
        while remaining:
            if in_comment:
                ending = remaining.find("-->")
                if ending == -1:
                    break
                in_comment = False
                remaining = remaining[ending + 3:]
            else:
                beginning = remaining.find("<!--")
                if beginning == -1:
                    visible += remaining
                    break
                visible += remaining[:beginning]
                remaining = remaining[beginning + 4:]
                in_comment = True
        line = visible
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence_match:
            fence = fence_match.group(1)
            previous = ""
            continue
        heading_match = re.match(r"^ {0,3}#{1,6}[ \t]+(.+?)\s*$", line)
        if heading_match:
            headings.add(re.sub(r"[ \t]+#+[ \t]*$", "", heading_match.group(1)))
        elif previous and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", line):
            headings.add(previous.strip())
        previous = line if line.strip() and not line.startswith(("    ", "\t")) else ""
    return headings


def _validate_body(metadata: dict, body: str) -> None:
    _string(body, "正文", multiline=True)
    heading_key = metadata["type"] if metadata["type"] == "project" else metadata.get("kind")
    required = REQUIRED_HEADINGS.get(heading_key, set())
    if not required <= _headings(body):
        _invalid("正文缺少本记录类型要求的标题。")


def record_path(metadata: dict) -> str:
    if not isinstance(metadata, dict):
        _invalid("记录元数据必须为字段对象。")
    record_id = _identifier(metadata.get("id"))
    record_type = _enum(metadata.get("type"), TYPE_DIRECTORIES, "type")
    status = _enum(metadata.get("status"), {"active", "archived", "superseded"}, "status")
    directory = TYPE_DIRECTORIES[record_type] if status == "active" else f"L3_archive/{record_type}"
    filename = _slug(metadata["slug"]) if "slug" in metadata else record_id
    return f"knowledge/{directory}/{filename}.md"


def parse_record(markdown: str, relative_path: str | None = None) -> Record:
    if not isinstance(markdown, str):
        _invalid("记录必须为 UTF-8 Markdown 文本。")
    markdown = markdown.replace("\r\n", "\n")
    _check_text(markdown)
    if not markdown.startswith("---\n"):
        _invalid("记录必须以 YAML front matter 开始。")
    delimiter = re.search(r"^---(?:\n|$)", markdown[4:], re.MULTILINE)
    if delimiter is None:
        _invalid("YAML front matter 缺少结束标记。")
    front_matter = markdown[4:4 + delimiter.start()]
    body = markdown[4 + delimiter.end():]
    try:
        metadata = yaml.load(front_matter, Loader=_StrictLoader)
    except (yaml.YAMLError, RecursionError, ValueError, OverflowError, TypeError, KeyError, IndexError, AttributeError):
        _invalid("YAML 内容无法安全解析。")
    try:
        _validate_metadata(metadata)
    except RecursionError:
        _invalid("YAML 内容嵌套过深。")
    _validate_body(metadata, body)
    expected = record_path(metadata)
    if relative_path is not None and relative_path != expected:
        _invalid("记录路径与类型、状态或 ID 不一致。")
    return Record(metadata=metadata, body=body, markdown=markdown, relative_path=expected)


def dump_record(metadata: dict, body: str) -> str:
    if not isinstance(body, str):
        _invalid("正文必须为字符串。")
    try:
        serialized = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
    except (yaml.YAMLError, RecursionError, ValueError):
        _invalid("记录元数据无法安全序列化。")
    return parse_record("---\n" + serialized + "---\n" + body).markdown


def body_count(body: str) -> int:
    return sum(not character.isspace() for character in body)


def _validate_budget(records: dict[str, Record]) -> None:
    active_core = [record for record in records.values() if record.type == "core" and record.status == "active"]
    projects = {record.id for record in records.values() if record.type == "project"}
    projects.update(project_id for record in active_core for project_id in record.metadata["scope"] if project_id != "global")
    for project_id in [None, *sorted(projects)]:
        selected = [record for record in active_core if "global" in record.metadata["scope"] or project_id in record.metadata["scope"]]
        counts = {view: 0 for view in ("base", "coding", "thinking")}
        for record in selected:
            views = record.metadata["views"]
            for view in ["base"] if "base" in views else views:
                counts[view] += body_count(record.body)
        for view, limit in (("base", 400), ("coding", 300), ("thinking", 300)):
            if counts[view] > limit:
                _invalid(f"L0 {view} 适用组合为 {counts[view]} 字，超过 {limit} 字预算。", "BUDGET_EXCEEDED")


def _same_value(expected, actual) -> bool:
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(_same_value(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(_same_value(left, right) for left, right in zip(expected, actual))
    return expected == actual


def validate_records(records: dict[str, Record], allow_unresolved: bool = False) -> list[str]:
    if not isinstance(records, dict):
        _invalid("记录集合必须以 ID 索引。")
    unresolved = set()
    paths = set()
    for record_id, record in records.items():
        if not isinstance(record, Record) or not isinstance(record.metadata, dict) or record_id != record.metadata.get("id"):
            _invalid("记录索引与记录 ID 不一致。")
        if not isinstance(record.relative_path, str):
            _invalid("记录必须具有规范的仓库相对路径。")
        checked = parse_record(record.markdown, record.relative_path)
        if not _same_value(checked.metadata, record.metadata) or not _same_value(checked.body, record.body):
            _invalid("记录正文或元数据与 Markdown 不一致。")
        if record.relative_path in paths:
            _invalid("不同记录不能占用同一文件路径；请合并同主题记录，或在名称中明确不同适用范围。", "DUPLICATE_PATH")
        paths.add(record.relative_path)
    previous_records = []
    for record in records.values():
        for previous in previous_records:
            if ("slug" in record.metadata or "slug" in previous.metadata) and same_topic(record, previous):
                _invalid(f"相同范围已有同主题或同标题记录 {previous.id}；须合并后更新原 ID。", "DUPLICATE_TOPIC")
        previous_records.append(record)
    for record in records.values():
        project_refs = [project_id for project_id in record.metadata["scope"] if project_id != "global"]
        if "project_id" in record.metadata:
            project_refs.append(record.metadata["project_id"])
        for reference in [*project_refs, *record.metadata["related_ids"], *record.metadata["supersedes"]]:
            if reference not in records:
                unresolved.add(reference)
        for reference in project_refs:
            if reference in records and records[reference].type != "project":
                _invalid("scope 或 project_id 必须引用项目。", "INVALID_REFERENCE")
        for reference in record.metadata["supersedes"]:
            if reference in records and records[reference].status != "superseded":
                _invalid("被替代记录必须在最终记录集合中为 superseded。", "INVALID_REFERENCE")
    indegree = dict.fromkeys(records, 0)
    for record in records.values():
        for reference in record.metadata["supersedes"]:
            if reference in indegree:
                indegree[reference] += 1
    pending = [record_id for record_id, degree in indegree.items() if degree == 0]
    visited = 0
    while pending:
        record_id = pending.pop()
        visited += 1
        for reference in records[record_id].metadata["supersedes"]:
            if reference in indegree:
                indegree[reference] -= 1
                if indegree[reference] == 0:
                    pending.append(reference)
    if visited != len(records):
        _invalid("supersedes 不允许形成循环。", "INVALID_REFERENCE")
    if unresolved and not allow_unresolved:
        _invalid("记录包含未解析的引用。", "INVALID_REFERENCE")
    _validate_budget(records)
    return sorted(unresolved)


def load_snapshot(root: Path) -> dict[str, Record]:
    root = Path(root)
    knowledge = root / "knowledge"
    if knowledge.is_symlink():
        _invalid("知识快照不允许符号链接。")
    if not knowledge.exists():
        return {}
    if not knowledge.is_dir():
        _invalid("knowledge 必须为目录。")
    allowed_directories = {"knowledge", "knowledge/L3_archive"}
    allowed_directories.update(f"knowledge/{directory}" for directory in TYPE_DIRECTORIES.values())
    allowed_directories.update(f"knowledge/L3_archive/{record_type}" for record_type in TYPE_DIRECTORIES)
    records = {}
    pending = [knowledge]
    try:
        while pending:
            directory = pending.pop()
            for entry in sorted(directory.iterdir()):
                relative_path = entry.relative_to(root).as_posix()
                if entry.is_symlink():
                    _invalid("知识快照不允许符号链接。")
                if entry.is_dir():
                    if relative_path not in allowed_directories:
                        _invalid("知识快照包含未允许的目录。")
                    pending.append(entry)
                elif entry.is_file() and entry.suffix == ".md":
                    record = parse_record(entry.read_bytes().decode("utf-8"), relative_path)
                    if record.id in records:
                        _invalid("知识快照存在重复记录 ID。")
                    records[record.id] = record
                else:
                    _invalid("知识快照包含未允许的文件。")
    except (OSError, UnicodeError):
        _invalid("知识快照无法按 UTF-8 安全读取。")
    validate_records(records)
    return records
