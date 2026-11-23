import base64
import binascii
import hashlib
import json
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps

from mcp_server.models import HubError
from mcp_server.records import CONTROL_PATTERN, ID_PATTERN


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold().strip()


def query_terms(query):
    phrase = normalize(query)
    return list(dict.fromkeys([phrase, *phrase.split()])) if phrase else []


def read_operation(operation):
    @wraps(operation)
    def wrapped(self, *args, **kwargs):
        try:
            if self.snapshot is None or not self.snapshot.commit:
                raise HubError("SNAPSHOT_UNAVAILABLE", "没有可用的读取快照", retryable=True)
            return operation(self, *args, **kwargs)
        except HubError as error:
            return self._response(error=error)
    return wrapped


def _text(value, field, *, empty=False):
    if (not isinstance(value, str) or (not empty and not value.strip())
            or CONTROL_PATTERN.search(value)
            or any(0xD800 <= ord(character) <= 0xDFFF for character in value)):
        raise HubError("INVALID_ARGUMENT", f"{field} 必须为有效字符串")
    return value


def _optional_id(value, field):
    if value is not None and (not isinstance(value, str) or not ID_PATTERN.fullmatch(value)):
        raise HubError("INVALID_ARGUMENT", f"{field} 必须为合法记录 ID")


