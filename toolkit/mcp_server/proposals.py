import hashlib
import json
import uuid

from mcp_server.models import HubError
from mcp_server.delivery import export_bundle, review_item
from mcp_server.records import ID_PATTERN, dump_record, parse_record, same_topic, validate_records
from mcp_server.storage import atomic_json, checked_path, ensure_directory, file_lock, read_json, utc_now
from review.sync import check_commit


def fingerprint(record, operation):
    metadata = {key: value for key, value in record.metadata.items() if key != "updated_at"}
    content = json.dumps([operation, metadata, record.body], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()


def proposal_path(directory, proposal_id):
    if not isinstance(proposal_id, str) or not ID_PATTERN.fullmatch(proposal_id):
        raise HubError("INVALID_RECORD", "提案标识无效")
    return checked_path(directory / (proposal_id + ".json"))


def validate_operation(operation, target_id, record, records):
    if not isinstance(operation, str) or operation not in {"create", "replace", "archive"} or record.id != target_id:
        raise HubError("INVALID_RECORD", "操作或目标 ID 与候选记录不匹配")
    existing = records.get(target_id)
    if (operation == "create" and existing) or (operation != "create" and not existing):
        raise HubError("INVALID_RECORD", "创建目标已存在或更新目标不存在")
    if existing and existing.type != record.type:
        raise HubError("INVALID_RECORD", "已有记录的类型不能改变")
    if existing and "slug" in existing.metadata and "slug" not in record.metadata:
        raise HubError("INVALID_RECORD", "更新必须保留已有 slug；不能因漏传字段将语义文件名退回 ID")
    if existing and existing.type == "deliverable":
        events = {event["id"]: event for event in record.metadata["publication_events"]}
        if any(events.get(event["id"]) != event for event in existing.metadata["publication_events"]):
            raise HubError("INVALID_RECORD", "覆盖文档必须完整保留历史发布事实；只允许追加新事件")
    if operation == "archive" and record.status not in {"archived", "superseded"}:
        raise HubError("INVALID_RECORD", "归档操作必须提交归档或被取代状态")


def read_proposal(path):
    proposal = read_json(path)
    required = {"proposal_id", "trigger", "basis", "operation", "target_id", "base_commit", "proposed_markdown", "state"}
    if not isinstance(proposal, dict) or not required <= proposal.keys() or any(not isinstance(proposal[key], str) for key in required):
        raise HubError("INVALID_RECORD", "待审提案必须包含完整的字符串字段")
    if proposal["proposal_id"] != path.stem or proposal["trigger"] not in {"manual", "auto"} or proposal["state"] != "pending":
        raise HubError("INVALID_RECORD", "待审提案标识或状态无效")
    proposal_path(path.parent, proposal["proposal_id"])
    check_commit(proposal["base_commit"])
    record = parse_record(proposal["proposed_markdown"])
    if proposal["operation"] not in {"create", "replace", "archive"} or record.id != proposal["target_id"]:
        raise HubError("INVALID_RECORD", "待审提案操作与目标无效")
    proposal["fingerprint"] = fingerprint(record, proposal["operation"])
    return proposal


class ProposalStore:
    def __init__(self, config, snapshots):
        self.config = config
        self.snapshots = snapshots

    def propose(self, trigger, basis, operation, target_id, base_commit, proposed_markdown):
        if not isinstance(trigger, str) or trigger not in {"manual", "auto"} or not isinstance(basis, str):
            raise HubError("INVALID_RECORD", "提案触发来源或依据无效")
        if not isinstance(target_id, str) or not ID_PATTERN.fullmatch(target_id):
            raise HubError("INVALID_RECORD", "提案目标 ID 无效")
        snapshot = self.snapshots.get(base_commit)
        record = parse_record(proposed_markdown)
        validate_operation(operation, target_id, record, snapshot.records)
        if operation == "create" and "slug" not in record.metadata:
            raise HubError("INVALID_RECORD", "新记录须提供有语义的 slug；ID 仅用于稳定引用，不再作为新文档的默认文件名")
        for previous in snapshot.records.values():
            if same_topic(record, previous):
                return self.merge_required(previous, "replace", snapshot.commit)
        candidate = {**snapshot.records, target_id: record}
        required_updates = []
        for reference in record.metadata["supersedes"]:
            previous = candidate.get(reference)
            if previous and previous.status != "superseded":
                required_updates.append(reference)
                candidate[reference] = parse_record(dump_record({**previous.metadata, "status": "superseded"}, previous.body))
        try:
            unresolved = validate_records(candidate, allow_unresolved=True)
        except HubError as error:
            if error.code == "BUDGET_EXCEEDED":
                return {**error.as_dict(), "preview": record.markdown}
            raise
        digest = fingerprint(record, operation)
        existing = snapshot.records.get(target_id)
        if existing and fingerprint(existing, operation) == digest:
            return {"ok": True, "data": {"result": "no_change"}, "warnings": []}
        ensure_directory(self.config.proposal_dir)
        warnings = ["同批次仍须将以下旧记录改为 superseded：" + ", ".join(required_updates)] if required_updates else []
        with file_lock(self.config.proposal_dir / "proposals.lock"):
            for path in sorted(self.config.proposal_dir.glob("*.json")):
                try:
                    proposal = read_proposal(path)
                except HubError:
                    warnings.append("提案区存在无效文件，已跳过；请在审核列表检查")
                    continue
                status = read_json(proposal_path(self.config.review_status_dir, proposal.get("proposal_id", "")), {})
                if proposal.get("fingerprint") == digest and proposal.get("state") == "pending" and status.get("state", "pending") == "pending":
                    return self.result(proposal, unresolved, "deduplicated", warnings, required_updates, snapshot)
                if proposal.get("fingerprint") == digest and status.get("state") in {"approved", "committed", "pending_sync", "needs_review"}:
                    return {"ok": True, "data": {"proposal_id": proposal["proposal_id"], "result": "already_approved", **status}, "warnings": []}
                previous = parse_record(proposal["proposed_markdown"])
                if status.get("state", "pending") in {"pending", "approved", "committed", "pending_sync", "needs_review"} and same_topic(record, previous):
                    return self.merge_required(previous, proposal["operation"], proposal["base_commit"],
                                               proposal["proposal_id"], status.get("state", "pending"))
            proposal = {"proposal_id": "proposal-" + uuid.uuid4().hex, "created_at": utc_now(), "trigger": trigger,
                        "operation": operation, "target_id": target_id, "base_commit": base_commit,
                        "proposed_markdown": record.markdown, "basis": basis, "state": "pending", "fingerprint": digest}
            atomic_json(proposal_path(self.config.proposal_dir, proposal["proposal_id"]), proposal, mode=0o640)
            return self.result(proposal, unresolved, "created", warnings, required_updates, snapshot)

    @staticmethod
    def merge_required(record, operation, base_commit, proposal_id=None, state="active"):
        response = HubError("DUPLICATE_TOPIC", "同范围、同主题或同标题已有文档；先读取完整候选，合并新增内容并保留原 ID，不能另建文档。已批准待同步时须先完成原批次。").as_dict()
        response["merge_target"] = {"target_id": record.id, "operation": operation, "base_commit": base_commit,
                                    "proposal_id": proposal_id, "state": state, "relative_path": record.relative_path,
                                    "proposed_markdown": record.markdown}
        return response

    def result(self, proposal, unresolved, result, warnings, required_updates, snapshot):
        response = {"ok": True, "data": {"proposal_id": proposal["proposal_id"], "result": result,
                                          "preview": proposal["proposed_markdown"], "unresolved_ids": unresolved,
                                          "required_updates": required_updates},
                    "warnings": warnings + (["引用尚未批准，须在审核批次中完整解析"] if unresolved else [])}
        try:
            response["data"]["delivery"] = self._review_bundle([proposal["proposal_id"]], snapshot)
        except (HubError, OSError):
            response["warnings"].append("提案已保存，但双视图交付生成失败；请调用 prepare_review_bundle 重试")
        return response

    def prepare_review_bundle(self, proposal_ids, snapshot_commit=None):
        if not isinstance(proposal_ids, list) or not proposal_ids or any(
                not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier) for identifier in proposal_ids):
            raise HubError("INVALID_ARGUMENT", "须提供非空的有效提案 ID 列表")
        if len(proposal_ids) != len(set(proposal_ids)):
            raise HubError("INVALID_ARGUMENT", "提案 ID 不可重复")
        snapshot = self.snapshots.get(snapshot_commit)
        with file_lock(self.config.proposal_dir / "proposals.lock"):
            delivery = self._review_bundle(proposal_ids, snapshot)
        return {"ok": True, "data": delivery, "snapshot_commit": snapshot.commit,
                "sync_status": snapshot.sync_status, "last_synced_at": snapshot.last_synced_at,
                "warnings": delivery["warnings"]}

    def _review_bundle(self, proposal_ids, snapshot):
        candidate = dict(snapshot.records)
        targets = set()
        items = []
        for proposal_id in proposal_ids:
            path = proposal_path(self.config.proposal_dir, proposal_id)
            if not path.exists():
                raise HubError("NOT_FOUND", "提案不存在")
            proposal = read_proposal(path)
            status = read_json(proposal_path(self.config.review_status_dir, proposal_id), {})
            if status.get("state", "pending") != "pending":
                raise HubError("INVALID_STATE", "仅能为待审提案生成交付；已批准或拒绝的提案请查看审核状态")
            record = parse_record(proposal["proposed_markdown"])
            if record.id in targets:
                raise HubError("INVALID_RECORD", "同一交付不能重复修改相同记录")
            targets.add(record.id)
            validate_operation(proposal["operation"], record.id, record, snapshot.records)
            current = snapshot.records.get(record.id)
            if proposal["base_commit"] == snapshot.commit:
                original = current
            else:
                if not self.snapshots.repository.is_ancestor(proposal["base_commit"], snapshot.commit):
                    raise HubError("BASE_CHANGED", "提案基线不属于交付快照历史")
                original = self.snapshots.repository.records(proposal["base_commit"]).get(record.id)
            candidate[record.id] = record
            items.append(review_item(proposal, current, original, snapshot.commit))
        errors = []
        try:
            unresolved = validate_records(candidate, allow_unresolved=True)
            if unresolved:
                errors.append("INVALID_REFERENCE：缺少同批依赖：" + "、".join(unresolved))
        except HubError as error:
            errors.append(error.code + "：" + error.message)
        preview = {"base_commit": snapshot.commit, "items": items, "remote_url": self.config.remote_url,
                   "branch": self.config.branch, "validation_errors": errors,
                   "warnings": [*snapshot.warnings, "读取侧固定快照交付；不代表已核实批准时的远端。人工审核区如有改稿，以本人重新预览为准。"]}
        return export_bundle(self.config, preview)
