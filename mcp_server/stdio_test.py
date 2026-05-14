"""End-to-end smoke test through the MCP stdio transport (JSON-RPC).

Proves the registered server starts, lists tools, and answers a real query.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SERVER = [
    str(Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"),
    str(Path(__file__).resolve().parent / "server.py"),
]


def main() -> None:
    proc = subprocess.Popen(
        SERVER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    def send(req: dict) -> dict:
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        return json.loads(line)

    # MCP initialise handshake
    init = send({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "stdio-test", "version": "0.1"},
        },
    })
    print("\n--- initialize ---")
    print(json.dumps(init, indent=2)[:600])

    # mcp library expects this notification after init
    proc.stdin.write(json.dumps({
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    }) + "\n")
    proc.stdin.flush()

    tools = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    print("\n--- tools/list (names) ---")
    for t in tools.get("result", {}).get("tools", []):
        print(f"  • {t['name']}")

    # Real query
    call = send({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "kg_search",
                   "arguments": {"query": '"hedge accounting" position', "limit": 3}},
    })
    print("\n--- kg_search('hedge accounting position', 3) ---")
    print(json.dumps(call, indent=2)[:1500])

    stats = send({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "kg_stats", "arguments": {}},
    })
    print("\n--- kg_stats ---")
    print(json.dumps(stats.get("result"), indent=2)[:600])

    proc.terminate()
    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
