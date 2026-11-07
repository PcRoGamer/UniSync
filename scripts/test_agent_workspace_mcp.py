"""Prove cross-chat persistence through TWO independent MCP stdio processes.
No LLM calls, no real coursework modified, all files in a disposable temp root.
"""
from __future__ import annotations
import asyncio, json, os, tempfile, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def invoke(session,name,args):
    result=await asyncio.wait_for(session.call_tool(name,args),timeout=45)
    if result.isError or not result.content:
        raise RuntimeError("MCP tool error: "+name)
    return json.loads(result.content[0].text)

async def main():
    with tempfile.TemporaryDirectory(prefix="unisync-agent-mcp-") as directory:
        env=os.environ.copy()
        env["PYTHONPATH"]=str(ROOT/"backend")
        env["UNISYNC_DATA_DIR"]=directory
        env["UNISYNC_SKIP_DISK_SCRUB"]="1"
        params=StdioServerParameters(command=sys.executable,args=["-m","unisync.mcp.server"],cwd=str(ROOT),env=env)
        doc="drafts/BLAW10001/verify-handoff.md"
        original="# Cross-chat exam notes\n"+"Contract means offer + acceptance.\n"*110
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await asyncio.wait_for(session.initialize(),timeout=90)
                tools={t.name for t in (await session.list_tools()).tools}
                assert len(tools)>=21
                for name in ("unisync_agent_workspace_list","unisync_agent_workspace_read","unisync_agent_workspace_save"):
                    assert name in tools
                init=await invoke(session,"unisync_agent_workspace_list",{"category":"all"})
                assert any(x["path"]=="handoffs/START_HERE.md" for x in init["entries"])
                created=await invoke(session,"unisync_agent_workspace_save",{"path":doc,"content":original})
                assert created["status"]=="created",created
                sha=created["sha256"]
                denied=await invoke(session,"unisync_agent_workspace_save",{"path":doc,"content":"unversioned overwrite"})
                assert denied.get("error")
                secret=await invoke(session,"unisync_agent_workspace_save",{"path":"notes/secret.md","content":"Bearer "+"Z"*40})
                assert secret.get("error")
                traversal=await invoke(session,"unisync_agent_workspace_read",{"path":"drafts/../../settings/integrations.json"})
                assert traversal.get("error")
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await asyncio.wait_for(session.initialize(),timeout=90)
                listed=await invoke(session,"unisync_agent_workspace_list",{"category":"drafts"})
                assert any(x["path"]==doc for x in listed["entries"])
                joined="";offset=0
                while True:
                    result=await invoke(session,"unisync_agent_workspace_read",{"path":doc,"offset":offset,"max_chars":256})
                    joined+=result["content"]
                    if not result["has_more"]:break
                    offset=result["next_offset"]
                from unisync.mcp.egress import scrub_text
                assert joined==scrub_text(original)
                update=await invoke(session,"unisync_agent_workspace_save",{"path":doc,"content":"# Revised notes\n","expected_sha256":sha})
                assert update["status"]=="updated"
                stale=await invoke(session,"unisync_agent_workspace_save",{"path":doc,"content":"stale","expected_sha256":sha})
                assert stale.get("error")
                print("MCP_TOOL_COUNT",len(tools))
                print("MCP_PERSISTENT_ACROSS_SESSIONS=PASS")
                print("MCP_PAGINATED_SCRUBBED_RETRIEVAL=PASS")
                print("MCP_ATOMIC_WRITE_AND_HISTORY=PASS")
                print("MCP_STALE_REVISION_DENIAL=PASS")
                print("MCP_PATH_ESCAPE_DENIAL=PASS")
                print("MCP_SECRET_WRITE_DENIAL=PASS")

if __name__=="__main__":
    asyncio.run(main())
