import json
from pathlib import Path

import pytest

from mcp_server.models import HubError
from mcp_server.records import parse_record
from mcp_server.storage import atomic_json, read_json
from tests.git_helpers import git, publish
from tests.helpers import make_markdown
from tests.test_batch import accept, setup_review
from tests.test_proposals import setup_store, submit


def bundle_for(store, snapshot, proposal_ids):
    result = store.prepare_review_bundle(proposal_ids, snapshot.commit)
    assert result["ok"] is True
    return result["data"]


def test_proposal_automatically_exports_both_views_and_deduplicates(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    markdown = make_markdown(title="通用个人偏好")
    first = submit(store, snapshot, markdown)
    delivery = first["data"]["delivery"]
    assert Path(delivery["human_path"]).name == "REVIEW.md"
    assert Path(delivery["agent_paths"][0]).read_text() == markdown
    human = Path(delivery["human_path"]).read_text()
    assert human.startswith("# 知识库入库审核")
    assert "通用个人偏好" in human and "本次变更" in human
    assert "完整拟保存正文" in human and "公开仓库" in human
    assert "本机已核实" in human and "适用范围" in human
    assert "确认前不会提交或推送" in human
    assert delivery["validation_passed"] is True
    second = submit(store, snapshot, markdown)
    assert second["data"]["delivery"]["delivery_id"] == delivery["delivery_id"]
    assert store.snapshots.refresh().commit == snapshot.commit


def test_four_record_task_combines_dependencies_without_new_proposals(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    source = "# 全局偏好\n\n" + "完整原文，不截断。\n" * 100
    proposal_ids = []
    markdowns = [make_markdown("doc-preferences", "deliverable", body=source, share_status="internal", audience=[])]
    markdowns.extend(make_markdown("core-" + view, "core", title=view + "偏好", views=[view],
                                  related_ids=["doc-preferences"], body="称呼主人，先结论。")
                     for view in ("base", "coding", "thinking"))
    for markdown in markdowns:
        record = parse_record(markdown)
        proposal_ids.append(submit(store, snapshot, markdown, operation="create", target=record.id)["data"]["proposal_id"])
    pending_before = sorted(store.config.proposal_dir.glob("*.json"))
    delivery = bundle_for(store, snapshot, proposal_ids)
    human = Path(delivery["human_path"]).read_text()
    assert source in human
    assert "4 条记录" in human
    assert delivery["validation_passed"] is True
    assert len(delivery["agent_paths"]) == 4
    assert {Path(path).read_text() for path in delivery["agent_paths"]} == set(markdowns)
    assert pending_before == sorted(store.config.proposal_dir.glob("*.json"))
    assert not list(store.config.review_status_dir.glob("*.json"))
    assert bundle_for(store, snapshot, list(reversed(proposal_ids)))["delivery_id"] == delivery["delivery_id"]


def test_incomplete_batch_is_readable_but_cannot_be_approved(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    markdown = make_markdown("core-dependent", "core", related_ids=["doc-missing"])
    result = submit(store, snapshot, markdown, operation="create", target="core-dependent")
    delivery = result["data"]["delivery"]
    assert delivery["validation_passed"] is False
    assert "doc-missing" in Path(delivery["human_path"]).read_text()
    with pytest.raises(HubError) as caught:
        service.approve_delivery(delivery["delivery_id"], accept)
    assert caught.value.code == "INVALID_REFERENCE"
    assert store.snapshots.refresh().commit == snapshot.commit


def test_combined_budget_failure_is_visible_without_truncating(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    proposals = [submit(store, snapshot, make_markdown(record_id, "core", body="偏" * 210),
                        operation="create", target=record_id)["data"]["proposal_id"]
                 for record_id in ("core-one", "core-two")]
    delivery = bundle_for(store, snapshot, proposals)
    assert delivery["validation_passed"] is False
    human = Path(delivery["human_path"]).read_text()
    assert "BUDGET_EXCEEDED" in human and "偏" * 210 in human


def test_delivery_approval_binds_candidate_and_still_requires_confirmation(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    result = submit(store, snapshot, make_markdown(title="新个人偏好"))
    delivery = result["data"]["delivery"]
    with pytest.raises(HubError) as caught:
        service.approve_delivery(delivery["delivery_id"], lambda preview, phrase: "yes")
    assert caught.value.code == "UNAUTHORIZED"
    assert store.snapshots.refresh().commit == snapshot.commit
    approved = service.approve_delivery(delivery["delivery_id"], accept)
    assert approved["state"] == "synced"
    assert store.snapshots.refresh().records["proj-alpha"].metadata["title"] == "新个人偏好"


@pytest.mark.parametrize("changed", ["source", "reviewer", "remote", "human", "agent", "manifest", "permission"])
def test_stale_or_tampered_delivery_never_reaches_confirmation(tmp_path, changed):
    service, store, snapshot, work = setup_review(tmp_path)
    markdown = make_markdown("doc-ready", "deliverable") if changed == "permission" else make_markdown(title="原候选")
    result = submit(store, snapshot, markdown, operation="create" if changed == "permission" else "replace",
                    target="doc-ready" if changed == "permission" else "proj-alpha")
    proposal_id = result["data"]["proposal_id"]
    delivery = result["data"]["delivery"]
    if changed == "source":
        path = store.config.proposal_dir / (proposal_id + ".json")
        proposal = read_json(path)
        proposal["proposed_markdown"] = make_markdown(title="被换的候选")
        atomic_json(path, proposal)
    elif changed == "reviewer":
        service.edit(proposal_id, make_markdown(title="本人改稿"))
    elif changed == "remote":
        publish(work, make_markdown("proj-new"))
    elif changed in {"human", "agent"}:
        path = Path(delivery["human_path"] if changed == "human" else delivery["agent_paths"][0])
        path.write_text("被改过的内容")
    elif changed == "manifest":
        path = Path(delivery["human_path"]).parent / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["preview"]["items"][0]["basis"] = "被改过的依据"
        path.write_text(json.dumps(manifest))
    before = git(work, "ls-remote", str(service.config.remote_url), "refs/heads/shared")
    confirmations = []
    with pytest.raises(HubError) as caught:
        service.approve_delivery(delivery["delivery_id"], lambda preview, phrase: confirmations.append(preview) or phrase)
    assert caught.value.code == "REVIEW_CHANGED"
    assert not confirmations
    assert git(work, "ls-remote", str(service.config.remote_url), "refs/heads/shared") == before


def test_cli_delivery_preview_uses_reviewer_private_edit(tmp_path, monkeypatch, capsys):
    import review.cli as cli

    service, store, snapshot, work = setup_review(tmp_path)
    result = submit(store, snapshot, make_markdown(title="初始标题"))
    service.edit(result["data"]["proposal_id"], make_markdown(title="本人修订后标题"))
    monkeypatch.setattr(cli, "load_config", lambda *args: service.config)
    monkeypatch.setattr(cli, "validate_runtime", lambda config: None)
    monkeypatch.setattr(cli, "ReviewService", lambda config: service)
    assert cli.main(["--config", "unused", "preview", "--delivery", result["data"]["delivery"]["delivery_id"]]) == 0
    output = capsys.readouterr().out
    assert "本人修订后标题" in output and "REVIEW.md" in output
    assert "delivery-" in output


def test_hostile_markdown_stays_literal_and_symlinks_are_rejected(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    body = "# 原文\n```\n<script>alert(1)</script>\n```\n" + "尾" * 1000
    result = submit(store, snapshot, make_markdown("doc-html", "deliverable", body=body),
                    operation="create", target="doc-html", basis="依据\x1b[2J\u202e风险")
    delivery = result["data"]["delivery"]
    human = Path(delivery["human_path"]).read_text()
    assert "````markdown\n" + body in human
    assert "\x1b" not in human and "\u202e" not in human
    assert "U+001B" in human
    destination = Path(delivery["human_path"])
    destination.unlink()
    destination.symlink_to(tmp_path / "outside")
    with pytest.raises(HubError) as caught:
        bundle_for(store, snapshot, [result["data"]["proposal_id"]])
    assert caught.value.code == "INVALID_PATH"
    assert not (tmp_path / "outside").exists()


def test_reviewer_export_does_not_expose_private_edits_to_reader(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    result = submit(store, snapshot, make_markdown(title="共享初稿"))
    proposal_id = result["data"]["proposal_id"]
    service.edit(proposal_id, make_markdown(title="本人私有修订"))
    preview = service.preview([proposal_id])
    delivery = preview["delivery"]
    human_path = Path(delivery["human_path"])
    assert human_path.is_relative_to(service.private)
    assert human_path.stat().st_mode & 0o777 == 0o600
    assert human_path.parent.stat().st_mode & 0o777 == 0o700
    assert "本人私有修订" not in Path(result["data"]["delivery"]["human_path"]).read_text()
    assert service.approve_delivery(delivery["delivery_id"], accept)["state"] == "synced"


@pytest.mark.parametrize("proposal_ids", [[], ["../escape"], [1], "proposal-any", ["proposal-any", "proposal-any"]])
def test_bundle_rejects_invalid_selection(tmp_path, proposal_ids):
    store, snapshot, work = setup_store(tmp_path)
    with pytest.raises(HubError) as caught:
        store.prepare_review_bundle(proposal_ids, snapshot.commit)
    assert caught.value.code == "INVALID_ARGUMENT"


def test_bundle_requires_explicit_dependencies_and_rejects_duplicate_target(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    first = submit(store, snapshot, make_markdown(title="甲"))["data"]["proposal_id"]
    second = submit(store, snapshot, make_markdown(title="乙"))["data"]["proposal_id"]
    with pytest.raises(HubError) as caught:
        store.prepare_review_bundle([first, second], snapshot.commit)
    assert caught.value.code == "INVALID_RECORD"


def test_failed_export_keeps_saved_proposal_and_supports_retry(tmp_path, monkeypatch):
    import mcp_server.proposals as proposals

    store, snapshot, work = setup_store(tmp_path)
    original_export = proposals.export_bundle

    def unavailable(*args):
        raise OSError("private detail not echoed")

    monkeypatch.setattr(proposals, "export_bundle", unavailable)
    result = submit(store, snapshot, make_markdown(title="已保存但未导出"))
    assert result["ok"] is True
    assert "delivery" not in result["data"]
    assert "prepare_review_bundle" in result["warnings"][-1]
    assert "private detail" not in str(result)
    monkeypatch.setattr(proposals, "export_bundle", original_export)
    delivery = bundle_for(store, snapshot, [result["data"]["proposal_id"]])
    assert Path(delivery["human_path"]).exists()
