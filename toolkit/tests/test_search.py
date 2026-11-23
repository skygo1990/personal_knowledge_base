import base64
import json

import pytest

from tests.helpers import LESSON_HEADINGS
from tests.test_context import COMMIT, ids, record, service


def lesson(record_id="lesson-abc", text="", **metadata):
    body = "\n".join(f"## {heading}\n内容\n" for heading in LESSON_HEADINGS["decision"])
    return record(record_id, "lesson", body=body + text, **metadata)


@pytest.mark.parametrize("query,metadata,text,score", [
    ("科学实验", {"title": "科学实验"}, "", 60),
    ("科学实验", {"aliases": ["科学实验", "科学实验说明"]}, "", 60),
    ("科学实验", {"tags": ["科学实验"]}, "", 10),
    ("科学实验", {}, "科学实验" * 100, 1),
    ("科学实验", {"title": "科学实验", "aliases": ["科学实验"], "tags": ["科学实验"]}, "科学实验", 71),
    ("alpha beta", {"title": "alpha beta"}, "", 100),
    ("alpha beta", {}, "alpha beta", 3),
    ("alpha beta", {"title": "alpha"}, "", 20),
    (" alpha alpha ", {"title": "alpha"}, "", 20),
    ("ＣＡＦÉ", {"title": "cafe\u0301"}, "", 60),
    (" STRASSE ", {"aliases": ["Straße"]}, "", 60),
    (" ＬＥＳＳＯＮ－ＡＢＣ ", {"title": "lesson-abc", "tags": ["lesson-abc"]}, "lesson-abc", 100),
])
def test_fixed_scores_and_unicode_normalization(query, metadata, text, score):
    response = service([lesson(text=text, **metadata)]).search_lessons(query=query)
    assert response["ok"]
    assert [(item["id"], item["score"]) for item in response["data"]["items"]] == [("lesson-abc", score)]


@pytest.mark.parametrize("association", ["project_id", "scope", "related_ids"])
def test_associated_project_title_matches_without_becoming_scope(association):
    metadata = {association: "proj-alpha" if association == "project_id" else ["proj-alpha"]}
    reader = service([record("proj-alpha", "project", title="家庭科学"), lesson(**metadata)])
    response = reader.search_lessons(query="家庭科学")
    assert response["data"]["items"][0]["score"] == 10


def test_scoring_class_counts_each_term_once_for_repeated_tags_and_project_titles():
    reader = service([record("proj-alpha", "project", title="科学"),
                      lesson(tags=["科学", "科学方法"], project_id="proj-alpha")])
    assert reader.search_lessons("科学")["data"]["items"][0]["score"] == 10


@pytest.mark.parametrize("method,kind", [("search_lessons", "lesson"), ("find_deliverables", "deliverable")])
@pytest.mark.parametrize("project_id,expected", [
    (None, ["item-alpha", "item-beta", "item-global"]),
    ("proj-alpha", ["item-alpha", "item-global"]),
    ("proj-beta", ["item-beta", "item-global"]),
])
def test_search_scope_truth_table_is_independent_of_source(method, kind, project_id, expected):
    reader = service([
        record("proj-alpha", "project"), record("proj-beta", "project"),
        record("item-global", kind, scope=["global"], project_id="proj-alpha"),
        record("item-alpha", kind, scope=["proj-alpha"], project_id="proj-beta"),
        record("item-beta", kind, scope=["proj-beta"], project_id="proj-alpha"),
    ])
    response = getattr(reader, method)(project_id=project_id)
    assert ids(response["data"]["items"]) == expected
    if project_id is None:
        assert response["data"]["items"][0]["scope"] == ["proj-alpha"]
        assert response["data"]["items"][0]["project_id"] == "proj-beta"


@pytest.mark.parametrize("method,kind", [("search_lessons", "lesson"), ("find_deliverables", "deliverable")])
def test_exact_id_honors_scope_but_not_source(method, kind):
    reader = service([record("proj-alpha", "project"), record("proj-beta", "project"),
                      record("item-global", kind, project_id="proj-alpha"),
                      record("item-alpha", kind, scope=["proj-alpha"], project_id="proj-beta")])
    assert getattr(reader, method)(record_id="item-global", project_id="proj-beta")["ok"]
    response = getattr(reader, method)(record_id="item-alpha", project_id="proj-beta")
    assert response["error"]["code"] == "NOT_FOUND"
    assert "范围" in response["error"]["message"]


