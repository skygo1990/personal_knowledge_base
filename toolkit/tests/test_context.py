import importlib
from datetime import datetime
from pathlib import Path

import pytest

from mcp_server.records import parse_record
from review.sync import Snapshot
from tests.helpers import PROJECT_BODY, make_markdown


COMMIT = "a" * 40


def record(record_id, record_type="core", body=None, **metadata):
    return parse_record(make_markdown(record_id, record_type, body=body, **metadata))


def service(records=(), *, commit=COMMIT, now=None, warnings=None, sync_status="fresh"):
    module = importlib.import_module("mcp_server.context")
    snapshot = Snapshot(commit, {item.id: item for item in records}, Path("/unused"),
                        "2026-11-20T01:00:00Z", sync_status, warnings or [])
    return module.ContextService(snapshot, now=now)


def ids(items):
    return [item["id"] for item in items]


def test_empty_context_has_snapshot_and_empty_results():
    response = service().get_core_context()
    assert response == {
        "ok": True,
        "data": {"view": "base", "base": [], "extra": [], "projects": [],
                 "total": 0, "next_cursor": None,
                 "budget": {"base": {"count": 0, "limit": 400},
                            "extra": {"count": 0, "limit": 0}}},
        "warnings": [], "snapshot_commit": COMMIT,
        "last_synced_at": "2026-11-20T01:00:00Z", "sync_status": "fresh",
    }


@pytest.mark.parametrize("project_id,expected", [
    (None, ["core-global"]),
    ("proj-alpha", ["core-alpha", "core-global"]),
    ("proj-beta", ["core-beta", "core-global"]),
])
def test_core_scope_truth_table(project_id, expected):
    reader = service([
        record("proj-alpha", "project"), record("proj-beta", "project"),
        record("core-global"), record("core-alpha", scope=["proj-alpha"]),
        record("core-beta", scope=["proj-beta"]),
    ])
    response = reader.get_core_context(project_id=project_id)
    assert response["ok"]
    assert ids(response["data"]["base"]) == expected


def test_views_deduplicate_base_and_preserve_full_bodies():
    shared = record("core-shared", body="基 本\n", views=["base", "coding", "thinking"])
    coding = record("core-coding", body="**代码**\t", views=["coding"])
    thinking = record("core-thinking", views=["thinking"])
    reader = service([shared, coding, thinking])
    response = reader.get_core_context(view="coding")
    assert ids(response["data"]["base"]) == ["core-shared"]
    assert ids(response["data"]["extra"]) == ["core-coding"]
    assert response["data"]["base"][0]["body"] == shared.body
    assert response["data"]["budget"] == {
        "base": {"count": 2, "limit": 400}, "extra": {"count": 6, "limit": 300},
    }
    assert ids(reader.get_core_context(view="thinking")["data"]["extra"]) == ["core-thinking"]
    assert reader.get_core_context()["data"]["extra"] == []


@pytest.mark.parametrize("view,body,bucket", [("base", "字" * 401, "base"), ("coding", "字" * 301, "extra")])
def test_budget_overflow_returns_full_records_and_counts(view, body, bucket):
    original = record("core-large", views=[view], body=body)
    response = service([original]).get_core_context(view=view)
    assert not response["ok"]
    assert response["error"]["code"] == "BUDGET_EXCEEDED"
    assert response["data"][bucket][0]["body"] == body
    assert response["data"]["budget"][bucket]["count"] == len(body)
    assert response["snapshot_commit"] == COMMIT


def test_budget_applies_only_to_current_project_combination():
    reader = service([
        record("proj-alpha", "project"), record("proj-beta", "project"),
        record("core-global", body="字" * 200),
        record("core-alpha", body="字" * 200, scope=["proj-alpha"]),
        record("core-beta", body="字" * 200, scope=["proj-beta"]),
    ])
    for project_id in (None, "proj-alpha", "proj-beta"):
        assert reader.get_core_context(project_id=project_id)["ok"]


@pytest.mark.parametrize("status", ["archived", "superseded"])
def test_exact_core_reads_history_without_aggregate_budget_or_view_filter(status):
    original = record("core-old", body="旧" * 500, status=status, views=["thinking"], scope=["proj-alpha"])
    reader = service([original, record("proj-alpha", "project")])
    assert reader.get_core_context()["data"]["base"] == []
    response = reader.get_core_context(record_id="core-old")
    assert response["ok"]
    assert response["data"]["record"]["body"] == original.body
    assert response["data"]["record"]["status"] == status
    assert response["warnings"]


def test_exact_core_honors_explicit_project_filter():
    reader = service([record("proj-alpha", "project"), record("proj-beta", "project"),
                      record("core-alpha", scope=["proj-alpha"])])
    response = reader.get_core_context(record_id="core-alpha", project_id="proj-beta")
    assert response["error"]["code"] == "NOT_FOUND"
    assert "范围" in response["error"]["message"]


def test_project_full_body_sources_links_and_history():
    body = PROJECT_BODY + "完整材料" * 1000
    original = record("proj-alpha", "project", body=body, status="archived", related_ids=["core-abc"])
    reader = service([original, record("core-abc")], now=datetime.fromisoformat("2026-11-20T00:00:00+00:00"))
    response = reader.get_project_context("proj-alpha")
    assert response["ok"]
    assert response["data"]["record"]["body"] == body
    assert response["data"]["record"]["status"] == "archived"
    assert response["data"]["related_ids"] == ["core-abc"]
    assert response["data"]["record"]["source"] == {
        "id": original.id, "relative_path": original.relative_path,
        "updated_at": original.metadata["updated_at"], "snapshot_commit": COMMIT,
    }
    assert response["warnings"]


