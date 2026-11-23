import pytest

from mcp_server.models import HubError
from mcp_server.storage import read_json
from tests.git_helpers import config_for, remote_fixture
from tests.helpers import make_markdown


def setup_store(tmp_path):
    from mcp_server.proposals import ProposalStore
    from review.sync import SnapshotStore

    remote, work = remote_fixture(tmp_path)
    config = config_for(tmp_path, remote)
    snapshot = SnapshotStore(config).refresh()
    return ProposalStore(config, SnapshotStore(config)), snapshot, work


def submit(store, snapshot, markdown, operation="replace", target="proj-alpha", **changes):
    values = dict(trigger="manual", basis="本机已核实", operation=operation, target_id=target,
                  base_commit=snapshot.commit, proposed_markdown=markdown)
    values.update(changes)
    return store.propose(**values)


def test_no_change_and_pending_dedup(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    assert submit(store, snapshot, make_markdown())["data"]["result"] == "no_change"
    first = submit(store, snapshot, make_markdown(title="新标题"))
    second = submit(store, snapshot, make_markdown(title="新标题", updated_at="2026-11-21T09:00:00+08:00"))
    assert first["data"]["proposal_id"] == second["data"]["proposal_id"]
    assert len(list(store.config.proposal_dir.glob("*.json"))) == 1
    assert snapshot.records["proj-alpha"].metadata["title"] == "测试记录"


def test_unresolved_reference_allowed_only_as_warning(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    result = submit(store, snapshot, make_markdown("doc-new", "deliverable", scope=["proj-new"]),
                    operation="create", target="doc-new")
    assert result["ok"] is True
    assert result["data"]["unresolved_ids"] == ["proj-new"]
    assert result["warnings"]


@pytest.mark.parametrize("changes", [
    {"target_id": "../escape"}, {"trigger": "shell"}, {"operation": "delete"},
    {"target_id": "proj-other"}, {"operation": "create"}, {"base_commit": "../escape"},
])
def test_invalid_proposal_never_persisted(tmp_path, changes):
    store, snapshot, work = setup_store(tmp_path)
    with pytest.raises(HubError):
        submit(store, snapshot, make_markdown(title="changed"), **changes)
    assert not list(store.config.proposal_dir.glob("*.json"))


def test_budget_error_returns_full_preview(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    markdown = make_markdown("core-long", "core", body="长" * 401)
    result = submit(store, snapshot, markdown, operation="create", target="core-long")
    assert result["error"]["code"] == "BUDGET_EXCEEDED"
    assert result["preview"] == markdown
    assert not list(store.config.proposal_dir.glob("*.json"))


def test_proposal_basis_remains_local(tmp_path):
    store, snapshot, work = setup_store(tmp_path)
    result = submit(store, snapshot, make_markdown(title="新标题"), basis="本机路径及依据")
    proposal = read_json(store.config.proposal_dir / (result["data"]["proposal_id"] + ".json"))
    assert proposal["basis"] == "本机路径及依据"
    assert "本机路径及依据" not in proposal["proposed_markdown"]


def test_malformed_source_is_structured_failure(tmp_path):
    from mcp_server.storage import atomic_json, ensure_directory

    store, snapshot, work = setup_store(tmp_path)
    ensure_directory(store.config.proposal_dir)
    atomic_json(store.config.proposal_dir / "bad-file.json", [])
    result = submit(store, snapshot, make_markdown(title="正常候选"))
    assert result["data"]["result"] == "created"
    assert result["warnings"]


def test_malformed_base_does_not_occupy_dedup(tmp_path):
    from mcp_server.storage import atomic_json

    store, snapshot, work = setup_store(tmp_path)
    markdown = make_markdown(title="候选")
    first = submit(store, snapshot, markdown)["data"]["proposal_id"]
    path = store.config.proposal_dir / (first + ".json")
    broken = read_json(path)
    broken["base_commit"] = "invalid-sha"
    atomic_json(path, broken)
    result = submit(store, snapshot, markdown)
    assert result["data"]["proposal_id"] != first
    assert result["warnings"]