def test_order_is_score_then_timestamp_instant_then_id_not_input_order():
    entries = [lesson("lesson-beta", title="科学", updated_at="2026-11-20T09:00:00+08:00"),
               lesson("lesson-abc", title="科学", updated_at="2026-11-20T01:00:00Z"),
               lesson("lesson-new", title="科学", updated_at="2026-11-20T00:30:00-01:00"),
               lesson("lesson-body", text="科学", updated_at="2026-11-21T00:00:00Z")]
    expected = ["lesson-new", "lesson-abc", "lesson-beta", "lesson-body"]
    for records in (entries, list(reversed(entries))):
        assert ids(service(records).search_lessons("科学")["data"]["items"]) == expected


def test_tag_filter_is_exact_normalized_and_requires_all_tags():
    reader = service([lesson(tags=["Python", "测试"]), lesson("lesson-partial", tags=["Python3", "测试"])])
    assert ids(reader.search_lessons(tags=["ｐｙｔｈｏｎ", "测试"])["data"]["items"]) == ["lesson-abc"]
    assert reader.search_lessons(tags=["Python", "没有标签"])["data"]["items"] == []
    mismatch = reader.search_lessons(record_id="lesson-abc", tags=["missing"])
    assert mismatch["error"]["code"] == "NOT_FOUND"
    assert "标签" in mismatch["error"]["message"]


def test_empty_query_lists_but_no_match_returns_empty_without_invented_fact():
    reader = service([lesson()])
    assert ids(reader.search_lessons()["data"]["items"]) == ["lesson-abc"]
    response = reader.search_lessons("确实没有的词")
    assert response["ok"]
    assert response["data"] == {"items": [], "total": 0, "next_cursor": None}


@pytest.mark.parametrize("status", ["archived", "superseded"])
def test_lesson_history_default_filter_and_explicit_id_warning(status):
    reader = service([lesson(status=status)])
    assert reader.search_lessons()["data"]["items"] == []
    assert ids(reader.search_lessons(include_history=True)["data"]["items"]) == ["lesson-abc"]
    response = reader.search_lessons(record_id="lesson-abc")
    assert response["ok"]
    assert response["data"]["items"][0]["status"] == status
    assert response["warnings"]


def test_search_only_returns_location_excerpt_and_source():
    original = lesson(text="前" * 700 + "唯一命中" + "后" * 700)
    response = service([original]).search_lessons("唯一命中")
    item = response["data"]["items"][0]
    assert "唯一命中" in item["excerpt"]
    assert len(item["excerpt"]) <= 240
    assert "body" not in item
    assert item["source"] == {"id": original.id, "relative_path": original.relative_path,
                              "updated_at": original.metadata["updated_at"], "snapshot_commit": COMMIT}


@pytest.mark.parametrize("prefix", ["Straße" * 150, "e\u0301" * 350, "\ufb03" * 350, " " * 800])
def test_excerpt_preserves_original_offset_after_unicode_length_changes(prefix):
    original = lesson(text=prefix + "唯一命中" + "后" * 500)
    item = service([original]).search_lessons("唯一命中")["data"]["items"][0]
    assert "唯一命中" in item["excerpt"]
    assert item["excerpt"] in original.body
    assert len(item["excerpt"]) <= 240


def test_timestamp_sort_does_not_lose_microseconds_in_float_conversion():
    reader = service([lesson("lesson-aaa", updated_at="9000-01-01T00:00:00.000001Z"),
                      lesson("lesson-zzz", updated_at="9000-01-01T00:00:00.000002Z")])
    assert ids(reader.search_lessons()["data"]["items"]) == ["lesson-zzz", "lesson-aaa"]


def test_default_page_size_and_explicit_maximum():
    reader = service([lesson(f"lesson-{number:03}") for number in range(105)])
    assert len(reader.search_lessons()["data"]["items"]) == 10
    assert len(reader.search_lessons(limit=100)["data"]["items"]) == 100


