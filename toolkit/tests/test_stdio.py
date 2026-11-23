import asyncio
from pathlib import Path
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.git_helpers import git, remote_fixture
from tests.helpers import make_markdown


def test_single_user_stdio_proposal_preview_and_confirmation(tmp_path):
    remote, work = remote_fixture(tmp_path)
    before = git(work, "rev-parse", "HEAD")
    for directory in ("reader", "reviewer", "proposals", "status"):
        (tmp_path / directory).mkdir(mode=0o700)
    common = f'''security_mode = "single_user"
device_id = "device-test"
remote_url = "{remote}"
branch = "shared"
proposal_dir = "proposals"
review_status_dir = "status"
'''
    reader = tmp_path / "reader.toml"
    reader.write_text(common + 'role = "reader"\ndata_root = "reader"\ntransport = "stdio"\n')
    reviewer = tmp_path / "reviewer.toml"
    reviewer.write_text(common + 'role = "reviewer"\ndata_root = "reviewer"\n')
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "mcp_server.server", "--config", str(reader)], cwd=str(Path(__file__).resolve().parents[1]))

    async def exercise():
        async with stdio_client(parameters) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing) as session:
                initialized = await session.initialize()
                assert "同账户" in initialized.instructions
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "get_core_context", "get_project_context", "search_lessons", "find_deliverables", "propose_memory_update", "prepare_review_bundle"}
                core = (await session.call_tool("get_core_context", {"refresh": True})).structuredContent
                assert core["ok"] is True
                assert core["snapshot_commit"] == before
                result = (await session.call_tool("propose_memory_update", {
                    "trigger": "manual", "basis": "本机安装测试", "operation": "replace", "target_id": "proj-alpha",
                    "base_commit": before, "proposed_markdown": make_markdown(title="同账户待审修改"),
                })).structuredContent
                assert result["ok"] is True, result
                delivery = (await session.call_tool("prepare_review_bundle", {
                    "proposal_ids": [result["data"]["proposal_id"]], "snapshot_commit": before,
                })).structuredContent
                assert delivery["ok"] is True, delivery
                assert "同账户待审修改" in Path(delivery["data"]["human_path"]).read_text()
                assert delivery["data"]["delivery_id"] == result["data"]["delivery"]["delivery_id"]
                current = (await session.call_tool("get_project_context", {"project_id": "proj-alpha"})).structuredContent
                assert current["data"]["record"]["title"] == "测试记录"
                return result["data"]["proposal_id"]

    proposal_id = asyncio.run(asyncio.wait_for(exercise(), timeout=30))
    command = [sys.executable, "-m", "review.cli", "--config", str(reviewer)]
    preview = subprocess.run([*command, "preview", "--proposal", proposal_id], capture_output=True, text=True, timeout=30)
    assert preview.returncode == 0
    assert "同账户待审修改" in preview.stdout
    assert "差异" in preview.stdout
    approve = subprocess.run([*command, "approve", "--proposal", proposal_id], input="yes\n",
                             capture_output=True, text=True, timeout=30)
    assert approve.returncode == 2
    assert "交互终端" in approve.stderr
    assert git(remote, "rev-parse", "refs/heads/shared") == before
    assert (tmp_path / "proposals" / (proposal_id + ".json")).exists()
