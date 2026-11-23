from concurrent.futures import ThreadPoolExecutor

import pytest

from mcp_server.models import HubError
from tests.git_helpers import config_for, git, publish, remote_fixture
from tests.helpers import make_markdown


def test_first_fetch_pinned_snapshot_and_update(tmp_path):
    from review.sync import SnapshotStore

    remote, work = remote_fixture(tmp_path)
    store = SnapshotStore(config_for(tmp_path, remote))
    initial = store.refresh()
    assert initial.sync_status == "fresh"
    assert initial.commit == git(work, "rev-parse", "HEAD")
    latest = publish(work, make_markdown(title="更新项目"))
    assert store.get().commit == initial.commit
    assert store.refresh().commit == latest
    assert store.get(initial.commit).records["proj-alpha"].metadata["title"] == "测试记录"
    with pytest.raises(HubError, match="快照"):
        store.get("a" * 40)
    with pytest.raises(HubError):
        store.get("")


def test_invalid_remote_keeps_old_snapshot(tmp_path):
    from review.sync import SnapshotStore

    remote, work = remote_fixture(tmp_path)
    store = SnapshotStore(config_for(tmp_path, remote))
    original = store.refresh()
    publish(work, raw_path="knowledge/L1_projects/broken.md", raw="not a record")
    failure = store.refresh()
    assert failure.commit == original.commit
    assert failure.sync_status == "stale"
    assert failure.last_synced_at == original.last_synced_at
    assert failure.warnings


def test_offline_with_and_without_cache(tmp_path):
    from review.sync import SnapshotStore

    remote, work = remote_fixture(tmp_path)
    store = SnapshotStore(config_for(tmp_path, remote))
    original = store.refresh()
    remote.rename(tmp_path / "unreachable.git")
    assert store.refresh().commit == original.commit
    assert store.get().sync_status == "stale"
    empty = SnapshotStore(config_for(tmp_path, remote, device="device-b"))
    with pytest.raises(HubError):
        empty.refresh()


def test_empty_remote_is_unavailable(tmp_path):
    from review.sync import SnapshotStore

    remote, work = remote_fixture(tmp_path)
    config = config_for(tmp_path, remote)
    object.__setattr__(config, "branch", "does-not-exist")
    with pytest.raises(HubError):
        SnapshotStore(config).refresh()


@pytest.mark.parametrize("stderr,expected", [
    (b"fatal: could not read Username for 'PRIVATE-SECRET': terminal prompts disabled", "Git 认证失败"),
    (b"remote: Permission to PRIVATE-SECRET denied", "Git 写入权限不足"),
    (b"remote: GH013: Repository rule violations found PRIVATE-SECRET", "Git 分支规则拒绝推送"),
    (b"Failed to connect to PRIVATE-SECRET", "Git 连接失败"),
])
def test_git_failures_return_safe_actionable_categories(tmp_path, monkeypatch, stderr, expected):
    import subprocess
    from review.sync import GitRepository

    repository = GitRepository(config_for(tmp_path, tmp_path / "remote.git"))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 128, b"", stderr))
    with pytest.raises(HubError) as caught:
        repository.run("push")
    assert caught.value.code == "SYNC_FAILED"
    assert expected in caught.value.message
    assert "PRIVATE-SECRET" not in caught.value.message


def test_github_cli_helper_is_scoped_and_does_not_persist_credentials(tmp_path, monkeypatch):
    from dataclasses import replace
    import shutil
    from review.sync import GitRepository

    monkeypatch.setenv("HOME", str(tmp_path))
    helper = tmp_path / "gh helper's executable"
    helper.write_text("#!/bin/sh\n"
                      '[ "$1" = auth ] && [ "$2" = git-credential ] && [ "$3" = get ] || exit 1\n'
                      "cat >/dev/null\n"
                      "printf 'username=test-user\\npassword=synthetic-test-secret\\n'\n")
    helper.chmod(0o700)
    monkeypatch.setattr(shutil, "which", lambda name: str(helper) if name == "gh" else None)
    global_config = tmp_path / ".gitconfig"
    global_config.write_text('[credential]\n\thelper = !printf "password=WRONG-HELPER\\\\n"\n')
    before = global_config.read_bytes()
    config = replace(config_for(tmp_path, "https://github.com/owner/knowledge.git"),
                     security_mode="single_user", git_credential_helper="github_cli")
    repository = GitRepository(config)
    repository.initialize()
    request = b"protocol=https\nhost=github.com\npath=owner/knowledge.git\n\n"
    result = repository.run("credential", "fill", input=request)
    assert b"password=synthetic-test-secret" in result.stdout
    assert b"WRONG-HELPER" not in result.stdout
    for other in (b"protocol=https\nhost=example.invalid\npath=owner/knowledge.git\n\n",
                  b"protocol=https\nhost=github.com\npath=other/repository.git\n\n"):
        denied = repository.run("credential", "fill", input=other, allow_failure=True)
        assert denied.returncode != 0
        assert b"synthetic-test-secret" not in denied.stdout + denied.stderr
    local_config = (repository.path / "config").read_text()
    assert "credential" not in local_config and "synthetic-test-secret" not in local_config
    assert global_config.read_bytes() == before


def test_github_cli_helper_missing_executable_fails_closed(tmp_path, monkeypatch):
    from dataclasses import replace
    import shutil
    from review.sync import GitRepository

    monkeypatch.setattr(shutil, "which", lambda name: None)
    config = replace(config_for(tmp_path, "https://github.com/owner/knowledge.git"),
                     security_mode="single_user", git_credential_helper="github_cli")
    with pytest.raises(HubError) as caught:
        GitRepository(config)
    assert caught.value.code == "INVALID_CONFIG"
    assert "GitHub CLI" in caught.value.message


def test_default_git_does_not_resolve_github_cli(tmp_path, monkeypatch):
    import shutil
    from review.sync import GitRepository

    def unexpected_lookup(name):
        pytest.fail("default Git must not resolve a credential helper")

    monkeypatch.setattr(shutil, "which", unexpected_lookup)
    repository = GitRepository(config_for(tmp_path, tmp_path / "remote.git"))
    repository.initialize()
    assert repository.run("config", "--local", "--get", "credential.helper", allow_failure=True).returncode == 1


def test_concurrent_refreshes_are_serialized(tmp_path):
    from review.sync import SnapshotStore

    remote, work = remote_fixture(tmp_path)
    config = config_for(tmp_path, remote)
    store = SnapshotStore(config)
    store.refresh()
    latest = publish(work, make_markdown(title="新状态"))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: SnapshotStore(config).refresh().commit, range(2)))
    assert results == [latest, latest]
    assert store.get().commit == latest


def test_non_knowledge_files_not_exported(tmp_path):
    from review.sync import SnapshotStore

    remote, work = remote_fixture(tmp_path)
    publish(work, raw_path="mcp_server/injected.py", raw="raise Exception('never run')")
    store = SnapshotStore(config_for(tmp_path, remote))
    snapshot = store.refresh()
    assert not (snapshot.root / "mcp_server").exists()


def test_remote_history_rollback_retains_latest_snapshot(tmp_path, monkeypatch):
    from review.sync import SnapshotStore

    remote, work = remote_fixture(tmp_path)
    store = SnapshotStore(config_for(tmp_path, remote))
    old = store.refresh()
    latest = publish(work, make_markdown(title="不能倒退"))
    assert store.refresh().commit == latest
    monkeypatch.setattr(store.repository, "fetch", lambda: old.commit)
    result = store.refresh()
    assert result.commit == latest
    assert result.sync_status == "stale"
    assert "不连续" in result.warnings[0]
