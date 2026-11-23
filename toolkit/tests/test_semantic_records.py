from pathlib import Path

import pytest

from mcp_server.models import HubError
from mcp_server.records import dump_record, parse_record, validate_records
from mcp_server.storage import atomic_json
from tests.git_helpers import git, publish
from tests.helpers import make_markdown
from tests.test_batch import accept, setup_review
from tests.test_proposals import setup_store, submit


def without_slug(markdown):
    record = parse_record(markdown)
    metadata = dict(record.metadata)
    metadata.pop("slug", None)
    return dump_record(metadata, record.body)


def test_semantic_filename_is_independent_from_id_and_title():
    record = parse_record(make_markdown("core-opaque", "core", slug="general-preferences", title="通用个人偏好"))
    renamed = parse_record(make_markdown("core-opaque", "core", slug="general-preferences", title="交流与授权偏好"))
    assert record.relative_path == renamed.relative_path == "knowledge/L0_core/general-preferences.md"
    assert record.id == renamed.id == "core-opaque"
    chinese = parse_record(make_markdown("core-chinese", "core", slug="编码偏好"))
    assert chinese.relative_path == "knowledge/L0_core/编码偏好.md"


def test_legacy_paths_remain_readable_without_automatic_rename():
    markdown = without_slug(make_markdown("core-old", "core"))
    assert parse_record(markdown, "knowledge/L0_core/core-old.md").relative_path.endswith("/core-old.md")


@pytest.mark.parametrize("slug", ["../escape", "sub/name", "a\\b", "Title", "name.md", ".hidden", "a--b",
                                 "", "a b", "`code`", "x" * 81, 12, "core-" + "a" * 32])
def test_slug_rejects_unsafe_or_opaque_filenames(slug):
    with pytest.raises(HubError) as caught:
        parse_record(make_markdown("core-invalid", "core", slug=slug))
    assert caught.value.code == "INVALID_RECORD"