def test_pagination_is_deterministic_across_reader_instances_and_exhausts_once():
    entries = [lesson(f"lesson-{number:03}") for number in range(5)]
    first = service(entries).search_lessons(limit=2)
    second = service(list(reversed(entries))).search_lessons(limit=2, cursor=first["data"]["next_cursor"])
    third = service(entries).search_lessons(limit=2, cursor=second["data"]["next_cursor"])
    assert ids(first["data"]["items"] + second["data"]["items"] + third["data"]["items"]) == [entry.id for entry in entries]
    assert third["data"]["next_cursor"] is None
    assert first["data"]["total"] == second["data"]["total"] == third["data"]["total"] == 5


@pytest.mark.parametrize("parameters", [{"query": "changed"}, {"tags": ["new"]}, {"include_history": True},
                                        {"project_id": "proj-alpha"}, {"limit": 1}, {"record_id": "lesson-000"}])
def test_cursor_binds_all_query_filters(parameters):
    entries = [record("proj-alpha", "project"), lesson("lesson-000"), lesson("lesson-001"), lesson("lesson-002")]
    reader = service(entries)
    cursor = reader.search_lessons(limit=2)["data"]["next_cursor"]
    response = reader.search_lessons(**{"limit": 2, "cursor": cursor, **parameters})
    assert response["error"]["code"] == "INVALID_CURSOR"


def test_cursor_binds_snapshot_and_endpoint():
    entries = [lesson("lesson-000"), lesson("lesson-001")]
    cursor = service(entries).search_lessons(limit=1)["data"]["next_cursor"]
    assert service(entries, commit="b" * 40).search_lessons(limit=1, cursor=cursor)["error"]["code"] == "INVALID_CURSOR"
    assert service(entries).find_deliverables(limit=1, cursor=cursor)["error"]["code"] == "INVALID_CURSOR"


@pytest.mark.parametrize("parameters", [{"audience": "parents"}, {"include_unavailable": True}])
def test_deliverable_cursor_binds_permission_filters(parameters):
    reader = service([record("doc-alpha", "deliverable"), record("doc-beta", "deliverable")])
    cursor = reader.find_deliverables(limit=1)["data"]["next_cursor"]
    response = reader.find_deliverables(limit=1, cursor=cursor, **parameters)
    assert response["error"]["code"] == "INVALID_CURSOR"


def test_cursor_accepts_equivalent_normalized_query_and_tag_sets():
    reader = service([lesson("lesson-000", title="Python", tags=["py", "dev"]),
                      lesson("lesson-001", title="Python", tags=["py", "dev"])])
    cursor = reader.search_lessons("ＰＹＴＨＯＮ", tags=["py", "dev"], limit=1)["data"]["next_cursor"]
    response = reader.search_lessons("python", tags=["dev", "py", "py"], limit=1, cursor=cursor)
    assert ids(response["data"]["items"]) == ["lesson-001"]


