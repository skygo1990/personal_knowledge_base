from pathlib import Path
import os
import subprocess
import tomllib

import pytest

from mcp_server.config import load_config
from mcp_server.models import HubError
from mcp_server.security import validate_runtime
from tests.git_helpers import remote_fixture


def initialize(tmp_path, **changes):
    from ai_context_hub.setup import initialize

    values = dict(home=tmp_path / "runtime", remote=str(tmp_path / "remote.git"), branch="shared",
                  codex_home=tmp_path / "codex", accepted=True)
    values.update(changes)
    return initialize(**values)


def test_install_requires_explicit_acceptance(tmp_path):
    with pytest.raises(HubError, match="确认"):
        initialize(tmp_path, accepted=False)
    assert not (tmp_path / "runtime").exists()


def test_initialize_creates_portable_private_config_and_preserves_client(tmp_path):
    codex = tmp_path / "codex"
    codex.mkdir()
    original = 'model = "existing-model"\n[mcp_servers.other]\ncommand = "keep-me"\n'
    (codex / "config.toml").write_text(original)
    (codex / "AGENTS.md").write_text("# Existing personal instructions\n")
    result = initialize(tmp_path)
    reader = load_config(tmp_path / "runtime/reader.toml", "reader")
    reviewer = load_config(tmp_path / "runtime/reviewer.toml", "reviewer")
    validate_runtime(reader)
    validate_runtime(reviewer)
    assert reader.transport == "stdio"
    assert reader.allow_conversation_approval is False
    assert reviewer.git_credential_helper == "system"
    config = tomllib.loads((codex / "config.toml").read_text())
    assert config["model"] == "existing-model"
    assert config["mcp_servers"]["other"]["command"] == "keep-me"
    assert "serve" in config["mcp_servers"]["personal_knowledge_base"]["args"]
    assert (codex / "AGENTS.md").read_text().startswith("# Existing personal instructions\n")
    assert (Path(result["home"]) / "STARTUP.md").exists()
    assert (tmp_path / "runtime/reader.toml").stat().st_mode & 0o777 == 0o600
    assert "knowledge" not in {entry.name for entry in (tmp_path / "runtime").iterdir()}


def test_repeat_install_preserves_data_and_does_not_duplicate_rules(tmp_path):
    initialize(tmp_path, conversation_approval=True)
    config = tmp_path / "runtime/reader.toml"
    before = config.read_bytes()
    draft = tmp_path / "runtime/proposals/existing.json"
    draft.write_text("private draft")
    rules = tmp_path / "codex/AGENTS.md"
    previous_rules = rules.read_bytes()
    initialize(tmp_path)
    assert config.read_bytes() == before
    assert draft.read_text() == "private draft"
    assert rules.read_bytes() == previous_rules


def test_different_remote_or_partial_config_never_overwrites(tmp_path):
    initialize(tmp_path)
    before = (tmp_path / "runtime/reader.toml").read_bytes()
    with pytest.raises(HubError):
        initialize(tmp_path, remote="https://example.invalid/other.git")
    assert (tmp_path / "runtime/reader.toml").read_bytes() == before


def test_existing_client_requires_explicit_replacement(tmp_path):
    codex = tmp_path / "codex"
    codex.mkdir()
    original = '[mcp_servers.personal_knowledge_base]\ncommand = "old-launcher"\n[mcp_servers.other]\ncommand = "keep"\n'
    config = codex / "config.toml"
    config.write_text(original)
    with pytest.raises(HubError, match="replace-client"):
        initialize(tmp_path)
    assert config.read_text() == original
    initialize(tmp_path, replace_client=True)
    updated = tomllib.loads(config.read_text())
    assert updated["mcp_servers"]["other"] == {"command": "keep"}
    assert updated["mcp_servers"]["personal_knowledge_base"]["command"] != "old-launcher"


@pytest.mark.parametrize("remote", ["https://secret@github.com/org/repo", "https://github.com/org/repo?token=secret", "ext::run", "-bad"])
def test_init_rejects_unsafe_remote_without_persisting_it(tmp_path, remote):
    with pytest.raises(HubError):
        initialize(tmp_path, remote=remote)
    assert not (tmp_path / "runtime/reader.toml").exists()


def test_cli_does_not_echo_embedded_credentials(tmp_path, capsys):
    from ai_context_hub.cli import main

    result = main(["init", "--home", str(tmp_path / "runtime"), "--remote", "https://PRIVATE-CREDENTIAL@example.invalid/repo"])
    captured = capsys.readouterr()
    assert result == 2
    assert "PRIVATE-CREDENTIAL" not in captured.out + captured.err


def test_init_refuses_symlink_and_world_writable_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "runtime").symlink_to(outside)
    with pytest.raises(HubError):
        initialize(tmp_path)
    assert not list(outside.iterdir())


def test_init_refuses_existing_public_runtime_without_chmod(tmp_path):
    home = tmp_path / "runtime"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    with pytest.raises(HubError):
        initialize(tmp_path)
    assert home.stat().st_mode & 0o777 == 0o755


def test_invalid_github_helper_target_leaves_no_config(tmp_path):
    with pytest.raises(HubError):
        initialize(tmp_path, remote="https://example.invalid/project.git", git_helper="github_cli")
    assert not (tmp_path / "runtime/reader.toml").exists()


def test_bootstrap_rejects_corrupt_bundle_before_installing_python(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    wheel = "ai_context_hub-0.2.0-py3-none-any.whl"
    (bundle / wheel).write_bytes(b"corrupt")
    (bundle / "requirements.lock").write_text("")
    (bundle / "SHA256SUMS").write_text("0" * 64 + "  " + wheel + "\n")
    home = tmp_path / "runtime"
    script = Path(__file__).resolve().parents[1] / "install.sh"
    result = subprocess.run(["sh", str(script), "--accept-install"], capture_output=True, text=True,
                            env={**os.environ, "PKB_HOME": str(home), "PKB_BUNDLE_DIR": str(bundle)}, timeout=15)
    assert result.returncode != 0
    assert "校验失败" in result.stderr
    assert not (home / "apps/v0.2.0").exists()


def test_doctor_uses_real_stdio_and_latest_optional_tools(tmp_path):
    from ai_context_hub.setup import doctor

    remote, work = remote_fixture(tmp_path)
    initialize(tmp_path, remote=str(remote), conversation_approval=True, codex_home=None)
    result = doctor(tmp_path / "runtime", refresh=True)
    assert result["ok"] is True
    assert result["tool_count"] == 9
    assert result["sync_status"] == "fresh"
    assert result["project_count"] == 1
    assert "approve_review_bundle" in result["tools"]
    assert not list((tmp_path / "runtime/proposals").glob("*.json"))


def test_packaged_startup_rules_have_no_source_directory_dependency():
    from ai_context_hub.resources import startup_rules

    assert "prepare_review_bundle" in startup_rules()
    assert "slug" in startup_rules()