def test_new_create_requires_semantic_slug(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    with pytest.raises(HubError) as caught:
        submit(store, snapshot, without_slug(make_markdown("doc-new", "deliverable")),
               operation="create", target="doc-new")
    assert caught.value.code == "INVALID_RECORD"
    assert "slug" in caught.value.message


def test_path_collision_cannot_overwrite_another_record():
    first = parse_record(make_markdown("core-one", "core", slug="general-preferences", title="交流偏好"))
    second = parse_record(make_markdown("core-two", "core", slug="general-preferences", title="编码偏好"))
    with pytest.raises(HubError) as caught:
        validate_records({first.id: first, second.id: second})
    assert caught.value.code == "DUPLICATE_PATH"


def test_same_normalized_title_in_same_scope_is_one_document():
    first = parse_record(make_markdown("core-one", "core", slug="general-preferences", title="Agent 偏好"))
    second = parse_record(make_markdown("core-two", "core", slug="another-name", title="  ＡＧＥＮＴ   偏好  "))
    with pytest.raises(HubError) as caught:
        validate_records({first.id: first, second.id: second})
    assert caught.value.code == "DUPLICATE_TOPIC"


def test_same_title_in_different_scopes_is_not_silently_merged():
    project = parse_record(make_markdown())
    first = parse_record(make_markdown("core-one", "core", slug="general-preferences", title="编码偏好"))
    second = parse_record(make_markdown("core-two", "core", slug="alpha-preferences", title="编码偏好",
                                        scope=[project.id]))
    assert validate_records({record.id: record for record in (project, first, second)}) == []


def test_duplicate_create_returns_original_full_text_for_merging(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    original = make_markdown("doc-original", "deliverable", slug="agent-preferences", title="Agent 偏好",
                             body="保留的原有内容\n", share_status="internal", audience=[])
    publish(work, original)
    current = store.snapshots.refresh()
    incoming = make_markdown("doc-other", "deliverable", slug="agent-preferences", title="新标题",
                             body="新增内容\n", share_status="internal", audience=[])
    result = submit(store, current, incoming, operation="create", target="doc-other")
    assert result["ok"] is False and result["error"]["code"] == "DUPLICATE_TOPIC"
    assert result["merge_target"]["target_id"] == "doc-original"
    assert result["merge_target"]["operation"] == "replace"
    assert result["merge_target"]["proposed_markdown"] == original
    assert not list(store.config.proposal_dir.glob("*.json"))


def test_duplicate_pending_title_returns_draft_not_another_record(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    first = submit(store, snapshot, make_markdown("doc-one", "deliverable", slug="agent-preferences", title="偏好"),
                   operation="create", target="doc-one")
    second = submit(store, snapshot, make_markdown("doc-two", "deliverable", slug="another-name", title="偏好"),
                    operation="create", target="doc-two")
    assert second["ok"] is False and second["error"]["code"] == "DUPLICATE_TOPIC"
    assert second["merge_target"]["proposal_id"] == first["data"]["proposal_id"]
    assert second["merge_target"]["operation"] == "create"
    assert second["merge_target"]["target_id"] == "doc-one"
    assert len(list(store.config.proposal_dir.glob("*.json"))) == 1


def test_successive_updates_preserve_one_file_and_prior_content(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    first = submit(store, snapshot, make_markdown("doc-preferences", "deliverable", slug="agent-preferences",
                   title="Agent 偏好", body="已有规则。\n", share_status="internal", audience=[]),
                   operation="create", target="doc-preferences")
    assert service.approve_delivery(first["data"]["delivery"]["delivery_id"], accept)["state"] == "synced"
    current = store.snapshots.refresh()
    updated = submit(store, current, make_markdown("doc-preferences", "deliverable", slug="agent-preferences",
                     title="Agent 协作偏好", body="已有规则。\n新增规则。\n", share_status="internal", audience=[]),
                     operation="replace", target="doc-preferences")
    assert service.approve_delivery(updated["data"]["delivery"]["delivery_id"], accept)["state"] == "synced"
    current = store.snapshots.refresh()
    record = current.records["doc-preferences"]
    assert record.body == "已有规则。\n新增规则。\n"
    assert record.relative_path == "knowledge/deliverables/agent-preferences.md"
    paths = git(work, "--git-dir", str(service.repository.path), "ls-tree", "-r", "--name-only", current.commit)
    assert paths.splitlines().count(record.relative_path) == 1
    assert "doc-preferences.md" not in paths


def test_legacy_filename_migration_is_reviewed_and_keeps_id(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    publish(work, without_slug(make_markdown()))
    snapshot = store.snapshots.refresh()
    current_record = snapshot.records["proj-alpha"]
    assert "slug" not in current_record.metadata
    updated = make_markdown(slug="project-overview", title="项目概览")
    result = submit(store, snapshot, updated)
    delivery = result["data"]["delivery"]
    human = Path(delivery["human_path"]).read_text()
    assert "原位置：`" + current_record.relative_path + "`" in human
    assert service.approve_delivery(delivery["delivery_id"], accept)["state"] == "synced"
    current = store.snapshots.refresh()
    assert current.records["proj-alpha"].relative_path == "knowledge/L1_projects/project-overview.md"
    paths = git(work, "--git-dir", str(service.repository.path), "ls-tree", "-r", "--name-only", current.commit)
    assert current_record.relative_path not in paths


def test_readable_delivery_paths_and_agent_filenames(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    result = submit(store, snapshot, make_markdown("core-first", "core", slug="general-preferences", title="通用偏好"),
                    operation="create", target="core-first")
    delivery = result["data"]["delivery"]
    human = Path(delivery["human_path"]).read_text()
    assert "知识分类：核心偏好" in human
    assert "文档名称：`general-preferences.md`" in human
    assert "目标位置：`knowledge/L0_core/general-preferences.md`" in human
    assert "L0\\_core" not in human
    assert Path(delivery["agent_paths"][0]).name == "general-preferences.md"


def test_update_cannot_accidentally_drop_existing_semantic_name(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    with pytest.raises(HubError) as caught:
        submit(store, snapshot, without_slug(make_markdown(title="更新标题")))
    assert caught.value.code == "INVALID_RECORD"
    assert "slug" in caught.value.message


def test_batch_filename_swap_keeps_both_records(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    for record_id, slug in (("doc-one", "first-topic"), ("doc-two", "second-topic")):
        publish(work, make_markdown(record_id, "deliverable", slug=slug, share_status="internal", audience=[]))
    current = store.snapshots.refresh()
    proposals = []
    for record_id, slug in (("doc-one", "second-topic"), ("doc-two", "first-topic")):
        record = current.records[record_id]
        proposal_id = "proposal-" + record_id
        atomic_json(store.config.proposal_dir / (proposal_id + ".json"), {
            "proposal_id": proposal_id, "trigger": "manual", "basis": "同批改名",
            "operation": "replace", "target_id": record_id, "base_commit": current.commit, "state": "pending",
            "proposed_markdown": dump_record({**record.metadata, "slug": slug}, record.body),
        })
        proposals.append(proposal_id)
    batch = service.approve(proposals, accept)
    assert batch["state"] == "synced"
    saved = store.snapshots.refresh()
    assert {"doc-one", "doc-two"} <= saved.records.keys()
    assert saved.records["doc-one"].relative_path.endswith("/second-topic.md")
    assert saved.records["doc-two"].relative_path.endswith("/first-topic.md")


def test_legacy_delivery_remains_verifiable_after_renderer_upgrade(tmp_path):
    from mcp_server.delivery import content_hash, load_bundle, render_review, review_digest, review_item
    from mcp_server.storage import atomic_json, atomic_text

    store, snapshot, work = setup_store(tmp_path)
    proposal = {"proposal_id": "proposal-legacy", "target_id": "proj-alpha", "operation": "replace",
                "base_commit": snapshot.commit, "basis": "原依据", "proposed_markdown": make_markdown(title="旧交付")}
    preview = {"base_commit": snapshot.commit, "remote_url": store.config.remote_url, "branch": store.config.branch,
               "items": [review_item(proposal, snapshot.records["proj-alpha"], snapshot.records["proj-alpha"], snapshot.commit)]}
    manifest = {"format_version": 1, "preview": preview, "review_digest": review_digest(preview)}
    delivery_id = "delivery-" + content_hash(manifest)
    root = store.config.proposal_dir / "deliveries" / delivery_id
    atomic_json(root / "manifest.json", manifest)
    atomic_text(root / "REVIEW.md", render_review(preview, 1))
    atomic_text(root / "agent" / "proj-alpha.md", proposal["proposed_markdown"])
    assert load_bundle(store.config, delivery_id) == manifest


def test_archive_and_restore_keep_semantic_filename(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    archived = submit(store, snapshot, make_markdown(status="archived"), operation="archive")
    service.approve_delivery(archived["data"]["delivery"]["delivery_id"], accept)
    current = store.snapshots.refresh()
    assert current.records["proj-alpha"].relative_path == "knowledge/L3_archive/project/proj-alpha.md"
    restored = submit(store, current, make_markdown())
    service.approve_delivery(restored["data"]["delivery"]["delivery_id"], accept)
    assert store.snapshots.refresh().records["proj-alpha"].relative_path == "knowledge/L1_projects/proj-alpha.md"


def test_blocked_filename_collision_still_exports_both_complete_drafts(tmp_path):
    from mcp_server.delivery import export_bundle, load_bundle, review_item

    store, snapshot, work = setup_store(tmp_path)
    items = []
    for record_id in ("doc-first", "doc-second"):
        proposal = {"proposal_id": "proposal-" + record_id, "target_id": record_id, "operation": "create",
                    "base_commit": snapshot.commit, "basis": "同名冲突待修正",
                    "proposed_markdown": make_markdown(record_id, "deliverable", slug="same-name", body=record_id)}
        items.append(review_item(proposal, None, None, snapshot.commit))
    delivery = export_bundle(store.config, {
        "base_commit": snapshot.commit, "items": items, "remote_url": store.config.remote_url,
        "branch": store.config.branch, "validation_errors": ["DUPLICATE_PATH"],
    })
    assert len(set(delivery["agent_paths"])) == 2
    assert {parse_record(Path(path).read_text()).body for path in delivery["agent_paths"]} == {"doc-first", "doc-second"}
    assert load_bundle(store.config, delivery["delivery_id"])["preview"]["validation_errors"]
