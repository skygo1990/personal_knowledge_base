from datetime import datetime

from mcp_server.context import ContextService
from review.sync import SnapshotStore
from tests.git_helpers import config_for
from tests.helpers import PROJECT_BODY, make_markdown
from tests.test_batch import accept, setup_review
from tests.test_proposals import submit


def test_two_local_devices_share_only_confirmed_facts_and_full_documents(tmp_path):
    service, store, initial, work = setup_review(tmp_path)
    second = SnapshotStore(config_for(tmp_path, service.config.remote_url, device="device-b"))
    old_second = second.refresh()
    project_markdown = make_markdown(title="第四集已完成", body=PROJECT_BODY + "已验证事实" * 200,
        related_ids=["doc-four"], constraints=[
            {"id": "weekly", "key": "minutes", "kind": "baseline", "value": 180},
            {"id": "busy-week", "key": "minutes", "kind": "exception", "value": 60, "overrides": "weekly",
             "valid_from": "2026-11-20T00:00:00+08:00", "valid_until": "2026-11-27T00:00:00+08:00"}])
    project = submit(store, initial, project_markdown)["data"]["proposal_id"]
    document = submit(store, initial, make_markdown("doc-four", "deliverable", body="# 第四集说明\n请家长陪同完成实验。", scope=["proj-alpha"]),
                      operation="create", target="doc-four")["data"]["proposal_id"]
    lesson = submit(store, initial, make_markdown("lesson-private", "lesson", title="本地未批准经验"),
                    operation="create", target="lesson-private")["data"]["proposal_id"]
    assert second.refresh().commit == old_second.commit
    service.reject(lesson, lambda paths, phrase: phrase)
    service.approve([project, document], accept, lambda record: True)
    assert second.get().commit == initial.commit
    updated = second.refresh()
    first = store.snapshots.refresh()
    assert first.commit == updated.commit
    assert first.records.keys() == updated.records.keys()
    assert "lesson-private" not in updated.records
    during = ContextService(updated, now=datetime.fromisoformat("2026-11-26T12:00:00+08:00")).get_project_context("proj-alpha")
    after = ContextService(updated, now=datetime.fromisoformat("2026-11-27T00:00:00+08:00")).get_project_context("proj-alpha")
    assert during["data"]["effective_constraints"][0]["value"] == 60
    assert after["data"]["effective_constraints"][0]["value"] == 180
    assert len(after["data"]["record"]["body"]) > 500
    assert after["data"]["record"]["source"]["snapshot_commit"] == updated.commit
    doc = ContextService(updated).find_deliverables(record_id="doc-four", audience="parents")["data"]["items"][0]
    assert doc["permission"]["can_share"]
    assert doc["body"] == "# 第四集说明\n请家长陪同完成实验。"
    assert "doc-four" not in second.get(initial.commit).records
