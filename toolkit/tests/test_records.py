import importlib.util
from pathlib import Path
import traceback

import pytest
import yaml

from tests.helpers import PROJECT_BODY, make_markdown


def test_record_implementation_exists():
    assert importlib.util.find_spec("mcp_server.records") is not None


def api():
    from mcp_server import records

    return records


def parse(record_id="proj-alpha", record_type="project", body=None, **overrides):
    return api().parse_record(make_markdown(record_id, record_type, body, **overrides))


def assert_error(code, operation):
    from mcp_server.models import HubError

    with pytest.raises(HubError) as caught:
        operation()
    assert caught.value.code == code
    assert caught.value.as_dict() == {
        "ok": False,
        "error": {"code": code, "message": caught.value.message, "retryable": False},
    }
    return caught.value


def test_valid_record_round_trip_and_defaults():
    record = parse()
    assert (record.id, record.type, record.status) == ("proj-alpha", "project", "active")
    assert record.relative_path == "knowledge/L1_projects/proj-alpha.md"
    assert record.body == PROJECT_BODY
    assert record.metadata["related_ids"] == []
    assert api().parse_record(api().dump_record(record.metadata, record.body)).metadata == record.metadata
    assert api().validate_records({record.id: record}) == []


@pytest.mark.parametrize("record_type", ["core", "project", "lesson", "deliverable"])
@pytest.mark.parametrize("status", ["active", "archived", "superseded"])
def test_all_record_types_have_canonical_paths(record_type, status):
    record = parse("test-record", record_type, status=status)
    directory = {
        "core": "L0_core", "project": "L1_projects", "lesson": "L2_lessons", "deliverable": "deliverables"
    }[record_type] if status == "active" else f"L3_archive/{record_type}"
    assert record.relative_path == f"knowledge/{directory}/test-record.md"
    restored = parse("test-record", record_type)
    assert restored.id == record.id


@pytest.mark.parametrize("overrides", [
    {"id": "../escape"}, {"id": "UPPER"}, {"id": "ab"}, {"id": "a" * 81},
    {"id": 123}, {"type": "unknown"}, {"type": []}, {"title": ""},
    {"title": "line\nbreak"}, {"title": 12}, {"status": "deleted"},
    {"scope": "global"}, {"scope": []}, {"scope": ["global", "proj-alpha"]},
    {"scope": ["proj-other"]}, {"scope": ["proj-alpha", "proj-alpha"]},
    {"updated_at": "2026-11-20T09:00:00"}, {"updated_at": "2026-02-30T09:00:00Z"},
    {"updated_at": "2026-11-20 09:00:00+08:00"}, {"tags": [1]},
    {"aliases": ["line\nbreak"]}, {"related_ids": ["../bad"]}, {"supersedes": ["proj-alpha"]},
    {"version": "1.0"}, {"summary": []}, {"summary": "line\nbreak"},
    {"kind": "decision"}, {"project_id": "proj-other"}, {"evidence_summary": 12},
])
def test_rejects_invalid_common_fields(overrides):
    markdown = make_markdown(**overrides)
    assert_error("INVALID_RECORD", lambda: api().parse_record(markdown))


@pytest.mark.parametrize("field", ["id", "type", "title", "scope", "status", "updated_at", "summary"])
def test_required_fields(field):
    markdown = make_markdown()
    metadata = yaml.safe_load(markdown.split("---", 2)[1])
    del metadata[field]
    candidate = "---\n" + yaml.safe_dump(metadata, allow_unicode=True) + "---\n" + PROJECT_BODY
    assert_error("INVALID_RECORD", lambda: api().parse_record(candidate))


@pytest.mark.parametrize("fragment", [
    "title: overwritten\n", "tags: &labels [one]\naliases: *labels\n",
    "evidence_summary: !!python/object/apply:os.system ['touch forbidden']\n",
    "tags: [unclosed\n", "? [one, two]\n: invalid\n", "<<: {title: injected}\n",
])
def test_yaml_rejects_duplicate_alias_custom_objects_and_invalid_shapes(fragment):
    markdown = make_markdown().replace("---\n", "---\n" + fragment, 1)
    error = assert_error("INVALID_RECORD", lambda: api().parse_record(markdown))
    assert "forbidden" not in error.message
    assert "overwritten" not in error.message


