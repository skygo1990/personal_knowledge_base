from copy import deepcopy
from pathlib import Path
import tempfile
import uuid

from mcp_server.models import HubError
from mcp_server.delivery import export_bundle, load_bundle, review_digest, review_item
from mcp_server.proposals import proposal_path, read_proposal, validate_operation
from mcp_server.records import dump_record, parse_record, validate_records
from mcp_server.storage import atomic_json, checked_path, file_lock, read_json, utc_now
from review.sync import GitRepository, check_commit


TERMINAL = {"synced", "cancelled"}
CONVERSATION_CONFIRMATION = "确认将此交付入库并推送至配置仓库"


class ReviewService:
    def __init__(self, config):
        if config.role != "reviewer":
            raise HubError("UNAUTHORIZED", "人工审核必须使用 reviewer 配置")
        self.config = config
        self.repository = GitRepository(config)
        self.private = config.data_root / "review-state" / "private"
        self.journal_path = self.private / "journal.json"
        self.lock_path = self.private / "review.lock"

    def _journal(self):
        return read_json(self.journal_path, {"batches": {}, "outcomes": {}})

    def _save(self, journal):
        atomic_json(self.journal_path, journal)
        self._project_status(journal)

    def _project_status(self, journal):
        for proposal_id, outcome in journal["outcomes"].items():
            status = dict(outcome)
            if outcome.get("batch_id"):
                batch = journal["batches"][outcome["batch_id"]]
                status["state"] = "pending" if batch["state"] == "cancelled" else batch["state"]
            atomic_json(proposal_path(self.config.review_status_dir, proposal_id), status, mode=0o640)

    def _import(self, proposal_id):
        destination = proposal_path(self.private / "imports", proposal_id)
        proposal = read_json(destination)
        if proposal is None:
            source = proposal_path(self.config.proposal_dir, proposal_id)
            proposal = read_proposal(source)
            required = {"trigger", "basis", "operation", "target_id", "base_commit", "proposed_markdown"}
            if not required <= proposal.keys() or proposal["trigger"] not in {"manual", "auto"} or not isinstance(proposal["basis"], str):
                raise HubError("INVALID_RECORD", "待审提案结构无效")
            check_commit(proposal["base_commit"])
            record = parse_record(proposal["proposed_markdown"])
            if record.id != proposal["target_id"] or proposal["operation"] not in {"create", "replace", "archive"}:
                raise HubError("INVALID_RECORD", "待审提案目标无效")
            proposal = {key: proposal[key] for key in required}
            proposal["proposal_id"] = proposal_id
            atomic_json(destination, proposal)
        return proposal

    def _fetch(self, journal):
        commit = self.repository.fetch()
        previous = journal.get("last_remote")
        if previous and not self.repository.is_ancestor(previous, commit):
            raise HubError("SYNC_FAILED", "远端历史不连续，停止审核写入")
        self.repository.records(commit)
        for batch in journal["batches"].values():
            self._remote_batch(batch, commit)
        journal["last_remote"] = commit
        self._save(journal)
        return commit

    def _prepare(self, proposal_ids, commit, share_confirm=None):
        if not proposal_ids or len(set(proposal_ids)) != len(proposal_ids):
            raise HubError("INVALID_RECORD", "须选择不重复的提案")
        records = self.repository.records(commit)
        candidate = dict(records)
        items = []
        targets = set()
        for proposal_id in proposal_ids:
            proposal = self._import(proposal_id)
            record = parse_record(proposal["proposed_markdown"])
            if record.id in targets:
                raise HubError("INVALID_RECORD", "同一批次不能两次覆盖相同 ID")
            targets.add(record.id)
            validate_operation(proposal["operation"], record.id, record, records)
            if not self.repository.is_ancestor(proposal["base_commit"], commit):
                raise HubError("BASE_CHANGED", "提案基线不属于当前远端历史")
            original_records = self.repository.records(proposal["base_commit"])
            original = original_records.get(record.id)
            current = records.get(record.id)
            metadata = deepcopy(record.metadata)
            metadata["updated_at"] = utc_now()
            if record.type == "deliverable" and metadata["share_status"] == "ready":
                permitted = share_confirm is not None and share_confirm(record) is True
                if not permitted:
                    metadata["share_status"] = "internal"
            record = parse_record(dump_record(metadata, record.body))
            candidate[record.id] = record
            items.append(review_item(proposal, current, original, commit, record.markdown))
        validate_records(candidate)
        return {"base_commit": commit, "items": items, "remote_url": self.config.remote_url,
                "branch": self.config.branch, "validation_errors": []}

    def preview(self, proposal_ids):
        with file_lock(self.lock_path):
            journal = self._journal()
            try:
                commit = self._fetch(journal)
                warnings = []
            except HubError:
                commit = journal.get("last_remote")
                if not commit:
                    raise
                warnings = ["无法核实最新远端；仅能预览，不能批准"]
            preview = self._prepare(proposal_ids, commit, share_confirm=lambda record: True)
            preview["warnings"] = warnings
            return {**preview, "delivery": export_bundle(self.config, preview)}

    def preview_delivery(self, delivery_id):
        manifest = load_bundle(self.config, delivery_id)
        return self.preview([item["proposal_id"] for item in manifest["preview"]["items"]])

    def approve_delivery(self, delivery_id, confirm, share_confirm=None, *, authorization=None):
        manifest = load_bundle(self.config, delivery_id)
        if authorization is not None:
            self._validate_conversation(authorization, manifest["review_digest"])
            if authorization["delivery_id"] != delivery_id:
                raise HubError("REVIEW_CHANGED", "授权不属于本交付")
        if manifest["preview"].get("validation_errors"):
            raise HubError("INVALID_REFERENCE", "交付存在校验阻塞，须修正或选择完整依赖后重新预览")
        proposal_ids = [item["proposal_id"] for item in manifest["preview"]["items"]]
        return self.approve(proposal_ids, confirm, share_confirm, expected_digest=manifest["review_digest"],
                            authorization=authorization)

    def approve(self, proposal_ids, confirm, share_confirm=None, *, expected_digest=None, authorization=None):
        if authorization is not None:
            self._validate_conversation(authorization, expected_digest)
        with file_lock(self.lock_path):
            journal = self._journal()
            existing = {journal["outcomes"].get(proposal_id, {}).get("batch_id") for proposal_id in proposal_ids}
            existing.discard(None)
            active_existing = {batch_id for batch_id in existing if journal["batches"][batch_id]["state"] != "cancelled"}
            if active_existing:
                if len(active_existing) == 1:
                    batch = journal["batches"][next(iter(active_existing))]
                    if set(proposal_ids) == set(batch["proposal_ids"]):
                        if expected_digest is not None and review_digest(batch["approvals"][-1]) != expected_digest:
                            raise HubError("REVIEW_CHANGED", "原批次批准内容与本次交付不同，不能将旧结果当作本次已入库")
                        self._project_status(journal)
                        return deepcopy(batch)
                raise HubError("INVALID_STATE", "所选提案已有批准，请先查看原批次")
            if any(batch["state"] not in TERMINAL for batch in journal["batches"].values()):
                raise HubError("INVALID_STATE", "有未结束批次，请先 retry 或 cancel")
            for proposal_id in proposal_ids:
                if journal["outcomes"].get(proposal_id, {}).get("state") == "rejected":
                    raise HubError("INVALID_STATE", "已拒绝提案不可批准")
            commit = self._fetch(journal)
            batch = {"batch_id": "batch-" + uuid.uuid4().hex, "proposal_ids": list(proposal_ids),
                     "state": "approved", "approvals": [], "push_attempted": False}
            self._confirm(batch, commit, confirm, share_confirm, expected_digest, authorization)
            journal["batches"][batch["batch_id"]] = batch
            for proposal_id in proposal_ids:
                journal["outcomes"][proposal_id] = {"batch_id": batch["batch_id"], "state": "approved"}
            self._save(journal)
            return self._advance(journal, batch)

    def _validate_conversation(self, authorization, expected_digest):
        fields = {"method", "delivery_id", "expected_review_digest", "confirmation", "allow_external_sharing"}
        if (self.config.security_mode != "single_user" or not isinstance(authorization, dict)
                or set(authorization) != fields or authorization["method"] != "conversation"
                or authorization["confirmation"] != CONVERSATION_CONFIRMATION
                or not isinstance(expected_digest, str) or authorization["expected_review_digest"] != expected_digest
                or type(authorization["allow_external_sharing"]) is not bool):
            raise HubError("UNAUTHORIZED", "对话授权模式、交付指纹或确认无效")

    def _confirm(self, batch, commit, confirm, share_confirm, expected_digest=None, authorization=None):
        if expected_digest is not None:
            before_permission = self._prepare(batch["proposal_ids"], commit, lambda record: True)
            if review_digest(before_permission) != expected_digest:
                raise HubError("REVIEW_CHANGED", "正文、元数据、依据、目标或远端基线已变化；请重新预览并审核")
        preview = self._prepare(batch["proposal_ids"], commit, share_confirm)
        if expected_digest is not None and review_digest(preview) != expected_digest:
            raise HubError("REVIEW_CHANGED", "外发许可改变了最终候选；请修改待审稿并重新预览，或按提案重新进行完整审核")
        approval_id = "approval-" + uuid.uuid4().hex
        phrase = "确认 " + batch["batch_id"] + " " + approval_id
        if authorization is not None:
            self._validate_conversation(authorization, expected_digest)
            phrase = authorization["confirmation"]
        elif confirm(deepcopy(preview), phrase) != phrase:
            raise HubError("UNAUTHORIZED", "确认短语不匹配；未生成批准")
        approval = {**preview, "approval_id": approval_id, "approved_at": utc_now(), "confirmation": phrase}
        if authorization is not None:
            approval["authorization"] = deepcopy(authorization)
        batch["approvals"].append(approval)
        batch.update(state="approved", push_attempted=False)
        batch.pop("candidate", None)

    def _build(self, batch):
        approval = batch["approvals"][-1]
        with tempfile.TemporaryDirectory(prefix="hub-index-", dir=self.private) as temporary:
            environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            self.repository.run("read-tree", approval["base_commit"], environment=environment)
            for item in approval["items"]:
                if item["old_path"] and item["old_path"] != item["relative_path"]:
                    removal = "0 " + "0" * len(approval["base_commit"]) + "\t" + item["old_path"] + "\n"
                    self.repository.run("update-index", "--index-info", input=removal.encode(), environment=environment)
            for item in approval["items"]:
                blob = self.repository.run("hash-object", "-w", "--stdin", input=item["proposed_markdown"].encode()).stdout.decode().strip()
                self.repository.run("update-index", "--add", "--cacheinfo", "100644," + blob + "," + item["relative_path"], environment=environment)
            tree = self.repository.run("write-tree", environment=environment).stdout.decode().strip()
            environment.update(GIT_AUTHOR_NAME="Knowledge Hub Review", GIT_COMMITTER_NAME="Knowledge Hub Review",
                               GIT_AUTHOR_EMAIL=self.config.device_id + "@local.invalid", GIT_COMMITTER_EMAIL=self.config.device_id + "@local.invalid",
                               GIT_AUTHOR_DATE=approval["approved_at"], GIT_COMMITTER_DATE=approval["approved_at"])
            message = f"docs: 更新已审核知识\n\nBatch-ID: {batch['batch_id']}\nApproval-ID: {approval['approval_id']}\n"
            commit = self.repository.run("commit-tree", tree, "-p", approval["base_commit"], input=message.encode(), environment=environment).stdout.decode().strip()
        self.repository.run("update-ref", "refs/hub/recovery/" + approval["approval_id"], commit)
        approval["candidate"] = commit
        batch.update(candidate=commit, state="committed")

    def _remote_batch(self, batch, remote):
        commits = self.repository.run("rev-list", remote).stdout.decode().splitlines()
        matches = []
        expected = {approval.get("candidate") for approval in batch["approvals"]}
        for commit in commits:
            message = self.repository.run("show", "-s", "--format=%B", commit).stdout.decode("utf-8", errors="replace")
            if "Batch-ID: " + batch["batch_id"] in message.splitlines():
                if commit not in expected:
                    raise HubError("INVALID_STATE", "远端批次标识与批准内容不一致，停止写入")
                matches.append(commit)
        if len(matches) > 1:
            raise HubError("INVALID_STATE", "同一批次在远端多次生效，停止写入")
        return matches[0] if matches else None

    def _advance(self, journal, batch):
        batch.pop("sync_error", None)
        try:
            remote = self._fetch(journal)
        except HubError as error:
            if error.code != "SYNC_FAILED":
                raise
            batch["state"] = "pending_sync"
            batch["sync_error"] = {**error.as_dict()["error"], "stage": "fetch"}
            self._save(journal)
            return deepcopy(batch)
        found = self._remote_batch(batch, remote)
        if found:
            batch.update(state="synced", candidate=found)
            self._save(journal)
            return deepcopy(batch)
        approval = batch["approvals"][-1]
        if remote != approval["base_commit"]:
            batch["state"] = "needs_review"
            self._save(journal)
            return deepcopy(batch)
        if not batch.get("candidate"):
            self._build(batch)
            self._save(journal)
        batch["push_attempted"] = True
        batch["state"] = "pending_sync"
        self._save(journal)
        try:
            self.repository.run("push", "--porcelain", "--", self.config.remote_url,
                                batch["candidate"] + ":refs/heads/" + self.config.branch)
        except HubError as error:
            batch["sync_error"] = {**error.as_dict()["error"], "stage": "push"}
            self._save(journal)
            return deepcopy(batch)
        batch["state"] = "synced"
        self._save(journal)
        return deepcopy(batch)

    def retry(self, batch_id, confirm=None, share_confirm=None, *, conversation_only=False):
        with file_lock(self.lock_path):
            journal = self._journal()
            batch = self._batch(journal, batch_id)
            if conversation_only and (self.config.security_mode != "single_user"
                                      or batch["approvals"][-1].get("authorization", {}).get("method") != "conversation"):
                raise HubError("UNAUTHORIZED", "此批次不属于对话授权，请使用原人工审核入口恢复")
            if batch["state"] == "cancelled":
                raise HubError("INVALID_STATE", "此批次已取消，原批准不能重试")
            if batch["state"] == "synced":
                self._remote_batch(batch, self._fetch(journal))
                return deepcopy(batch)
            result = self._advance(journal, batch)
            if result["state"] == "needs_review" and confirm is not None:
                self._confirm(batch, journal["last_remote"], confirm, share_confirm)
                self._save(journal)
                return self._advance(journal, batch)
            return result

    @staticmethod
    def _batch(journal, batch_id):
        batch = journal["batches"].get(batch_id)
        if batch is None:
            raise HubError("NOT_FOUND", "审核批次不存在")
        return batch

    def cancel(self, batch_id, confirm):
        with file_lock(self.lock_path):
            journal = self._journal()
            batch = self._batch(journal, batch_id)
            if batch["state"] in TERMINAL:
                raise HubError("INVALID_STATE", "终态批次不能取消")
            if batch["push_attempted"]:
                if batch["state"] != "needs_review":
                    raise HubError("INVALID_STATE", "推送结果未知；请先 retry 查明结果")
                remote = self._fetch(journal)
                if self._remote_batch(batch, remote) or remote == batch["approvals"][-1]["base_commit"]:
                    raise HubError("INVALID_STATE", "批次已生效或仍可能生效，不能取消")
            phrase = "取消 " + batch_id
            if confirm(deepcopy(batch), phrase) != phrase:
                raise HubError("UNAUTHORIZED", "未确认取消")
            batch["state"] = "cancelled"
            self._save(journal)
            return deepcopy(batch)

    def reject(self, proposal_id, confirm):
        with file_lock(self.lock_path):
            journal = self._journal()
            outcome = journal["outcomes"].get(proposal_id, {})
            if outcome.get("batch_id") and journal["batches"][outcome["batch_id"]]["state"] != "cancelled":
                raise HubError("INVALID_STATE", "已批准提案不能通过 reject 处理")
            self._import(proposal_id)
            paths = [proposal_path(self.config.proposal_dir, proposal_id), proposal_path(self.private / "imports", proposal_id)]
            phrase = "拒绝 " + proposal_id
            if confirm(paths, phrase) != phrase:
                raise HubError("UNAUTHORIZED", "未确认拒绝")
            journal["outcomes"][proposal_id] = {"state": "rejected", "rejected_at": utc_now()}
            self._save(journal)
            for path in paths:
                checked_path(path).unlink(missing_ok=True)
            return {"proposal_id": proposal_id, "state": "rejected"}

    def edit(self, proposal_id, markdown):
        with file_lock(self.lock_path):
            journal = self._journal()
            outcome = journal["outcomes"].get(proposal_id, {})
            if outcome.get("batch_id") and journal["batches"][outcome["batch_id"]]["state"] != "cancelled":
                raise HubError("INVALID_STATE", "批次内提案不能直接编辑；需要取消后重新审核")
            proposal = self._import(proposal_id)
            record = parse_record(markdown)
            if record.id != proposal["target_id"]:
                raise HubError("INVALID_RECORD", "编辑不能改变目标 ID")
            proposal["proposed_markdown"] = record.markdown
            atomic_json(proposal_path(self.private / "imports", proposal_id), proposal)
            return {"proposal_id": proposal_id, "state": "pending", "message": "修改后必须重新预览确认"}

    def draft(self, proposal_id):
        with file_lock(self.lock_path):
            return deepcopy(self._import(proposal_id))

    def status(self):
        with file_lock(self.lock_path):
            journal = self._journal()
            self._project_status(journal)
            return [{key: batch.get(key) for key in ("batch_id", "state", "proposal_ids", "candidate", "sync_error")} for batch in journal["batches"].values()]

    def list(self):
        with file_lock(self.lock_path):
            journal = self._journal()
            self._project_status(journal)
            results = []
            for path in sorted(self.config.proposal_dir.glob("*.json")):
                try:
                    proposal = read_proposal(path)
                except HubError:
                    results.append({"source_file": path.name, "state": "invalid", "message": "无效提案未导入，请人工检查"})
                    continue
                proposal_id = proposal.get("proposal_id")
                proposal_path(self.config.proposal_dir, proposal_id)
                outcome = dict(journal["outcomes"].get(proposal_id, {"state": "pending"}))
                if outcome.get("batch_id"):
                    batch = journal["batches"][outcome["batch_id"]]
                    outcome["state"] = "pending" if batch["state"] == "cancelled" else batch["state"]
                results.append({"proposal_id": proposal_id, "target_id": proposal.get("target_id"), **outcome})
            return results

    def history(self, record_id):
        with file_lock(self.lock_path):
            journal = self._journal()
            remote = self._fetch(journal)
            records = self.repository.records(remote)
            if record_id not in records:
                raise HubError("NOT_FOUND", "记录不存在")
            record = records[record_id]
            paths = {record.relative_path}
            for status in ("active", "archived", "superseded"):
                metadata = {**record.metadata, "status": status}
                paths.add(parse_record(dump_record(metadata, record.body)).relative_path)
            return self.repository.run("log", "--format=%H %aI %s", remote, "--", *sorted(paths)).stdout.decode("utf-8", errors="replace")
