import re

from mcp_server.config import load_config
from mcp_server.delivery import load_bundle
from mcp_server.models import HubError
from mcp_server.security import validate_runtime
from review.batch import CONVERSATION_CONFIRMATION, ReviewService


class ConversationReview:
    def __init__(self, config):
        if (config.role != "reader" or config.allow_conversation_approval is not True
                or config.security_mode != "single_user" or config.transport != "stdio"
                or config.reviewer_config is None):
            raise HubError("UNAUTHORIZED", "对话批准能力未在本机 stdio 配置中显式启用")
        validate_runtime(config)
        self.config = config
        self.service = ReviewService(self._reviewer_config())

    def _reviewer_config(self):
        reviewer = load_config(self.config.reviewer_config, "reviewer")
        if reviewer.security_mode != "single_user" or any(
                getattr(reviewer, field) != getattr(self.config, field)
                for field in ("device_id", "remote_url", "branch", "proposal_dir", "review_status_dir")):
            raise HubError("INVALID_CONFIG", "对话批准的读取／审核配置必须使用相同设备、固定仓库、分支和共享目录")
        if (reviewer.data_root == self.config.data_root or reviewer.data_root in self.config.data_root.parents
                or self.config.data_root in reviewer.data_root.parents):
            raise HubError("INVALID_CONFIG", "读取与审核私有数据目录必须相互独立")
        validate_runtime(reviewer)
        return reviewer

    def _check_config(self):
        validate_runtime(self.config)
        if self._reviewer_config() != self.service.config:
            raise HubError("REVIEW_CHANGED", "审核配置已变化；须重新启动并核对交付目标")

    @staticmethod
    def _result(batch):
        return {"ok": True, "data": {key: batch.get(key) for key in ("batch_id", "state", "candidate", "sync_error")},
                "warnings": ["仅 state=synced 表示远端已接收；仍须刷新读取侧验证。"]}

    def approve(self, delivery_id, expected_review_digest, confirmation, allow_external_sharing=False):
        self._check_config()
        if confirmation != CONVERSATION_CONFIRMATION or type(allow_external_sharing) is not bool:
            raise HubError("UNAUTHORIZED", "需要确切的入库推送确认，外发许可必须单独显式确认")
        if not isinstance(expected_review_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_review_digest):
            raise HubError("INVALID_ARGUMENT", "须提供人读交付对应的完整内容指纹")
        manifest = load_bundle(self.service.config, delivery_id)
        if manifest["review_digest"] != expected_review_digest:
            raise HubError("REVIEW_CHANGED", "确认的内容指纹与交付不一致，须重新审核")
        authorization = {"method": "conversation", "delivery_id": delivery_id,
                         "expected_review_digest": expected_review_digest, "confirmation": confirmation,
                         "allow_external_sharing": allow_external_sharing}
        batch = self.service.approve_delivery(delivery_id, None, lambda record: allow_external_sharing,
                                              authorization=authorization)
        return self._result(batch)

    def status(self):
        self._check_config()
        return {"ok": True, "data": self.service.status(), "warnings": []}

    def retry(self, batch_id):
        self._check_config()
        if not isinstance(batch_id, str) or not re.fullmatch(r"batch-[0-9a-f]{32}", batch_id):
            raise HubError("INVALID_ARGUMENT", "审核批次标识无效")
        return self._result(self.service.retry(batch_id, conversation_only=True))
