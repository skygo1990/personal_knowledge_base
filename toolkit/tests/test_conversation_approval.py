import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_server.config import load_config
from mcp_server.models import HubError
from mcp_server.storage import atomic_json, read_json
from tests.git_helpers import git, publish
from tests.helpers import make_markdown
from tests.test_batch import setup_review
from tests.test_proposals import submit


CONFIRMATION = "确认将此交付入库并推送至配置仓库"


def setup_conversation(tmp_path):
    service, store, snapshot, work = setup_review(tmp_path)
    reviewer = replace(service.config, security_mode="single_user")
    for directory in (store.config.data_root, reviewer.data_root, reviewer.proposal_dir, reviewer.review_status_dir):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    reviewer_path = tmp_path / "reviewer.toml"
    values = {"role": "reviewer", "security_mode": "single_user", "device_id": reviewer.device_id,
              "remote_url": reviewer.remote_url, "branch": reviewer.branch, "data_root": str(reviewer.data_root),
              "proposal_dir": str(reviewer.proposal_dir), "review_status_dir": str(reviewer.review_status_dir)}
    reviewer_path.write_text("\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items()))
    reader_path = tmp_path / "reader.toml"
    values.update(role="reader", data_root=str(store.config.data_root), transport="stdio")
    reader_path.write_text("\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items())
                           + '\nallow_conversation_approval = true\nreviewer_config = "reviewer.toml"\n')
    return reader_path, reviewer_path, store, snapshot, work


def approval_request(result):
    delivery = result["data"]["delivery"]
    return {"delivery_id": delivery["delivery_id"], "expected_review_digest": delivery["review_digest"],
            "confirmation": CONFIRMATION}


def test_configuration_requires_explicit_opt_in_and_matching_local_transport(tmp_path):
    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    config = load_config(path, "reader")
    assert config.allow_conversation_approval is True
    assert config.reviewer_config == reviewer
    assert store.config.allow_conversation_approval is False


@pytest.mark.parametrize("change", ["http", "isolated", "missing-reviewer", "string-flag", "reviewer-only"])
def test_invalid_approval_configuration_fails_closed(tmp_path, change):
    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    text = path.read_text()
    if change == "http":
        text = text.replace('transport = "stdio"', 'transport = "http"\ntoken_file = "access-token"')
    elif change == "isolated":
        text = text.replace('security_mode = "single_user"', 'security_mode = "isolated"')
    elif change == "missing-reviewer":
        text = text.replace('reviewer_config = "reviewer.toml"', "")
    elif change == "string-flag":
        text = text.replace("allow_conversation_approval = true", 'allow_conversation_approval = "true"')
    else:
        text = text.replace("allow_conversation_approval = true", "allow_conversation_approval = false")
    path.write_text(text)
    with pytest.raises(HubError) as caught:
        load_config(path, "reader")
    assert caught.value.code == "INVALID_CONFIG"


def test_default_server_has_no_approval_tools(tmp_path):
    from mcp_server.server import create_mcp

    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    tools = asyncio.run(create_mcp(store.config).list_tools())
    assert not {tool.name for tool in tools}.intersection({"approve_review_bundle", "get_review_status", "retry_review_sync"})


@pytest.mark.parametrize("field,value", [("branch", "other"), ("device_id", "other-device"),
                                        ("security_mode", "isolated"), ("remote_url", "/unrelated.git"),
                                        ("data_root", "SAME_AS_READER")])
def test_pair_mismatch_rejected_before_tool_registration(tmp_path, field, value):
    from mcp_server.server import create_mcp

    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    import tomllib

    values = tomllib.loads(reviewer.read_text())
    values[field] = str(store.config.data_root) if value == "SAME_AS_READER" else value
    reviewer.write_text("\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items()))
    with pytest.raises(HubError):
        create_mcp(load_config(path, "reader"))


def test_explicit_conversation_approval_is_audited_and_idempotent(tmp_path):
    from mcp_server.approvals import ConversationReview

    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    gateway = ConversationReview(load_config(path, "reader"))
    result = submit(store, snapshot, make_markdown(title="对话确认的新标题"))
    request = approval_request(result)
    approved = gateway.approve(**request)
    assert approved["ok"] and approved["data"]["state"] == "synced"
    assert "candidate" in approved["data"] and "confirmation" not in approved["data"]
    assert gateway.approve(**request)["data"]["batch_id"] == approved["data"]["batch_id"]
    journal = read_json(gateway.service.journal_path)
    authorization = journal["batches"][approved["data"]["batch_id"]]["approvals"][0]["authorization"]
    assert authorization == {"method": "conversation", **request, "allow_external_sharing": False}
    assert journal["batches"][approved["data"]["batch_id"]]["approvals"][0]["confirmation"] == CONFIRMATION
    assert store.snapshots.refresh().records["proj-alpha"].metadata["title"] == "对话确认的新标题"
    assert git(work, "--git-dir", str(gateway.service.repository.path), "rev-list", "--count", approved["data"]["candidate"]) == "2"
    assert gateway.status()["data"][0]["state"] == "synced"


@pytest.mark.parametrize("invalid", ["confirmation", "digest", "source", "remote", "human", "permission"])
def test_mismatched_or_missing_consent_never_pushes(tmp_path, invalid):
    from mcp_server.approvals import ConversationReview

    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    gateway = ConversationReview(load_config(path, "reader"))
    markdown = make_markdown("doc-ready", "deliverable") if invalid == "permission" else make_markdown(title="待确认")
    result = submit(store, snapshot, markdown, operation="create" if invalid == "permission" else "replace",
                    target="doc-ready" if invalid == "permission" else "proj-alpha")
    request = approval_request(result)
    if invalid == "confirmation":
        request["confirmation"] = "yes"
    elif invalid == "digest":
        request["expected_review_digest"] = "0" * 64
    elif invalid == "source":
        source = store.config.proposal_dir / (result["data"]["proposal_id"] + ".json")
        proposal = read_json(source)
        proposal["proposed_markdown"] = make_markdown(title="后来被改动")
        atomic_json(source, proposal)
    elif invalid == "remote":
        publish(work, make_markdown("proj-other"))
    elif invalid == "human":
        Path(result["data"]["delivery"]["human_path"]).write_text("被修改的审核文件")
    before = git(work, "ls-remote", str(store.config.remote_url), "refs/heads/shared")
    with pytest.raises(HubError):
        gateway.approve(**request)
    assert git(work, "ls-remote", str(store.config.remote_url), "refs/heads/shared") == before
    assert not read_json(gateway.service.journal_path, {}).get("batches")


def test_ready_requires_separate_explicit_sharing_consent(tmp_path):
    from mcp_server.approvals import ConversationReview

    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    result = submit(store, snapshot, make_markdown("doc-ready", "deliverable"), operation="create", target="doc-ready")
    gateway = ConversationReview(load_config(path, "reader"))
    assert gateway.approve(**approval_request(result), allow_external_sharing=True)["data"]["state"] == "synced"
    assert store.snapshots.refresh().records["doc-ready"].metadata["share_status"] == "ready"


def test_retry_only_reuses_existing_authorization_and_stops_on_new_baseline(tmp_path, monkeypatch):
    from mcp_server.approvals import ConversationReview

    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    gateway = ConversationReview(load_config(path, "reader"))
    result = submit(store, snapshot, make_markdown(title="待同步"))
    original = gateway.service.repository.run

    def fail_push(*arguments, **kwargs):
        if arguments[0] == "push":
            raise HubError("SYNC_FAILED", "测试网络失败")
        return original(*arguments, **kwargs)

    monkeypatch.setattr(gateway.service.repository, "run", fail_push)
    pending = gateway.approve(**approval_request(result))["data"]
    assert pending["state"] == "pending_sync"
    monkeypatch.setattr(gateway.service.repository, "run", original)
    publish(work, make_markdown("proj-new"))
    assert gateway.retry(pending["batch_id"])["data"]["state"] == "needs_review"
    journal = read_json(gateway.service.journal_path)
    assert len(journal["batches"][pending["batch_id"]]["approvals"]) == 1
    assert store.snapshots.refresh().records["proj-alpha"].metadata["title"] == "测试记录"


def test_duplicate_approval_cannot_claim_a_different_delivery_was_saved(tmp_path):
    from mcp_server.approvals import ConversationReview
    from mcp_server.delivery import export_bundle, load_bundle

    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    gateway = ConversationReview(load_config(path, "reader"))
    result = submit(store, snapshot, make_markdown(title="真正批准的内容"))
    gateway.approve(**approval_request(result))
    preview = load_bundle(store.config, result["data"]["delivery"]["delivery_id"])["preview"]
    preview["items"][0]["proposed_markdown"] = make_markdown(title="从未批准的内容")
    changed = export_bundle(store.config, preview)
    with pytest.raises(HubError) as caught:
        gateway.approve(changed["delivery_id"], changed["review_digest"], CONFIRMATION)
    assert caught.value.code == "REVIEW_CHANGED"
    assert store.snapshots.refresh().records["proj-alpha"].metadata["title"] == "真正批准的内容"


def test_write_failure_response_does_not_claim_nothing_was_saved(tmp_path, monkeypatch):
    from mcp_server.approvals import ConversationReview
    from mcp_server.server import create_mcp

    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    result = submit(store, snapshot, make_markdown(title="异常返回测试"))

    def unavailable(*args):
        raise OSError("PRIVATE-WRITE-ERROR")

    monkeypatch.setattr(ConversationReview, "approve", unavailable)
    response = asyncio.run(create_mcp(load_config(path, "reader")).call_tool("approve_review_bundle", approval_request(result)))
    output = str(response)
    assert "PRIVATE-WRITE-ERROR" not in output
    assert "未写入正式知识" not in output
    assert "状态" in output


def test_successful_retry_reuses_same_approval_after_network_failure(tmp_path, monkeypatch):
    from mcp_server.approvals import ConversationReview

    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    gateway = ConversationReview(load_config(path, "reader"))
    result = submit(store, snapshot, make_markdown(title="可恢复同步"))
    original = gateway.service.repository.run

    def fail_push(*arguments, **kwargs):
        if arguments[0] == "push":
            raise HubError("SYNC_FAILED", "测试网络失败")
        return original(*arguments, **kwargs)

    monkeypatch.setattr(gateway.service.repository, "run", fail_push)
    pending = gateway.approve(**approval_request(result))["data"]
    assert pending["sync_error"]["code"] == "SYNC_FAILED"
    assert pending["sync_error"]["stage"] == "push"
    assert gateway.status()["data"][0]["sync_error"] == pending["sync_error"]
    monkeypatch.setattr(gateway.service.repository, "run", original)
    completed = gateway.retry(pending["batch_id"])["data"]
    assert completed["state"] == "synced" and completed["candidate"] == pending["candidate"]
    assert not completed.get("sync_error")
    assert not gateway.status()["data"][0].get("sync_error")
    assert len(read_json(gateway.service.journal_path)["batches"][completed["batch_id"]]["approvals"]) == 1


def test_gateway_refuses_terminal_approval_retries_and_disabled_config(tmp_path):
    from mcp_server.approvals import ConversationReview
    from tests.test_batch import accept

    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    with pytest.raises(HubError) as caught:
        ConversationReview(store.config)
    assert caught.value.code == "UNAUTHORIZED"
    gateway = ConversationReview(load_config(path, "reader"))
    result = submit(store, snapshot, make_markdown(title="终端批准的批次"))
    batch = gateway.service.approve([result["data"]["proposal_id"]], accept)
    with pytest.raises(HubError) as caught:
        gateway.retry(batch["batch_id"])
    assert caught.value.code == "UNAUTHORIZED"


def test_four_preference_records_can_be_saved_together_in_temporary_repo(tmp_path):
    from mcp_server.approvals import ConversationReview
    from mcp_server.context import ContextService
    from mcp_server.records import parse_record

    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    markdowns = [make_markdown("doc-original", "deliverable", body="# AGENTS\n完整规则原文\n",
                               share_status="internal", audience=[])]
    markdowns.extend(make_markdown("core-" + view, "core", views=[view], body="称呼主人，遵守本次授权。",
                                  related_ids=["doc-original"]) for view in ("base", "coding", "thinking"))
    proposals = [submit(store, snapshot, markdown, operation="create", target=parse_record(markdown).id)["data"]["proposal_id"]
                 for markdown in markdowns]
    delivery = store.prepare_review_bundle(proposals, snapshot.commit)["data"]
    gateway = ConversationReview(load_config(path, "reader"))
    batch = gateway.approve(delivery["delivery_id"], delivery["review_digest"], CONFIRMATION)["data"]
    assert batch["state"] == "synced"
    current = store.snapshots.refresh()
    for markdown in markdowns:
        expected = parse_record(markdown)
        actual = current.records[expected.id]
        assert actual.body == expected.body
        assert {key: value for key, value in actual.metadata.items() if key != "updated_at"} == {
            key: value for key, value in expected.metadata.items() if key != "updated_at"}
    coding = ContextService(current).get_core_context(view="coding")["data"]
    assert len(coding["base"]) == 1 and len(coding["extra"]) == 1
    tree = git(work, "--git-dir", str(gateway.service.repository.path), "ls-tree", "-r", "--name-only", batch["candidate"])
    assert "REVIEW.md" not in tree and "manifest.json" not in tree


def test_enabled_stdio_contract_and_strict_arguments(tmp_path):
    path, reviewer, store, snapshot, work = setup_conversation(tmp_path)
    result = submit(store, snapshot, make_markdown(title="真实协议批准"))
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "mcp_server.server", "--config", str(path)], cwd=str(Path(__file__).resolve().parents[1]))

    async def exercise():
        async with stdio_client(parameters) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing) as session:
                initialized = await session.initialize()
                assert "不能独立验证" in initialized.instructions
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"approve_review_bundle", "get_review_status", "retry_review_sync"} <= names
                invalid = (await session.call_tool("approve_review_bundle", {
                    **approval_request(result), "allow_external_sharing": "false",
                })).structuredContent
                assert invalid["error"]["code"] == "INVALID_ARGUMENT"
                approved = (await session.call_tool("approve_review_bundle", approval_request(result))).structuredContent
                assert approved["ok"] and approved["data"]["state"] == "synced"
                states = (await session.call_tool("get_review_status", {})).structuredContent
                assert states["data"][0]["state"] == "synced"

    asyncio.run(asyncio.wait_for(exercise(), timeout=30))
