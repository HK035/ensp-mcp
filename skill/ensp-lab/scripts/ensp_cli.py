"""Command line fallback for the eNSP console tools - same handlers as the MCP server.

Use it when the MCP server is not registered in Qoder yet, or to debug the channel:

    python ensp_cli.py status
    python ensp_cli.py devices
    python ensp_cli.py read 2001 "display ap all"
    python ensp_cli.py backup AC1
    python ensp_cli.py exec 2001 "system-view" "vlan batch 100"
    python ensp_cli.py exec 2001 --confirm save
    python ensp_cli.py release
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

import ensp_console as ec
import ensp_mcp_server as srv


def emit(tool: str, arguments: dict) -> int:
    try:
        data = srv.HANDLERS[tool](arguments)
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}")
        return 1
    text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
    cap = int(os.environ.get("ENSP_CLI_LIMIT", "12000"))
    print(text if len(text) <= cap else text[:cap] + "\n... [truncated]")
    return 0


ACCEPTS_TIMEOUT = {"ensp_read", "ensp_exec", "ensp_backup", "ensp_save_config", "ensp_wait_for"}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="eNSP console tools without MCP")
    sub = parser.add_subparsers(dest="tool", required=True)
    names = {
        "status": [], "devices": [], "release": ["device"],
        "read": ["device", "command"], "backup": ["device"], "save": ["device"],
        "wait": ["device", "command", "pattern"],
    }
    for name, positional in names.items():
        sp = sub.add_parser(name)
        for arg in positional:
            sp.add_argument(arg, nargs="?" if name == "release" else None)
        sp.add_argument("--topology")
        sp.add_argument("--confirm", action="store_true")
        sp.add_argument("--timeout", type=float)
        sp.add_argument("--limit", type=int)
        if name == "wait":
            sp.add_argument("--interval", type=float)

    ep = sub.add_parser("exec")
    ep.add_argument("device")
    ep.add_argument("commands", nargs="+")
    ep.add_argument("--topology")
    ep.add_argument("--confirm", action="store_true")
    ep.add_argument("--timeout", type=float)
    ep.add_argument("--limit", type=int)

    args = parser.parse_args(argv)
    tool = {
        "status": "ensp_status", "devices": "ensp_list_devices", "read": "ensp_read",
        "exec": "ensp_exec", "backup": "ensp_backup", "save": "ensp_save_config",
        "wait": "ensp_wait_for", "release": "ensp_release",
    }[args.tool]
    arguments = {k: v for k, v in vars(args).items()
                 if k not in ("tool",) and v is not None and v is not False}
    if args.tool == "exec":
        arguments["commands"] = args.commands
    if args.confirm or args.tool == "save":
        arguments["confirm"] = True
    if tool not in ACCEPTS_TIMEOUT:
        arguments.pop("timeout", None)
    try:
        return emit(tool, arguments)
    finally:
        ec.POOL.close_all()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
