import asyncio, json, sys, os, subprocess

env = os.environ.copy()
env["PYTHONPATH"] = "C:\\Users\\Admin\\Desktop\\UniSync\\backend"

cmd = [
    "C:\\Users\\Admin\\Desktop\\UniSync\\.venv\\Scripts\\python.exe",
    "-m", "unisync.mcp.server",
]

proc = subprocess.Popen(
    cmd,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env=env,
    text=True,
)

# Initialize
init_msg = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "clientInfo": {"name": "test", "version": "1.0"},
    },
}
proc.stdin.write(json.dumps(init_msg) + "\n")
proc.stdin.flush()

# Read initialize response
line = proc.stdout.readline()
print(f"Init: {json.loads(line.strip())['result']['serverInfo']['name']}")

# Call unisync_get_overview
call_msg = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
        "name": "unisync_get_overview",
        "arguments": {},
    },
}
proc.stdin.write(json.dumps(call_msg) + "\n")
proc.stdin.flush()

line = proc.stdout.readline()
msg = json.loads(line.strip())
content = msg["result"]["content"]
print(f"\nOverview: {content[0]['text'][:500]}...")

proc.terminate()
