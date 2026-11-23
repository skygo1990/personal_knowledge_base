from concurrent.futures import ThreadPoolExecutor
import multiprocessing

import pytest

from mcp_server.models import HubError
from mcp_server.storage import read_json
from tests.git_helpers import git, publish
from tests.helpers import make_markdown
from tests.test_batch import accept, setup_review
from tests.test_proposals import submit


def approve_in_process(config, proposal_id, queue):
    from review.batch import ReviewService

    result = ReviewService(config).approve([proposal_id], accept)
    queue.put(result["batch_id"])


def test_push_response_lost_restart_reuses_commit(tmp_path, monkeypatch):
    from review.batch import ReviewService

    service, store, snapshot, work = setup_review(tmp_path)
    proposal_id = submit(store, snapshot, make_markdown(title="已接收"))["data"]["proposal_id"]
    original = service.repository.run

    def lose_response(*arguments, **kwargs):
        result = original(*arguments, **kwargs)
        if arguments[0] == "push":
            raise HubError("SYNC_FAILED", "响应丢失")
        return result

    monkeypatch.setattr(service.repository, "run", lose_response)
    pending = service.approve([proposal_id], accept)
    assert pending["state"] == "pending_sync"
    recovered = ReviewService(service.config).retry(pending["batch_id"])
    assert recovered["state"] == "synced"
    assert recovered["candidate"] == pending["candidate"]
    assert git(work, "--git-dir", str(service.repository.path), "rev-list", "--count", recovered["candidate"]) == "2"