@pytest.mark.parametrize("scalar", [
    "!!bool secret-invalid-bool", "!!int 0x", "!!float .", "!!timestamp secret-invalid-time",
    '!!int ""', '!!float ""',
    "!!bool [secret-invalid-shape]", "!!int {secret: invalid}",
])
def test_malformed_standard_yaml_tags_are_structured_errors(scalar):
    markdown = make_markdown().replace("title: 测试记录", f"title: {scalar}")
    assert_error("INVALID_RECORD", lambda: api().parse_record(markdown))


@pytest.mark.parametrize("fragment", [
    "evidence_summary: !!python/object/apply:os.system [SECRET-MARKER]\n",
    "evidence_summary: [SECRET-MARKER\n",
    "evidence_summary: !!int SECRET-MARKER\n",
])
def test_yaml_tracebacks_suppress_untrusted_source_context(fragment):
    markdown = make_markdown().replace("---\n", "---\n" + fragment, 1)
    error = assert_error("INVALID_RECORD", lambda: api().parse_record(markdown))
    formatted = "".join(traceback.format_exception(error))
    assert "SECRET-MARKER" not in formatted
    assert error.__suppress_context__


@pytest.mark.parametrize("offset", ["+08:60", "+00:99", "-08:60", "+24:00"])
@pytest.mark.parametrize("field", ["updated_at", "valid_from", "valid_until", "published_at"])
def test_rfc3339_rejects_invalid_offset_components(offset, field):
    timestamp = "2026-11-20T00:00:00" + offset
    if field == "updated_at":
        assert_error("INVALID_RECORD", lambda: parse(updated_at=timestamp))
    elif field in {"valid_from", "valid_until"}:
        constraints = constraint_examples()
        constraints[1][field] = timestamp
        assert_error("CONSTRAINT_CONFLICT", lambda: parse(constraints=constraints))
    else:
        event = {"id": "pub-first", "published_at": timestamp, "channel": "私信", "audience": ["parents"], "result": "已发送"}
        assert_error("INVALID_RECORD", lambda: parse("doc-alpha", "deliverable", publication_events=[event]))


@pytest.mark.parametrize("character", ["\x00", "\x1b", "\x7f", "\x85", "\u061c", "\u200e", "\u202e", "\u2066", "\r"])
@pytest.mark.parametrize("location", ["body", "title"])
def test_forbidden_control_characters(character, location):
    markdown = make_markdown(body=PROJECT_BODY + character) if location == "body" else make_markdown(title="secret" + character)
    error = assert_error("INVALID_RECORD", lambda: api().parse_record(markdown))
    assert "secret" not in error.message


def test_lf_normalization_and_literal_escape():
    markdown = make_markdown(body=PROJECT_BODY + "\\u001b\t文本\n").replace("\n", "\r\n")
    record = api().parse_record(markdown)
    assert "\r" not in record.markdown
    assert "\\u001b" in record.body


@pytest.mark.parametrize("record_type,overrides", [
    ("core", {"views": []}), ("core", {"views": ["unknown"]}),
    ("core", {"views": ["base", "base"]}), ("core", {"strength": "hard"}),
    ("core", {"kind": "decision"}), ("lesson", {"views": ["base"]}),
    ("deliverable", {"format": "pdf"}), ("deliverable", {"share_status": "public"}),
    ("deliverable", {"audience": []}), ("deliverable", {"purpose": ""}),
    ("deliverable", {"audience": "parents"}), ("deliverable", {"publication_events": {}}),
])
def test_type_specific_fields(record_type, overrides):
    assert_error("INVALID_RECORD", lambda: parse("test-record", record_type, **overrides))


@pytest.mark.parametrize("record_type", ["project", "lesson"])
def test_required_headings_cannot_be_faked_by_code_fence(record_type):
    body = make_markdown("test-record", record_type).split("---\n", 2)[2]
    assert_error("INVALID_RECORD", lambda: parse("test-record", record_type, body="```markdown\n" + body + "\n```\n"))