@pytest.mark.parametrize("offset", [0, -1, True, "1", 1000])
def test_invalid_cursor_offsets_are_rejected(offset):
    reader = service([lesson("lesson-000"), lesson("lesson-001")])
    cursor = reader.search_lessons(limit=1)["data"]["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(cursor))
    payload["offset"] = offset
    bad_cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    assert reader.search_lessons(limit=1, cursor=bad_cursor)["error"]["code"] == "INVALID_CURSOR"


@pytest.mark.parametrize("cursor", ["", "not base64!", "e30=", "bnVsbA==", "W10=", [], 5,
                                   base64.urlsafe_b64encode(json.dumps({"offset": True}).encode()).decode()])
def test_malformed_cursor_is_safe_structured_error(cursor):
    response = service([lesson()]).search_lessons(cursor=cursor)
    assert response["error"]["code"] == "INVALID_CURSOR"


@pytest.mark.parametrize("method,parameters", [
    ("search_lessons", {"query": None}), ("search_lessons", {"query": []}),
    ("search_lessons", {"tags": "python"}), ("search_lessons", {"tags": [None]}),
    ("search_lessons", {"include_history": "true"}), ("search_lessons", {"limit": False}),
    ("find_deliverables", {"audience": []}), ("find_deliverables", {"audience": ""}),
    ("find_deliverables", {"include_unavailable": 1}),
])
def test_invalid_search_parameters_are_structured(method, parameters):
    response = getattr(service(), method)(**parameters)
    assert response["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("share_status,status,visible,can_share", [
    ("ready", "active", True, True), ("internal", "active", False, False),
    ("withdrawn", "active", False, False), ("ready", "archived", False, False),
    ("ready", "superseded", False, False),
])
def test_deliverable_default_visibility_and_exact_read_permission(share_status, status, visible, can_share):
    original = record("doc-guide", "deliverable", share_status=share_status, status=status)
    reader = service([original])
    assert bool(reader.find_deliverables()["data"]["items"]) is visible
    assert ids(reader.find_deliverables(include_unavailable=True)["data"]["items"]) == ["doc-guide"]
    response = reader.find_deliverables(record_id="doc-guide")
    item = response["data"]["items"][0]
    assert item["body"] == original.body
    assert item["permission"] == {"can_share": can_share, "share_status": share_status, "audience": ["parents"]}
    if not can_share:
        assert response["warnings"]


def test_deliverable_full_body_separates_internal_metadata_and_does_not_infer_publication():
    original = record("doc-guide", "deliverable", body="# 家长说明\n" + "完整" * 2000,
                      evidence_summary="内部依据", publication_events=[])
    reader = service([original])
    listed = reader.find_deliverables()["data"]["items"][0]
    assert "body" not in listed
    response = reader.find_deliverables(record_id="doc-guide")
    item = response["data"]["items"][0]
    assert item["body"] == original.body
    assert "内部依据" not in item["body"]
    assert item["internal_metadata"]["evidence_summary"] == "内部依据"
    assert item["publication"]["current_body_published"] is None
    assert item["publication"]["events"] == []
    assert item["publication"]["note"] == "库内无发布记录"


def test_historical_publication_event_never_marks_current_body_published_or_unwithdrawn():
    event = {"id": "pub-abc", "published_at": "2026-11-01T00:00:00Z", "channel": "chat",
             "audience": ["parents"], "result": "本人确认已发出", "source_commit": "b" * 40}
    original = record("doc-guide", "deliverable", share_status="withdrawn", publication_events=[event])
    response = service([original]).find_deliverables(record_id="doc-guide")
    item = response["data"]["items"][0]
    assert item["publication"]["events"] == [event]
    assert item["publication"]["current_body_published"] is None
    assert "当前正文" in item["publication"]["note"]
    assert item["permission"]["can_share"] is False


@pytest.mark.parametrize("audience", ["Parents", "parent", "家长", "Ｐａｒｅｎｔｓ", "students"])
def test_audience_filter_is_exact_and_cannot_be_bypassed_by_id(audience):
    reader = service([record("doc-guide", "deliverable")])
    assert reader.find_deliverables(audience=audience)["data"]["items"] == []
    response = reader.find_deliverables(record_id="doc-guide", audience=audience)
    assert response["error"]["code"] == "NOT_FOUND"
    assert "受众" in response["error"]["message"]


def test_matching_audience_can_read_ready_document_and_id_is_not_text_scored():
    response = service([record("doc-guide", "deliverable")]).find_deliverables(
        record_id="doc-guide", audience="parents", query="另一个不相关词")
    assert response["ok"]
    assert response["data"]["items"][0]["score"] == 100


@pytest.mark.parametrize("method,parameters", [
    ("search_lessons", {"record_id": "lesson-absent"}),
    ("search_lessons", {"record_id": "doc-guide"}),
    ("find_deliverables", {"record_id": "lesson-abc"}),
    ("find_deliverables", {"project_id": "proj-absent"}),
])
def test_explicit_missing_or_wrong_type_query_is_not_silent_empty_result(method, parameters):
    response = getattr(service([lesson(), record("doc-guide", "deliverable")]), method)(**parameters)
    assert response["error"]["code"] == "NOT_FOUND"
