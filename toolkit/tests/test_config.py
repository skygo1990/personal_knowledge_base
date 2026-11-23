import pytest


def write_config(tmp_path, **changes):
    values = {
        "role": "reader",
        "device_id": "device-a",
        "remote_url": str(tmp_path / "remote.git"),
        "branch": "main",
        "data_root": "reader",
        "proposal_dir": "proposals",
        "review_status_dir": "reviewer/status",
        "token_file": "access-token",
    }
    values.update(changes)
    config_path = tmp_path / "settings.toml"
    config_path.write_text("\n".join(f'{key} = {value!r}' for key, value in values.items()))
    return config_path


def test_config_resolves_paths_against_file_not_cwd(tmp_path):
    from mcp_server.config import load_config

    config = load_config(write_config(tmp_path), "reader")
    assert config.data_root == tmp_path / "reader"
    assert config.proposal_dir == tmp_path / "proposals"
    assert config.listen_host == "127.0.0.1"


def test_security_mode_defaults_to_isolated(tmp_path):
    from mcp_server.config import load_config

    assert load_config(write_config(tmp_path), "reader").security_mode == "isolated"


def test_git_credential_helper_defaults_to_system(tmp_path):
    from mcp_server.config import load_config

    assert load_config(write_config(tmp_path), "reader").git_credential_helper == "system"


@pytest.mark.parametrize("role", ["reader", "reviewer"])
def test_github_cli_helper_is_explicit_single_user_option(tmp_path, role):
    from mcp_server.config import load_config

    path = write_config(tmp_path, role=role, security_mode="single_user", git_credential_helper="github_cli",
                        remote_url="https://github.com/owner/knowledge.git")
    if role == "reviewer":
        path.write_text("\n".join(line for line in path.read_text().splitlines() if not line.startswith("token_file")))
    assert load_config(path, role).git_credential_helper == "github_cli"


@pytest.mark.parametrize("change", [
    {"git_credential_helper": "arbitrary-command"},
    {"git_credential_helper": 1},
    {"security_mode": "isolated"},
    {"remote_url": "https://example.invalid/owner/knowledge.git"},
    {"remote_url": "https://github.com.evil.invalid/owner/knowledge.git"},
    {"remote_url": "ssh://git@github.com/owner/knowledge.git"},
    {"remote_url": "https://github.com:8443/owner/knowledge.git"},
    {"remote_url": "https://github.com/owner/../knowledge.git"},
    {"remote_url": "https://github.com/owner/knowledge.git?token=PRIVATE-SECRET"},
])
def test_github_cli_helper_rejects_unsafe_configuration(tmp_path, change):
    from mcp_server.config import load_config
    from mcp_server.models import HubError

    values = {"security_mode": "single_user", "git_credential_helper": "github_cli",
              "remote_url": "https://github.com/owner/knowledge.git", **change}
    with pytest.raises(HubError) as caught:
        load_config(write_config(tmp_path, **values), "reader")
    assert caught.value.code == "INVALID_CONFIG"
    assert "PRIVATE-SECRET" not in caught.value.message


def test_single_user_stdio_needs_no_http_token(tmp_path):
    from mcp_server.config import load_config

    path = write_config(tmp_path, security_mode="single_user", transport="stdio")
    path.write_text("\n".join(line for line in path.read_text().splitlines() if not line.startswith("token_file")))
    config = load_config(path, "reader")
    assert config.security_mode == "single_user"
    assert config.transport == "stdio"
    assert config.token_file is None


def test_reviewer_can_explicitly_choose_single_user(tmp_path):
    from mcp_server.config import load_config

    path = write_config(tmp_path, role="reviewer", security_mode="single_user")
    path.write_text("\n".join(line for line in path.read_text().splitlines() if not line.startswith("token_file")))
    assert load_config(path, "reviewer").security_mode == "single_user"


@pytest.mark.parametrize("change", [
    {"security_mode": "disabled"},
    {"security_mode": "single-user"},
    {"transport": "stdio"},
    {"security_mode": "single_user", "transport": "unknown"},
])
def test_security_mode_and_transport_fail_closed(tmp_path, change):
    from mcp_server.config import load_config
    from mcp_server.models import HubError

    with pytest.raises(HubError):
        load_config(write_config(tmp_path, **change), "reader")


@pytest.mark.parametrize("change", [
    {"remote_url": "https://not-a-real-token@example.invalid/repo"},
    {"remote_url": "ext::arbitrary-command"},
    {"remote_url": "https://example.invalid/repo?secret=not-real"},
    {"branch": "../wrong"},
    {"listen_host": "0.0.0.0"},
    {"data_root": "."},
    {"data_root": "proposals/reviewer-private"},
    {"data_root": "reviewer/status/private"},
    {"extra": "unknown"},
])
def test_config_rejects_unsafe_settings(tmp_path, change):
    from mcp_server.config import load_config
    from mcp_server.models import HubError

    with pytest.raises(HubError):
        load_config(write_config(tmp_path, **change), "reader")


def test_reviewer_cannot_run_reader_config(tmp_path):
    from mcp_server.config import load_config
    from mcp_server.models import HubError

    with pytest.raises(HubError):
        load_config(write_config(tmp_path), "reviewer")


def test_storage_refuses_symlinked_target(tmp_path):
    from mcp_server.storage import atomic_json
    from mcp_server.models import HubError

    destination = tmp_path / "outside"
    destination.write_text("unchanged")
    (tmp_path / "state.json").symlink_to(destination)
    with pytest.raises(HubError):
        atomic_json(tmp_path / "state.json", {"value": "changed"})
    assert destination.read_text() == "unchanged"


def test_atomic_state_and_lock(tmp_path):
    from mcp_server.storage import atomic_json, file_lock, read_json

    with file_lock(tmp_path / "state.lock"):
        atomic_json(tmp_path / "state.json", {"state": "approved"})
    assert read_json(tmp_path / "state.json") == {"state": "approved"}


def test_display_never_emits_terminal_sequences():
    from mcp_server.storage import safe_display

    result = safe_display("正文\x1b[2J\r隐藏\u202e内容")
    assert "\x1b" not in result and "\r" not in result and "\u202e" not in result
    assert "隐藏" in result and "U+001B" in result


def test_config_owned_by_another_identity_is_rejected(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from types import SimpleNamespace
    from mcp_server.config import load_config
    from mcp_server.models import HubError

    config_path = write_config(tmp_path)
    original_stat = Path.stat

    def stat_with_untrusted_owner(path, **kwargs):
        result = original_stat(path, **kwargs)
        if path == config_path:
            return SimpleNamespace(st_mode=result.st_mode, st_uid=os.getuid() + 10000)
        return result

    monkeypatch.setattr(Path, "stat", stat_with_untrusted_owner)
    with pytest.raises(HubError):
        load_config(config_path, "reader")