@pytest.mark.parametrize("record_type", ["project", "lesson"])
@pytest.mark.parametrize("closed", [False, True])
def test_required_headings_cannot_be_faked_by_html_comments(record_type, closed):
    body = make_markdown("test-record", record_type).split("---\n", 2)[2]
    commented = "<!--\n" + body + ("\n-->\n" if closed else "")
    assert_error("INVALID_RECORD", lambda: parse("test-record", record_type, body=commented))


def test_comment_fences_cannot_toggle_markdown_heading_recognition():
    body = "<!--\n```\n-->\n```markdown\n" + PROJECT_BODY + "\n```\n"
    assert_error("INVALID_RECORD", lambda: parse(body=body))


def test_visible_headings_with_comments_and_fenced_comment_literals_remain_valid():
    body = "```html\n<!--\n```\n" + PROJECT_BODY.replace("\n内容", " <!--说明-->\n内容")
    assert parse(body=body).body == body


def test_comment_marker_in_fence_info_does_not_hide_visible_headings():
    body = "```html <!--\ncode\n```\n" + PROJECT_BODY
    assert parse(body=body).body == body


def test_l1_body_has_no_length_limit_and_markdown_count_includes_syntax():
    body = PROJECT_BODY + "内容" * 10000
    assert parse(body=body).body == body
    assert api().body_count("# 文\t字\n😀 \u3000") == 4


@pytest.mark.parametrize("relative_path", [
    "/knowledge/L1_projects/proj-alpha.md", "knowledge/L1_projects/../proj-alpha.md",
    "knowledge/L3_archive/project/proj-alpha.md", "knowledge/L1_projects/other.md",
    "knowledge\\L1_projects\\proj-alpha.md", "knowledge/L1_projects/proj-alpha.MD",
])
def test_rejects_noncanonical_path(relative_path):
    assert_error("INVALID_RECORD", lambda: api().parse_record(make_markdown(), relative_path))


def test_missing_references_preflight_and_final_validation():
    record = parse("doc-alpha", "deliverable", scope=["proj-next"], project_id="proj-source", related_ids=["exp-next"])
    records = {record.id: record}
    assert api().validate_records(records, allow_unresolved=True) == ["exp-next", "proj-next", "proj-source"]
    assert_error("INVALID_REFERENCE", lambda: api().validate_records(records))
    for record in [parse("proj-next"), parse("proj-source"), parse("exp-next", "lesson")]:
        records[record.id] = record
    assert api().validate_records(records) == []


def test_scope_and_source_are_independent_but_must_reference_projects():
    project = parse()
    lesson = parse("exp-alpha", "lesson", scope=["global"], project_id=project.id)
    assert api().validate_records({project.id: project, lesson.id: lesson}) == []
    invalid = parse("doc-alpha", "deliverable", scope=[lesson.id])
    assert_error("INVALID_REFERENCE", lambda: api().validate_records({project.id: project, lesson.id: lesson, invalid.id: invalid}, True))


def test_supersession_requires_superseded_target_and_no_cycles():
    old = parse("exp-old", "lesson")
    new = parse("exp-new", "lesson", supersedes=[old.id])
    assert_error("INVALID_REFERENCE", lambda: api().validate_records({old.id: old, new.id: new}))
    old = parse("exp-old", "lesson", status="superseded")
    assert api().validate_records({old.id: old, new.id: new}) == []
    old = parse("exp-old", "lesson", status="superseded", supersedes=[new.id])
    new = parse("exp-new", "lesson", status="superseded", supersedes=[old.id])
    assert_error("INVALID_REFERENCE", lambda: api().validate_records({old.id: old, new.id: new}))


def test_snapshot_rejects_mismatched_dictionary_key():
    assert_error("INVALID_RECORD", lambda: api().validate_records({"wrong-id": parse()}))


@pytest.mark.parametrize("metadata", [None, [], {"id": "proj-broken"}])
def test_full_validation_rejects_malformed_record_objects_without_crashing(metadata):
    from mcp_server.models import Record

    broken = Record(metadata=metadata, body="text", markdown="invalid", relative_path="invalid")
    reference = parse(related_ids=["proj-broken"], scope=["proj-alpha"])
    assert_error("INVALID_RECORD", lambda: api().validate_records({reference.id: reference, "proj-broken": broken}))