def test_remote_advance_requires_second_confirmation(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    proposal_id = submit(store, snapshot, make_markdown(title="电脑B候选"))["data"]["proposal_id"]

    def concurrent_update(preview, phrase):
        publish(work, make_markdown(title="电脑A先完成"))
        return phrase

    pending = service.approve([proposal_id], concurrent_update)
    assert pending["state"] == "needs_review"
    assert store.snapshots.refresh().records["proj-alpha"].metadata["title"] == "电脑A先完成"
    assert service.retry(pending["batch_id"])["state"] == "needs_review"
    previews = []
    result = service.retry(pending["batch_id"], lambda preview, phrase: previews.append(preview) or phrase)
    assert result["state"] == "synced"
    assert "电脑A先完成" in previews[0]["items"][0]["current_markdown"]
    assert len(result["approvals"]) == 2
    assert result["approvals"][0]["approval_id"] != result["approvals"][1]["approval_id"]


def test_cancel_before_push_allows_fresh_batch(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    proposal_id = submit(store, snapshot, make_markdown(title="候选"))["data"]["proposal_id"]

    def concurrent_update(preview, phrase):
        publish(work, make_markdown(title="远端变化"))
        return phrase

    batch = service.approve([proposal_id], concurrent_update)
    service.cancel(batch["batch_id"], lambda value, phrase: phrase)
    with pytest.raises(HubError):
        service.retry(batch["batch_id"])
    result = service.approve([proposal_id], accept)
    assert result["state"] == "synced"
    assert result["batch_id"] != batch["batch_id"]


def test_unknown_push_cannot_cancel(tmp_path, monkeypatch):
    service, store, snapshot, work = setup_review(tmp_path)
    proposal_id = submit(store, snapshot, make_markdown(title="网络未知"))["data"]["proposal_id"]
    original = service.repository.run

    def refuse_push(*arguments, **kwargs):
        if arguments[0] == "push":
            raise HubError("SYNC_FAILED", "断网")
        return original(*arguments, **kwargs)

    monkeypatch.setattr(service.repository, "run", refuse_push)
    result = service.approve([proposal_id], accept)
    with pytest.raises(HubError, match="未知"):
        service.cancel(result["batch_id"], lambda value, phrase: phrase)
    with pytest.raises(HubError):
        service.reject(proposal_id, lambda paths, phrase: phrase)


def test_crash_after_commit_before_push_recovers(tmp_path, monkeypatch):
    from review.batch import ReviewService

    service, store, snapshot, work = setup_review(tmp_path)
    proposal_id = submit(store, snapshot, make_markdown(title="恢复批准"))["data"]["proposal_id"]
    original = service._save

    def crash_when_committed(journal):
        original(journal)
        if any(batch["state"] == "committed" for batch in journal["batches"].values()):
            raise RuntimeError("模拟进程退出")

    monkeypatch.setattr(service, "_save", crash_when_committed)
    with pytest.raises(RuntimeError):
        service.approve([proposal_id], accept)
    batch = next(iter(read_json(service.journal_path)["batches"].values()))
    assert batch["state"] == "committed" and not batch["push_attempted"]
    result = ReviewService(service.config).retry(batch["batch_id"])
    assert result["state"] == "synced" and result["candidate"] == batch["candidate"]


def test_projection_failure_does_not_lose_authoritative_approval(tmp_path, monkeypatch):
    from review.batch import ReviewService

    service, store, snapshot, work = setup_review(tmp_path)
    proposal_id = submit(store, snapshot, make_markdown(title="批准存活"))["data"]["proposal_id"]
    original = service._project_status

    def fail_projection(journal):
        if journal["batches"]:
            raise OSError("模拟投影写入失败")
        return original(journal)

    monkeypatch.setattr(service, "_project_status", fail_projection)
    with pytest.raises(OSError):
        service.approve([proposal_id], accept)
    restarted = ReviewService(service.config)
    batches = restarted.status()
    assert len(batches) == 1
    assert read_json(store.config.review_status_dir / (proposal_id + ".json"))["state"] == "approved"
    assert restarted.retry(batches[0]["batch_id"])["state"] == "synced"


def test_concurrent_approvals_share_one_batch(tmp_path):
    from review.batch import ReviewService

    service, store, snapshot, work = setup_review(tmp_path)
    proposal_id = submit(store, snapshot, make_markdown(title="并发确认"))["data"]["proposal_id"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: ReviewService(service.config).approve([proposal_id], accept), range(2)))
    assert results[0]["batch_id"] == results[1]["batch_id"]
    assert len(service.status()) == 1


def test_previous_content_can_be_proposed_again_after_later_change(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    first = submit(store, snapshot, make_markdown(title="状态A"))["data"]["proposal_id"]
    service.approve([first], accept)
    second = submit(store, store.snapshots.refresh(), make_markdown(title="状态B"))["data"]["proposal_id"]
    service.approve([second], accept)
    third = submit(store, store.snapshots.refresh(), make_markdown(title="状态A"))
    assert third["data"]["result"] == "created"
    assert third["data"]["proposal_id"] != first
    service.approve([third["data"]["proposal_id"]], accept)
    assert store.snapshots.refresh().records["proj-alpha"].metadata["title"] == "状态A"


def test_cancelled_proposal_deduplicates_and_lists_as_pending(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    markdown = make_markdown(title="待重新审核")
    proposal_id = submit(store, snapshot, markdown)["data"]["proposal_id"]

    def concurrent_update(preview, phrase):
        publish(work, make_markdown(title="新基线"))
        return phrase

    batch = service.approve([proposal_id], concurrent_update)
    service.cancel(batch["batch_id"], lambda value, phrase: phrase)
    duplicate = submit(store, store.snapshots.refresh(), markdown)
    assert duplicate["data"]["proposal_id"] == proposal_id
    assert service.list()[0]["state"] == "pending"


def test_superseded_state_and_new_record_can_be_approved_together(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    publish(work, make_markdown("lesson-old", "lesson"))
    snapshot = store.snapshots.refresh()
    old = submit(store, snapshot, make_markdown("lesson-old", "lesson", status="superseded"),
                 operation="archive", target="lesson-old")["data"]["proposal_id"]
    new = submit(store, snapshot, make_markdown("lesson-new", "lesson", supersedes=["lesson-old"]),
                 operation="create", target="lesson-new")["data"]["proposal_id"]
    with pytest.raises(HubError):
        service.approve([new], accept)
    service.approve([old, new], accept)
    records = store.snapshots.refresh().records
    assert records["lesson-old"].status == "superseded"
    assert records["lesson-new"].metadata["supersedes"] == ["lesson-old"]


def test_duplicate_batch_in_remote_history_stops_new_writes(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    proposal_id = submit(store, snapshot, make_markdown(title="已同步批次"))["data"]["proposal_id"]
    result = service.approve([proposal_id], accept)
    current = store.snapshots.refresh()
    git(work, "fetch", "origin", "shared")
    git(work, "merge", "--ff-only", "FETCH_HEAD")
    git(work, "commit", "--allow-empty", "-m", "docs: 重复标识\n\nBatch-ID: " + result["batch_id"])
    git(work, "push", "origin", "HEAD:refs/heads/shared")
    fresh = store.snapshots.refresh()
    assert fresh.sync_status == "stale"
    assert fresh.commit == current.commit
    next_id = submit(store, current, make_markdown(title="不能继续写"))["data"]["proposal_id"]
    with pytest.raises(HubError):
        service.approve([next_id], accept)


def test_two_processes_cannot_apply_one_proposal_twice(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    proposal_id = submit(store, snapshot, make_markdown(title="两进程审核"))["data"]["proposal_id"]
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [context.Process(target=approve_in_process, args=(service.config, proposal_id, queue)) for number in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert queue.get(timeout=2) == queue.get(timeout=2)
    queue.close()
    assert len(service.status()) == 1
