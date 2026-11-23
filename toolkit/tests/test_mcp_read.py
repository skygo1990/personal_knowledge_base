import pytest
from starlette.testclient import TestClient

from tests.git_helpers import config_for, remote_fixture


def make_app(tmp_path):
    from mcp_server.server import create_app

    remote, work = remote_fixture(tmp_path)
    config = config_for(tmp_path, remote)
    token_file = tmp_path / "access-token"
    token_file.write_text("test-only-access-token-0123456789abcd")
    token_file.chmod(0o600)
    object.__setattr__(config, "token_file", token_file)
    return create_app(config), config


def rpc(client, method, params=None, request_id=1, **headers):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
                       headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer test-only-access-token-0123456789abcd", **headers})


def test_real_mcp_initialize_tools_and_core_read(tmp_path):
    app, config = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        initialized = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}})
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]
        result = rpc(client, "tools/list").json()["result"]
        assert {tool["name"] for tool in result["tools"]} == {"get_core_context", "get_project_context", "search_lessons", "find_deliverables", "propose_memory_update", "prepare_review_bundle"}
        core = rpc(client, "tools/call", {"name": "get_core_context", "arguments": {"refresh": True}}).json()["result"]["structuredContent"]
        assert core["ok"] is True
        assert core["data"]["projects"][0]["id"] == "proj-alpha"
        project = rpc(client, "tools/call", {"name": "get_project_context", "arguments": {"project_id": "proj-alpha", "snapshot_commit": core["snapshot_commit"]}}).json()["result"]["structuredContent"]
        assert project["snapshot_commit"] == core["snapshot_commit"]
        assert "验证依据" in project["data"]["record"]["body"]


@pytest.mark.parametrize("headers,status", [
    ({"Authorization": ""}, 401), ({"Authorization": "Bearer wrong"}, 401),
    ({"Origin": "https://external.invalid"}, 403), ({"Host": "external.invalid"}, 421),
    ({"Origin": "null"}, 403),
])
def test_transport_rejects_unsafe_requests(tmp_path, headers, status):
    app, config = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert rpc(client, "tools/list", **headers).status_code == status


def test_refresh_cannot_mix_with_pinned_snapshot(tmp_path):
    app, config = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = rpc(client, "tools/call", {"name": "get_core_context", "arguments": {"refresh": True, "snapshot_commit": "a" * 40}})
        assert response.json()["result"]["structuredContent"]["ok"] is False


@pytest.mark.parametrize("name,arguments", [
    ("get_core_context", {"limit": "PRIVATE-INPUT-MARKER"}),
    ("get_core_context", {"limit": True}),
    ("get_core_context", {"limit": 1.0}),
    ("get_core_context", {"limit": "1"}),
    ("find_deliverables", {"audiences": "PRIVATE-INPUT-MARKER"}),
    ("propose_memory_update", {"basis": {"secret": "PRIVATE-INPUT-MARKER"}}),
    ("prepare_review_bundle", {"proposal_ids": "PRIVATE-INPUT-MARKER"}),
    ("prepare_review_bundle", {"proposal_ids": [1]}),
])
def test_invalid_tool_arguments_are_strict_and_do_not_echo_input(tmp_path, name, arguments):
    app, config = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        result = rpc(client, "tools/call", {"name": name, "arguments": arguments}).json()["result"]
        assert "PRIVATE-INPUT-MARKER" not in str(result)
        assert result["structuredContent"]["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("field", ["audience", "record_id", "snapshot_commit"])
def test_literal_null_string_is_not_reinterpreted(tmp_path, field):
    from tests.git_helpers import publish
    from tests.helpers import make_markdown

    app, config = make_app(tmp_path)
    publish(tmp_path / "work", make_markdown("doc-parents", "deliverable"))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        rpc(client, "tools/call", {"name": "get_core_context", "arguments": {"refresh": True}})
        result = rpc(client, "tools/call", {"name": "find_deliverables", "arguments": {field: "null"}}).json()["result"]["structuredContent"]
        if field == "audience":
            assert result["data"]["items"] == []
        else:
            assert result["ok"] is False


@pytest.mark.parametrize("payload", [
    {"jsonrpc": "PRIVATE-ENVELOPE-MARKER", "id": 1, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_core_context", "arguments": [{"secret": "PRIVATE-ENVELOPE-MARKER"}]}},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "\x1b[2JPRIVATE-ENVELOPE-MARKER", "arguments": {}}},
])
def test_invalid_envelope_never_reaches_sdk_logs(tmp_path, caplog, payload):
    app, config = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/mcp", json=payload, headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer test-only-access-token-0123456789abcd"})
        assert response.status_code == 400
        assert "PRIVATE-ENVELOPE-MARKER" not in response.text
        assert "PRIVATE-ENVELOPE-MARKER" not in caplog.text


@pytest.mark.parametrize("headers", [
    {"Content-Type": "PRIVATE-HEADER-MARKER"},
    {"Content-Type": " application/json; PRIVATE-HEADER-MARKER"},
    {"MCP-Protocol-Version": "PRIVATE-HEADER-MARKER"},
])
def test_invalid_protocol_headers_never_echo_to_response_or_log(tmp_path, caplog, headers):
    app, config = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = rpc(client, "tools/list", **headers)
        assert response.status_code >= 400
        assert "PRIVATE-HEADER-MARKER" not in response.text
        assert "PRIVATE-HEADER-MARKER" not in caplog.text


@pytest.mark.parametrize("method,params", [
    ("prompts/get", {"name": "\x1b[2JPRIVATE-METHOD-MARKER"}),
    ("resources/read", {"uri": "file:///PRIVATE-METHOD-MARKER"}),
    ("logging/setLevel", {"level": "debug"}),
])
def test_unsupported_mcp_surfaces_are_safely_rejected(tmp_path, caplog, method, params):
    app, config = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = rpc(client, method, params)
        assert response.status_code == 400
        assert "PRIVATE-METHOD-MARKER" not in response.text
        assert "PRIVATE-METHOD-MARKER" not in caplog.text


def test_initialized_notification_is_supported(tmp_path):
    app, config = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                               headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer test-only-access-token-0123456789abcd"})
        assert response.status_code == 202
