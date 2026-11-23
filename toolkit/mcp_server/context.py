from copy import deepcopy
from datetime import datetime, timezone

from mcp_server.models import HubError
from mcp_server.records import _validate_constraints, body_count
from mcp_server.search import SearchService, _optional_id, applies_to, read_operation, record_order


class ContextService(SearchService):
    def __init__(self, snapshot, now=None):
        super().__init__(snapshot)
        self.now = now

    @read_operation
    def get_core_context(self, view="base", project_id=None, record_id=None, limit=10, cursor=None):
        if not isinstance(view, str) or view not in {"base", "coding", "thinking"}:
            raise HubError("INVALID_ARGUMENT", "view 必须为 base、coding 或 thinking")
        _optional_id(record_id, "record_id")
        self._validate_project(project_id)
        offset, query_hash = self._pagination(cursor, limit, {
            "operation": "get_core_context", "view": view, "project_id": project_id, "record_id": record_id,
        })
        if record_id is not None:
            original = self._record(record_id, "core")
            if project_id is not None and not applies_to(original, project_id):
                raise HubError("NOT_FOUND", "记录适用范围不包含当前项目")
            if cursor is not None:
                raise HubError("INVALID_CURSOR", "精确核心记录读取没有下一页")
            return self._response({"record": self._item(original, full=True)}, self._history_warnings([original]))
        applicable = sorted((record for record in self.records.values()
                             if record.type == "core" and record.status == "active" and applies_to(record, project_id)),
                            key=record_order)
        base = [record for record in applicable if "base" in record.metadata["views"]]
        extra = [record for record in applicable
                 if view != "base" and view in record.metadata["views"] and "base" not in record.metadata["views"]]
        projects = sorted((record for record in self.records.values()
                           if record.type == "project" and record.status == "active"), key=record_order)
        selected, next_cursor = self._page(projects, offset, limit, query_hash)
        summaries = [{**self._item(record), "summary": record.metadata["summary"]} for record in selected]
        budget = {"base": {"count": sum(body_count(record.body) for record in base), "limit": 400},
                  "extra": {"count": sum(body_count(record.body) for record in extra), "limit": 300 if view != "base" else 0}}
        data = {"view": view, "base": [self._item(record, full=True) for record in base],
                "extra": [self._item(record, full=True) for record in extra], "projects": summaries,
                "total": len(projects), "next_cursor": next_cursor, "budget": budget}
        if any(part["count"] > part["limit"] for part in budget.values()):
            return self._response(data, error=HubError("BUDGET_EXCEEDED", "核心上下文超过预算；已返回相关全文与计数，不进行截断"))
        return self._response(data)

    @read_operation
    def get_project_context(self, project_id):
        if project_id is None:
            raise HubError("INVALID_ARGUMENT", "必须指定 project_id")
        self._validate_project(project_id)
        original = self._record(project_id, "project")
        constraints = original.metadata.get("constraints", [])
        _validate_constraints(constraints)
        moment = self.now() if callable(self.now) else self.now
        if moment is None:
            moment = datetime.now(timezone.utc)
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise HubError("INVALID_ARGUMENT", "当前时刻必须为带时区的 datetime")
        effective = {constraint["key"]: constraint for constraint in constraints if constraint["kind"] == "baseline"}
        for constraint in constraints:
            if constraint["kind"] == "exception":
                start = datetime.fromisoformat(constraint["valid_from"])
                end = datetime.fromisoformat(constraint["valid_until"])
                if start <= moment < end:
                    effective[constraint["key"]] = constraint
        data = {"record": self._item(original, full=True),
                "effective_constraints": [deepcopy(effective[key]) for key in sorted(effective)],
                "related_ids": list(original.metadata.get("related_ids", [])), "evaluated_at": moment.isoformat()}
        return self._response(data, self._history_warnings([original]))
