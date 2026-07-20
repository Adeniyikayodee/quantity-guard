"""A small MCP server written the ordinary way, for the proxy demo.

It states its units in prose and answers in bare numbers, which is what most tool servers
do. Nothing here knows about quantity-guard; the point of the demo is that it does not
have to.

Run it behind the proxy:

    quantity-guard-mcp --annotations demo/water.toml -- python demo/usgs_server.py
"""

from __future__ import annotations

import json
import sys

TOOLS = [
    {
        "name": "read_discharge",
        "description": "Latest observed discharge at a streamgage, in cfs.",
        "inputSchema": {
            "type": "object",
            "properties": {"station": {"type": "string"}},
            "required": ["station"],
        },
    },
    {
        "name": "read_drainage_area",
        "description": "Contributing drainage area, in square kilometres.",
        "inputSchema": {
            "type": "object",
            "properties": {"station": {"type": "string"}},
            "required": ["station"],
        },
    },
    {
        "name": "runoff_depth",
        "description": "Depth-equivalent runoff. Discharge in m3/s, area in km2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "discharge": {"type": "number"},
                "area": {"type": "number"},
            },
            "required": ["discharge", "area"],
        },
    },
]

OBSERVED = {"discharge_cfs": 1250.0, "area_km2": 29000.0}


def call(name: str, arguments: dict) -> dict:
    if name == "read_discharge":
        value = OBSERVED["discharge_cfs"]
    elif name == "read_drainage_area":
        value = OBSERVED["area_km2"]
    elif name == "runoff_depth":
        value = arguments["discharge"] / (arguments["area"] * 1e6) * 1000 * 86400
    else:
        return {"content": [{"type": "text", "text": f"no tool {name}"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(value)}]}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if message.get("id") is None:
            continue
        method = message["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "usgs-demo", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = call(params.get("name", ""), params.get("arguments") or {})
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}),
              flush=True)


if __name__ == "__main__":
    main()