def constraints():
    return [
        {"id": "weekly-time", "key": "weekly_minutes", "kind": "baseline", "value": 240},
        {"id": "busy-week", "key": "weekly_minutes", "kind": "exception", "value": 60,
         "overrides": "weekly-time", "valid_from": "2026-11-20T00:00:00+08:00",
         "valid_until": "2026-11-27T00:00:00+08:00"},
    ]


@pytest.mark.parametrize("moment,expected_id,expected_value", [
    ("2026-11-19T15:59:59Z", "weekly-time", 240),
    ("2026-11-19T16:00:00Z", "busy-week", 60),
    ("2026-11-26T15:59:59Z", "busy-week", 60),
    ("2026-11-26T16:00:00Z", "weekly-time", 240),
])
def test_constraints_use_inclusive_start_exclusive_end_and_current_baseline(moment, expected_id, expected_value):
    reader = service([record("proj-alpha", "project", constraints=constraints())],
                     now=lambda: datetime.fromisoformat(moment))
    response = reader.get_project_context("proj-alpha")
    assert response["ok"]
    current = response["data"]["effective_constraints"]
    assert [(item["id"], item["value"]) for item in current] == [(expected_id, expected_value)]


def test_conflicting_constraints_are_not_silently_chosen():
    original = record("proj-alpha", "project", constraints=constraints())
    original.metadata["constraints"].append({"id": "other-base", "key": "weekly_minutes", "kind": "baseline", "value": 120})
    response = service([original]).get_project_context("proj-alpha")
    assert response["error"]["code"] == "CONSTRAINT_CONFLICT"


def test_naive_clock_is_rejected_without_raw_exception():
    response = service([record("proj-alpha", "project")], now=datetime(2026, 11, 20)).get_project_context("proj-alpha")
    assert response["error"]["code"] == "INVALID_ARGUMENT"


def test_active_project_table_paginates_without_project_body():
    reader = service([record("proj-gamma", "project", status="archived"),
                      record("proj-beta", "project"), record("proj-alpha", "project")])
    first = reader.get_core_context(limit=1)
    assert ids(first["data"]["projects"]) == ["proj-alpha"]
    assert first["data"]["total"] == 2
    assert "body" not in first["data"]["projects"][0]
    assert first["data"]["projects"][0]["summary"] == "测试项目目标"
    second = reader.get_core_context(limit=1, cursor=first["data"]["next_cursor"])
    assert ids(second["data"]["projects"]) == ["proj-beta"]
    assert second["data"]["next_cursor"] is None


def test_context_cursor_cannot_switch_view_or_snapshot():
    records = [record("proj-alpha", "project"), record("proj-beta", "project")]
    cursor = service(records).get_core_context(limit=1)["data"]["next_cursor"]
    for reader, parameters in [(service(records), {"view": "coding"}),
                               (service(records, commit="b" * 40), {})]:
        response = reader.get_core_context(limit=1, cursor=cursor, **parameters)
        assert response["error"]["code"] == "INVALID_CURSOR"


@pytest.mark.parametrize("parameters", [{"view": "unknown"}, {"view": []}, {"limit": 0}, {"limit": 101},
                                        {"limit": True}, {"limit": 1.5}, {"record_id": []}, {"project_id": []}])
def test_bad_core_parameters_have_structured_errors(parameters):
    response = service().get_core_context(**parameters)
    assert response["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("method,parameters", [
    ("get_core_context", {"record_id": "core-absent"}),
    ("get_core_context", {"project_id": "proj-absent"}),
    ("get_project_context", {"project_id": "proj-absent"}),
    ("get_project_context", {"project_id": "core-abc"}),
])
def test_missing_or_wrong_type_records_are_not_empty_success(method, parameters):
    response = getattr(service([record("core-abc")]), method)(**parameters)
    assert response["error"]["code"] == "NOT_FOUND"


def test_stale_snapshot_warnings_survive_and_results_do_not_mutate_snapshot():
    original = record("core-abc")
    reader = service([original], warnings=["网络不可用，保留本地快照"], sync_status="stale")
    response = reader.get_core_context()
    assert response["warnings"] == ["网络不可用，保留本地快照"]
    assert response["sync_status"] == "stale"
    response["data"]["base"][0]["metadata"]["views"].append("coding")
    response["warnings"].append("caller edit")
    assert original.metadata["views"] == ["base"]
    assert reader.get_core_context()["warnings"] == ["网络不可用，保留本地快照"]


@pytest.mark.parametrize("method,parameters", [
    ("get_core_context", {}), ("get_project_context", {"project_id": "proj-alpha"}),
    ("search_lessons", {}), ("find_deliverables", {}),
])
def test_no_snapshot_never_becomes_fake_empty_knowledge(method, parameters):
    module = importlib.import_module("mcp_server.context")
    response = getattr(module.ContextService(None), method)(**parameters)
    assert response["error"]["code"] == "SNAPSHOT_UNAVAILABLE"
    assert response["sync_status"] == "unavailable"
