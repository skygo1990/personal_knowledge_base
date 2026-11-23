from io import StringIO

from tests.helpers import make_markdown
from tests.test_batch import setup_review
from tests.test_proposals import submit


def test_cli_requires_config_without_guessing(monkeypatch, capsys):
    from review.cli import main

    monkeypatch.delenv("HUB_CONFIG", raising=False)
    assert main(["status"]) == 2
    assert "配置" in capsys.readouterr().err


def test_cli_approval_requires_tty():
    from review.cli import HumanTerminal
    from mcp_server.models import HubError

    terminal = HumanTerminal(StringIO("yes\n"), StringIO())
    try:
        terminal.confirm({"base_commit": "a" * 40, "items": []}, "确认 batch")
    except HubError as error:
        assert error.code == "UNAUTHORIZED"
    else:
        raise AssertionError("非本人交互会话不应批准")


def test_preview_safely_displays_basis_and_entire_content(tmp_path):
    from review.cli import render_preview

    service, store, snapshot, work = setup_review(tmp_path)
    proposal_id = submit(store, snapshot, make_markdown(title="完整预览"), basis="依据\x1b[2J\u202e风险")["data"]["proposal_id"]
    output = render_preview(service.preview([proposal_id]))
    assert "\x1b" not in output and "\u202e" not in output
    assert "U+001B" in output and "风险" in output
    assert "目标与范围" in output and "验证依据" in output
    assert "拟批准全文" in output and "远端当前全文" in output


def test_edit_requires_repreview_and_preserves_target(tmp_path):
    from mcp_server.models import HubError
    import pytest

    service, store, snapshot, work = setup_review(tmp_path)
    proposal_id = submit(store, snapshot, make_markdown(title="初稿"))["data"]["proposal_id"]
    service.edit(proposal_id, make_markdown(title="本人改稿"))
    assert "本人改稿" in service.preview([proposal_id])["items"][0]["proposed_markdown"]
    with pytest.raises(HubError):
        service.edit(proposal_id, make_markdown("proj-other"))


def test_cli_can_edit_a_draft_with_unresolved_references(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    import review.cli as cli

    service, store, snapshot, work = setup_review(tmp_path)
    markdown = make_markdown("doc-draft", "deliverable", scope=["proj-missing"])
    proposal_id = submit(store, snapshot, markdown, operation="create", target="doc-draft")["data"]["proposal_id"]
    monkeypatch.setattr(cli, "load_config", lambda *args: service.config)
    monkeypatch.setattr(cli, "validate_runtime", lambda config: None)
    monkeypatch.setattr(cli.HumanTerminal, "ask", lambda self, display, phrase: phrase)
    edited = []

    def editor(arguments, **kwargs):
        edited.append(arguments[-1])
        Path(arguments[-1]).write_text(make_markdown("doc-draft", "deliverable", scope=["global"]))

    monkeypatch.setattr(cli, "subprocess", SimpleNamespace(run=editor, SubprocessError=cli.subprocess.SubprocessError))
    monkeypatch.setattr(cli, "ReviewService", lambda config: service)
    assert cli.main(["--config", "unused", "edit", "--proposal", proposal_id]) == 0
    assert edited
    assert "global" in service.draft(proposal_id)["proposed_markdown"]
