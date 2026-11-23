import os

import pytest

from mcp_server.models import HubError


def test_token_file_rejects_world_readable_or_symlink(tmp_path):
    from mcp_server.security import read_token

    token = tmp_path / "token"
    token.write_text("test-only-token-not-a-secret-123456789")
    token.chmod(0o644)
    with pytest.raises(HubError):
        read_token(token)
    token.chmod(0o600)
    assert read_token(token).startswith("test-only")
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(HubError):
        read_token(link)


def test_reader_refuses_writable_approval_status(tmp_path):
    from mcp_server.security import validate_runtime
    from tests.git_helpers import config_for

    config = config_for(tmp_path, tmp_path / "remote.git")
    config.data_root.mkdir(mode=0o700)
    config.proposal_dir.mkdir(mode=0o700)
    config.review_status_dir.mkdir(mode=0o700)
    with pytest.raises(HubError, match="状态投影"):
        validate_runtime(config)


def test_single_user_allows_same_owner_status(tmp_path):
    from dataclasses import replace
    from mcp_server.security import validate_runtime
    from tests.git_helpers import config_for

    config = replace(config_for(tmp_path, tmp_path / "remote.git"), security_mode="single_user")
    for directory in (config.data_root, config.proposal_dir, config.review_status_dir):
        directory.mkdir(mode=0o700)
    validate_runtime(config)


@pytest.mark.parametrize("directory_name", ["data_root", "proposal_dir", "review_status_dir"])
def test_single_user_still_requires_private_directories(tmp_path, directory_name):
    from dataclasses import replace
    from mcp_server.security import validate_runtime
    from tests.git_helpers import config_for

    config = replace(config_for(tmp_path, tmp_path / "remote.git"), security_mode="single_user")
    for directory in (config.data_root, config.proposal_dir, config.review_status_dir):
        directory.mkdir(mode=0o700)
    getattr(config, directory_name).chmod(0o755)
    with pytest.raises(HubError):
        validate_runtime(config)


def test_private_data_requires_restricted_permissions(tmp_path):
    from mcp_server.security import validate_runtime
    from tests.git_helpers import config_for

    config = config_for(tmp_path, tmp_path / "remote.git", role="reviewer")
    config.data_root.mkdir()
    config.data_root.chmod(0o777)
    with pytest.raises(HubError):
        validate_runtime(config)


def test_token_under_untrusted_parent_is_rejected(tmp_path):
    from mcp_server.security import read_token

    parent = tmp_path / "replaceable"
    parent.mkdir()
    parent.chmod(0o777)
    token = parent / "token"
    token.write_text("test-only-token-with-enough-length-123")
    token.chmod(0o600)
    with pytest.raises(HubError):
        read_token(token)


def test_private_root_under_untrusted_parent_is_rejected(tmp_path):
    from mcp_server.security import validate_runtime
    from tests.git_helpers import config_for

    parent = tmp_path / "replaceable"
    parent.mkdir()
    parent.chmod(0o777)
    config = config_for(parent, tmp_path / "remote.git", role="reviewer")
    config.data_root.mkdir(mode=0o700)
    config.proposal_dir.mkdir(mode=0o700)
    config.review_status_dir.mkdir(mode=0o700)
    with pytest.raises(HubError):
        validate_runtime(config)


def test_missing_token_ancestor_is_safe_error(tmp_path):
    from mcp_server.security import read_token

    with pytest.raises(HubError):
        read_token(tmp_path / "missing" / "token")


def test_only_shared_proposal_and_projection_allow_group_read(tmp_path):
    from tests.helpers import make_markdown
    from tests.test_batch import accept, setup_review
    from tests.test_proposals import submit

    service, store, snapshot, work = setup_review(tmp_path)
    proposal_id = submit(store, snapshot, make_markdown(title="分账户共享"))["data"]["proposal_id"]
    proposal = store.config.proposal_dir / (proposal_id + ".json")
    assert proposal.stat().st_mode & 0o777 == 0o640
    service.approve([proposal_id], accept)
    status = store.config.review_status_dir / (proposal_id + ".json")
    assert status.stat().st_mode & 0o777 == 0o640
    assert service.journal_path.stat().st_mode & 0o777 == 0o600