def record_order(record):
    moment = datetime.fromisoformat(record.metadata["updated_at"])
    elapsed = moment - datetime(1970, 1, 1, tzinfo=timezone.utc)
    microseconds = (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
    return -microseconds, record.id


def applies_to(record, project_id):
    return "global" in record.metadata["scope"] or project_id in record.metadata["scope"]


def keyword_score(record, query, records):
    phrase = normalize(query)
    if phrase == normalize(record.id):
        return 100
    terms = query_terms(query)
    if not terms:
        return 0
    titles = [normalize(record.metadata["title"]), *map(normalize, record.metadata.get("aliases", []))]
    project_ids = set(record.metadata["scope"] + record.metadata.get("related_ids", []))
    if record.metadata.get("project_id"):
        project_ids.add(record.metadata["project_id"])
    labels = [normalize(tag) for tag in record.metadata.get("tags", [])]
    labels.extend(normalize(records[project_id].metadata["title"]) for project_id in project_ids
                  if project_id in records and records[project_id].type == "project")
    body = normalize(record.body)
    score = 40 if any(phrase in title for title in titles) else 0
    for term in terms:
        score += 20 if any(term in title for title in titles) else 0
        score += 10 if any(term in label for label in labels) else 0
        score += 1 if term in body else 0
    return score


def excerpt(body, query, limit=240):
    terms = query_terms(query)
    normalized = unicodedata.normalize("NFKC", body).casefold()
    positions = [normalized.find(term) for term in terms if term in normalized]
    if not positions:
        return body[:limit]
    position = min(positions)
    lower, upper = 0, len(body)
    while lower < upper:
        middle = (lower + upper) // 2
        prefix_length = len(unicodedata.normalize("NFKC", body[:middle]).casefold())
        if prefix_length <= position:
            lower = middle + 1
        else:
            upper = middle
    start = max(0, lower - 1 - 60)
    return body[start:start + limit]


class SearchService:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.records = deepcopy(snapshot.records) if snapshot is not None else {}

    def _response(self, data=None, warnings=None, error=None):
        response = {
            "ok": error is None,
            "warnings": list(getattr(self.snapshot, "warnings", [])) + (warnings or []),
            "snapshot_commit": getattr(self.snapshot, "commit", None),
            "last_synced_at": getattr(self.snapshot, "last_synced_at", None),
            "sync_status": getattr(self.snapshot, "sync_status", "unavailable"),
        }
        if data is not None:
            response["data"] = data
        if error is not None:
            response["error"] = error.as_dict()["error"]
        return response

    def _record(self, record_id, record_type):
        record = self.records.get(record_id)
        if record is None or record.type != record_type:
            raise HubError("NOT_FOUND", "指定记录不存在或记录类型不匹配")
        return record

    def _validate_project(self, project_id):
        _optional_id(project_id, "project_id")
        if project_id is not None:
            self._record(project_id, "project")

    def _item(self, record, *, full=False):
        item = {field: deepcopy(record.metadata[field]) for field in ("id", "title", "type", "status", "scope")}
        item.update({"project_id": record.metadata.get("project_id"),
                     "tags": list(record.metadata.get("tags", [])),
                     "source": {"id": record.id, "relative_path": record.relative_path,
                                "updated_at": record.metadata["updated_at"], "snapshot_commit": self.snapshot.commit}})
        if "kind" in record.metadata:
            item["kind"] = record.metadata["kind"]
        if full:
            item.update(body=record.body, metadata=deepcopy(record.metadata))
        return item

    def _history_warnings(self, records):
        return [f"记录 {record.id} 的状态为 {record.status}，仅作为历史资料读取"
                for record in records if record.status != "active"]

    def _pagination(self, cursor, limit, parameters):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError("INVALID_ARGUMENT", "limit 必须为 1～100 的整数")
        query_hash = hashlib.sha256(json.dumps({**parameters, "limit": limit}, sort_keys=True,
                                              ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        if cursor is None:
            return 0, query_hash
        try:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
                raise ValueError
            payload = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
            if (not isinstance(payload, dict) or set(payload) != {"commit", "query", "offset"}
                    or payload["commit"] != self.snapshot.commit or payload["query"] != query_hash
                    or type(payload["offset"]) is not int or payload["offset"] <= 0):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
            raise HubError("INVALID_CURSOR", "分页游标无效，或不属于当前快照与查询") from None
        return payload["offset"], query_hash

    def _page(self, records, offset, limit, query_hash):
        if offset and offset >= len(records):
            raise HubError("INVALID_CURSOR", "分页游标超出当前结果范围")
        next_offset = offset + limit
        cursor = None
        if next_offset < len(records):
            payload = {"commit": self.snapshot.commit, "query": query_hash, "offset": next_offset}
            cursor = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode()
        return records[offset:next_offset], cursor

    def _search(self, *, record_type, query, record_id, project_id, tags, audience,
                include_unavailable, limit, cursor):
        _text(query, "query", empty=True)
        _optional_id(record_id, "record_id")
        self._validate_project(project_id)
        if type(include_unavailable) is not bool:
            raise HubError("INVALID_ARGUMENT", "历史或不可外发筛选参数必须为布尔值")
        if tags is not None and not isinstance(tags, list):
            raise HubError("INVALID_ARGUMENT", "tags 必须为字符串列表")
        required_tags = sorted({normalize(_text(tag, "tags")) for tag in tags or []})
        if audience is not None:
            _text(audience, "audience")
        offset, query_hash = self._pagination(cursor, limit, {
            "record_type": record_type, "query": normalize(query), "record_id": record_id,
            "project_id": project_id, "tags": required_tags, "audience": audience,
            "include_unavailable": include_unavailable,
        })
        candidates = [self._record(record_id, record_type)] if record_id else self.records.values()
        scored = []
        for record in candidates:
            if record.type != record_type:
                continue
            reason = None
            if project_id is not None and not applies_to(record, project_id):
                reason = "记录适用范围不包含当前项目"
            elif not set(required_tags) <= {normalize(tag) for tag in record.metadata.get("tags", [])}:
                reason = "记录不符合明确指定的标签筛选"
            elif audience is not None and audience not in record.metadata.get("audience", []):
                reason = "记录未列出明确指定的受众"
            if reason:
                if record_id:
                    raise HubError("NOT_FOUND", reason)
                continue
            if not record_id and not include_unavailable:
                if record.status != "active":
                    continue
                if record_type == "deliverable" and record.metadata["share_status"] != "ready":
                    continue
            score = 100 if record_id else keyword_score(record, query, self.records)
            if record_id or not normalize(query) or score:
                scored.append((record, score))
        scored.sort(key=lambda item: (-item[1], *record_order(item[0])))
        selected, next_cursor = self._page(scored, offset, limit, query_hash)
        items = []
        warnings = self._history_warnings([record for record, score in selected])
        for record, score in selected:
            item = self._item(record)
            item.update(score=score, excerpt=excerpt(record.body, query))
            if record_type == "deliverable":
                can_share = record.status == "active" and record.metadata["share_status"] == "ready"
                item["permission"] = {"can_share": can_share, "share_status": record.metadata["share_status"],
                                      "audience": list(record.metadata["audience"])}
                events = deepcopy(record.metadata.get("publication_events", []))
                item["publication"] = {
                    "events": events, "current_body_published": None,
                    "note": "已有历史发布记录，不能据此确认当前正文已经发布" if events else "库内无发布记录",
                }
                if not can_share:
                    warnings.append(f"记录 {record.id} 当前不可外发；历史发布记录不代表已撤下外部内容")
                if record_id:
                    item.update(body=record.body, internal_metadata=deepcopy(record.metadata))
            items.append(item)
        return self._response({"items": items, "total": len(scored), "next_cursor": next_cursor}, warnings)

    @read_operation
    def search_lessons(self, query="", record_id=None, tags=None, project_id=None,
                       include_history=False, limit=10, cursor=None):
        return self._search(record_type="lesson", query=query, record_id=record_id, tags=tags,
                            project_id=project_id, audience=None, include_unavailable=include_history,
                            limit=limit, cursor=cursor)

    @read_operation
    def find_deliverables(self, query="", record_id=None, project_id=None, audience=None,
                          include_unavailable=False, limit=10, cursor=None):
        return self._search(record_type="deliverable", query=query, record_id=record_id, tags=None,
                            project_id=project_id, audience=audience, include_unavailable=include_unavailable,
                            limit=limit, cursor=cursor)
