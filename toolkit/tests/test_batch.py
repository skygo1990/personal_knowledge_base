import pytest

from mcp_server.models import HubError
from mcp_server.records import parse_record
from mcp_server.storage import atomic_json, read_json
from tests.git_helpers import config_for, git, remote_fixture
from tests.helpers import make_markdown
from tests.test_proposals import submit


def setup_review(tmp_path):
    from mcp_server.proposals import ProposalStore
    from review.batch import ReviewService
    from review.sync import SnapshotStore

    remote, work = remote_fixture(tmp_path)
    reader = config_for(tmp_path, remote)
    reviewer = config_for(tmp_path, remote, "reviewer")
    store = SnapshotStore(reader)
    snapshot = store.refresh()
    return ReviewService(reviewer), ProposalStore(reader, store), snapshot, work


def accept(preview, phrase):
    assert preview["items"]
    assert phrase.startswith("确认 ")
    return phrase


def test_approved_update_reaches_remote_once(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    proposed = submit(store, snapshot, make_markdown(title="审核标题"))["data"]["proposal_id"]
    result = service.approve([proposed], accept)
    assert result["state"] == "synced"
    assert service.approve([proposed], accept)["batch_id"] == result["batch_id"]
    refreshed = store.snapshots.refresh()
    assert refreshed.records["proj-alpha"].metadata["title"] == "审核标题"
    assert git(work, "--git-dir", str(service.repository.path), "rev-list", "--count", result["candidate"]) == "2"


def test_wrong_confirmation_does_not_commit(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    proposed = submit(store, snapshot, make_markdown(title="不能生效"))["data"]["proposal_id"]
    with pytest.raises(HubError):
        service.approve([proposed], lambda preview, phrase: "yes")
    assert store.snapshots.refresh().commit == snapshot.commit


def test_imported_copy_cannot_be_swapped_by_agent(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    proposed = submit(store, snapshot, make_markdown(title="本人看到的"))["data"]["proposal_id"]
    service.preview([proposed])
    path = store.config.proposal_dir / (proposed + ".json")
    source = read_json(path)
    source["proposed_markdown"] = make_markdown(title="后来偷换的")
    atomic_json(path, source)
    service.approve([proposed], accept)
    assert store.snapshots.refresh().records["proj-alpha"].metadata["title"] == "本人看到的"


def test_partial_selection_and_unresolved_dependencies(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    doc = submit(store, snapshot, make_markdown("doc-guide", "deliverable", scope=["proj-new"]),
                 operation="create", target="doc-guide")["data"]["proposal_id"]
    project = submit(store, snapshot, make_markdown("proj-new"), operation="create", target="proj-new")["data"]["proposal_id"]
    with pytest.raises(HubError) as caught:
        service.approve([doc], accept)
    assert caught.value.code == "INVALID_REFERENCE"
    service.approve([doc, project], accept, share_confirm=lambda record: True)
    records = store.snapshots.refresh().records
    assert records["doc-guide"].metadata["share_status"] == "ready"
    assert "proj-new" in records


def test_ready_without_explicit_permission_becomes_internal(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    doc = submit(store, snapshot, make_markdown("doc-guide", "deliverable"), operation="create", target="doc-guide")["data"]["proposal_id"]
    seen = []
    service.approve([doc], lambda preview, phrase: seen.append(preview) or phrase)
    record = store.snapshots.refresh().records["doc-guide"]
    assert record.metadata["share_status"] == "internal"
    assert "share_status: internal" in seen[0]["items"][0]["proposed_markdown"]


def test_archive_restore_removes_old_path(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    archive = submit(store, snapshot, make_markdown(status="archived"), operation="archive")["data"]["proposal_id"]
    service.approve([archive], accept)
    archived = store.snapshots.refresh()
    assert "L3_archive" in archived.records["proj-alpha"].relative_path
    restore = submit(store, archived, make_markdown(title="恢复"))["data"]["proposal_id"]
    service.approve([restore], accept)
    current = store.snapshots.refresh()
    assert list(current.records) == ["proj-alpha"]
    assert "L1_projects" in current.records["proj-alpha"].relative_path


def test_reject_deletes_both_exact_copies(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    proposed = submit(store, snapshot, make_markdown(title="拒绝"))["data"]["proposal_id"]
    service.preview([proposed])
    seen = []
    service.reject(proposed, lambda paths, phrase: seen.extend(paths) or phrase)
    assert len(seen) == 2
    assert all(not path.exists() for path in seen)
    assert read_json(store.config.review_status_dir / (proposed + ".json"))["state"] == "rejected"


def test_only_selected_files_change(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    selected = submit(store, snapshot, make_markdown(title="项目更新"))["data"]["proposal_id"]
    ignored = submit(store, snapshot, make_markdown("lesson-hidden", "lesson"), operation="create", target="lesson-hidden")["data"]["proposal_id"]
    service.approve([selected], accept)
    assert "lesson-hidden" not in store.snapshots.refresh().records
    assert read_json(store.config.proposal_dir / (ignored + ".json"))["state"] == "pending"