def test_full_validation_checks_target_before_project_reference_type():
    from mcp_server.models import Record

    source = parse("doc-first", "deliverable", project_id="proj-broken")
    broken = Record(metadata={"id": "proj-broken"}, body="text", markdown="invalid", relative_path="invalid")
    assert_error("INVALID_RECORD", lambda: api().validate_records({source.id: source, "proj-broken": broken}))


def test_full_validation_rejects_none_relative_path_in_record():
    record = parse()
    record.relative_path = None
    assert_error("INVALID_RECORD", lambda: api().validate_records({record.id: record}))


@pytest.mark.parametrize("original,replacement", [(True, 1), (False, 0), (1, 1.0), (0, False)])
def test_full_validation_preserves_constraint_value_scalar_types(original, replacement):
    record = parse(constraints=[{"id": "baseline", "key": "enabled", "kind": "baseline", "value": original}])
    record.metadata["constraints"][0]["value"] = replacement
    assert_error("INVALID_RECORD", lambda: api().validate_records({record.id: record}))


def test_escaped_yaml_control_character_in_unused_nested_field_is_rejected():
    assert_error("INVALID_RECORD", lambda: parse(constraints=[{"id": "normal", "key": "key", "kind": "baseline", "value": "secret\x1b"}]))


def test_safe_dump_rejects_self_referential_metadata():
    metadata = {"id": "proj-alpha"}
    metadata["metadata"] = metadata
    assert_error("INVALID_RECORD", lambda: api().dump_record(metadata, "body"))


def test_duplicate_record_id_across_active_and_archive_is_rejected(tmp_path):
    write_record(tmp_path, parse())
    write_record(tmp_path, parse(status="archived"))
    assert_error("INVALID_RECORD", lambda: api().load_snapshot(tmp_path))


def test_replacement_cycle_longer_than_two_is_rejected():
    records = [parse("exp-first", "lesson", status="superseded", supersedes=["exp-next"]),
               parse("exp-next", "lesson", status="superseded", supersedes=["exp-last"]),
               parse("exp-last", "lesson", status="superseded", supersedes=["exp-first"])]
    assert_error("INVALID_REFERENCE", lambda: api().validate_records({record.id: record for record in records}))


def constraint_examples():
    return [
        {"id": "regular", "key": "minutes", "kind": "baseline", "value": 180},
        {"id": "special", "key": "minutes", "kind": "exception", "value": 60,
         "overrides": "regular", "valid_from": "2026-11-20T00:00:00+08:00", "valid_until": "2026-11-27T00:00:00+08:00"},
    ]


def test_constraints_allow_touching_half_open_timezone_intervals():
    constraints = constraint_examples()
    constraints.append({**constraints[1], "id": "special-next", "valid_from": "2026-11-26T16:00:00Z", "valid_until": "2026-11-28T00:00:00Z"})
    assert len(parse(constraints=constraints).metadata["constraints"]) == 3


@pytest.mark.parametrize("change", [
    "missing_baseline", "duplicate_baseline", "duplicate_id", "wrong_key", "missing_timezone",
    "inverted", "equal", "overlap", "unknown_field", "invalid_kind", "nested_value",
])
def test_rejects_invalid_project_constraints(change):
    constraints = constraint_examples()
    if change == "missing_baseline":
        constraints.pop(0)
    elif change == "duplicate_baseline":
        constraints.append({**constraints[0], "id": "second"})
    elif change == "duplicate_id":
        constraints[1]["id"] = "regular"
    elif change == "wrong_key":
        constraints[1]["key"] = "other"
    elif change == "missing_timezone":
        constraints[1]["valid_from"] = "2026-11-20T00:00:00"
    elif change == "inverted":
        constraints[1]["valid_until"] = "2026-11-19T00:00:00Z"
    elif change == "equal":
        constraints[1]["valid_until"] = constraints[1]["valid_from"]
    elif change == "overlap":
        constraints.append({**constraints[1], "id": "second"})
    elif change == "unknown_field":
        constraints[0]["expression"] = "unsafe"
    elif change == "invalid_kind":
        constraints[0]["kind"] = "fallback"
    else:
        constraints[0]["value"] = {"arbitrary": "object"}
    assert_error("CONSTRAINT_CONFLICT", lambda: parse(constraints=constraints))


