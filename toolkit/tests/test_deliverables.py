from mcp_server.context import ContextService
from mcp_server.models import HubError
from tests.git_helpers import publish
from tests.helpers import make_markdown
from tests.test_batch import accept, setup_review
from tests.test_proposals import submit
import pytest


@pytest.mark.parametrize("mode", ["omit", "rewrite"])
def test_overwrite_cannot_erase_or_rewrite_publication_history(tmp_path, mode):
    service, store, snapshot, work = setup_review(tmp_path)
    event = {"id": "event-old", "published_at": "2026-11-20T12:00:00+08:00", "channel": "既有渠道",
             "audience": ["parents"], "result": "本人确认已送达"}
    publish(work, make_markdown("doc-history", "deliverable", publication_events=[event]))
    snapshot = store.snapshots.refresh()
    events = [] if mode == "omit" else [{**event, "result": "改写历史"}]
    with pytest.raises(HubError) as caught:
        submit(store, snapshot, make_markdown("doc-history", "deliverable", body="新的正文", publication_events=events), target="doc-history")
    assert caught.value.code == "INVALID_RECORD"


def test_overwrite_can_preserve_and_append_publication_events(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    event = {"id": "event-old", "published_at": "2026-11-20T12:00:00+08:00", "channel": "既有渠道",
             "audience": ["parents"], "result": "本人确认已送达"}
    publish(work, make_markdown("doc-history", "deliverable", publication_events=[event]))
    snapshot = store.snapshots.refresh()
    events = [event, {**event, "id": "event-new", "published_at": "2026-11-21T12:00:00+08:00", "audience": ["parents"]}]
    result = submit(store, snapshot, make_markdown("doc-history", "deliverable", body="新的正文", publication_events=events), target="doc-history")
    service.approve([result["data"]["proposal_id"]], accept, lambda record: True)
    assert store.snapshots.refresh().records["doc-history"].metadata["publication_events"] == events


def test_full_markdown_export_overwrite_and_publication_facts(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    body = "# 家长指南\n\n" + "可直接使用的正文。" * 120
    proposed = make_markdown("doc-parents", "deliverable", body=body, evidence_summary="仅内部依据")
    proposal_id = submit(store, snapshot, proposed, operation="create", target="doc-parents")["data"]["proposal_id"]
    service.approve([proposal_id], accept, lambda record: True)
    current = store.snapshots.refresh()
    result = ContextService(current).find_deliverables(record_id="doc-parents", audience="parents")["data"]["items"][0]
    assert result["body"] == body
    exported = tmp_path / "家长指南.md"
    exported.write_text(result["body"], encoding="utf-8")
    assert "仅内部依据" not in exported.read_text()
    assert result["publication"]["events"] == []
    assert result["publication"]["current_body_published"] is None
    event = {"id": "event-first", "published_at": "2026-11-20T12:00:00+08:00", "channel": "用户已有渠道",
             "audience": ["parents"], "result": "本人确认已送达", "source_commit": current.commit}
    updated = make_markdown("doc-parents", "deliverable", body="# 更新说明\n新的完整正文", publication_events=[event])
    update_id = submit(store, current, updated, target="doc-parents")["data"]["proposal_id"]
    service.approve([update_id], accept)
    latest = store.snapshots.refresh()
    assert ContextService(latest).find_deliverables()["data"]["items"] == []
    exact = ContextService(latest).find_deliverables(record_id="doc-parents")["data"]["items"][0]
    assert exact["permission"]["can_share"] is False
    assert exact["body"] == "# 更新说明\n新的完整正文"
    assert exact["publication"]["events"][0]["source_commit"] == current.commit
    assert exact["publication"]["current_body_published"] is None
    assert current.records["doc-parents"].relative_path == latest.records["doc-parents"].relative_path