def test_l0_budget_is_per_global_plus_project_and_base_deduplicated():
    records = [parse(), parse("proj-beta")]
    records += [
        parse("core-global", "core", body="字" * 100, views=["base", "coding"]),
        parse("core-alpha", "core", body="字" * 300, scope=["proj-alpha"]),
        parse("core-beta", "core", body="字" * 300, scope=["proj-beta"]),
        parse("core-code", "core", body="字" * 300, views=["coding"]),
    ]
    snapshot = {record.id: record for record in records}
    assert api().validate_records(snapshot) == []
    overflow = parse("core-overflow", "core", body="字", scope=["proj-alpha"])
    assert_error("BUDGET_EXCEEDED", lambda: api().validate_records({**snapshot, overflow.id: overflow}))


@pytest.mark.parametrize("view", ["base", "coding", "thinking"])
def test_l0_limits_are_aggregate_not_per_record_and_ignore_history(view):
    limit = 400 if view == "base" else 300
    records = [parse("core-first", "core", body="字" * limit, views=[view]), parse("core-old", "core", body="字" * 1000, status="archived", views=[view])]
    snapshot = {record.id: record for record in records}
    assert api().validate_records(snapshot) == []
    record = parse("core-second", "core", body="字", views=[view])
    assert_error("BUDGET_EXCEEDED", lambda: api().validate_records({**snapshot, record.id: record}))


def test_unresolved_project_budget_checked_before_approval():
    record = parse("core-project", "core", body="字" * 401, scope=["proj-future"])
    assert_error("BUDGET_EXCEEDED", lambda: api().validate_records({record.id: record}, allow_unresolved=True))


def test_publication_events_are_typed_and_unique():
    event = {"id": "pub-first", "published_at": "2026-11-20T00:00:00Z", "channel": "私信", "audience": ["parents"], "result": "已发送", "source_commit": "a" * 40}
    assert parse("doc-alpha", "deliverable", publication_events=[event]).metadata["publication_events"] == [event]
    for events in [[event, event], [{**event, "published_at": "2026-11-20"}], [{**event, "source_commit": "not-a-sha"}], [{**event, "unexpected": True}]]:
        assert_error("INVALID_RECORD", lambda: parse("doc-alpha", "deliverable", publication_events=events))


def write_record(root, record):
    filename = root / record.relative_path
    filename.parent.mkdir(parents=True, exist_ok=True)
    filename.write_text(record.markdown, encoding="utf-8")
    return filename


def test_load_snapshot_empty_and_valid(tmp_path):
    assert api().load_snapshot(tmp_path) == {}
    (tmp_path / "knowledge").mkdir()
    assert api().load_snapshot(tmp_path) == {}
    record = parse()
    write_record(tmp_path, record)
    assert api().load_snapshot(tmp_path)[record.id].body == record.body


@pytest.mark.parametrize("invalid_name", [".DS_Store", "notes.txt", "random.md", "L1_projects/.hidden.md", "extra/note.md"])
def test_load_snapshot_rejects_unknown_knowledge_files(tmp_path, invalid_name):
    filename = tmp_path / "knowledge" / invalid_name
    filename.parent.mkdir(parents=True, exist_ok=True)
    filename.write_text(make_markdown(), encoding="utf-8")
    assert_error("INVALID_RECORD", lambda: api().load_snapshot(tmp_path))


@pytest.mark.parametrize("directory_link", [False, True])
def test_load_snapshot_rejects_symlinks(tmp_path, directory_link):
    filename = write_record(tmp_path, parse())
    target = filename.parent if directory_link else filename
    (tmp_path / "knowledge" / "linked").symlink_to(target, target_is_directory=directory_link)
    assert_error("INVALID_RECORD", lambda: api().load_snapshot(tmp_path))


def test_load_snapshot_rejects_invalid_utf8_and_dangling_references(tmp_path):
    filename = write_record(tmp_path, parse(related_ids=["missing-id"]))
    assert_error("INVALID_REFERENCE", lambda: api().load_snapshot(tmp_path))
    filename.write_bytes(b"\xff")
    assert_error("INVALID_RECORD", lambda: api().load_snapshot(tmp_path))
