"""MCP stdio server that drives Huawei eNSP devices through their console ports.

Zero dependencies: speaks JSON-RPC 2.0 over stdin/stdout with newline framing,
so it runs on a lab machine with no network access and no pip install.

Register with ``python install.py --apply``, which merges the entry into
~/.qoder-cn/settings.json and links the skill. To add it by hand instead, use
this file's absolute path as the only argument:

    command: <python>
    args:    ["<this file>"]
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ensp_console as ec

SERVER_NAME = "ensp"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
FALLBACK_PROTOCOL = "2024-11-05"

# captured configs contain device credentials, so keep them out of the repo tree
BACKUP_DIR = os.environ.get("ENSP_BACKUP_DIR") or os.path.join(
    os.path.expanduser("~"), ".ensp-mcp", "backups"
)

READ_ONLY_RE = re.compile(
    r"^\s*(?:display|dis|ping|tracert|trace|arping|nslookup|telnet|snmpget|tee|slot|debugsupport)\b",
    re.I,
)
DESTRUCTIVE_RE = re.compile(
    r"^\s*(?:save|reset|delete|reboot|shutdown|undo\s+shutdown|format|erase|clear|kill|vsys)\b",
    re.I,
)

INSTRUCTIONS = (
    "Drives Huawei eNSP by its per-device console ports (127.0.0.1:2000+). "
    "Always call ensp_list_devices first to bind a device name to a port, "
    "ensp_backup before a config batch, and ensp_release before the user types "
    "in the eNSP GUI. Writes are refused unless confirm=true."
)


def tool_schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


PROPS = {
    "device": {
        "type": "string",
        "description": "Console port (e.g. \"2001\") or device name from the topology (e.g. \"AC1\", \"LSW1\"). A unique substring also works.",
    },
    "command": {"type": "string", "description": "One VRP command line, without a trailing newline."},
    "commands": {
        "type": "array",
        "items": {"type": "string"},
        "description": "VRP command lines sent in order, one per line. Include \"system-view\" yourself when you mean to configure.",
    },
    "confirm": {
        "type": "boolean",
        "description": "Must be true for anything that persists or destroys: save, reset, delete, reboot, shutdown, format, clear, undo shutdown.",
    },
    "timeout": {"type": "number", "description": "Seconds to wait per command. Default 25, max 180."},
    "topology": {"type": "string", "description": "Path to a .topo file. Omit to auto-discover the newest one whose com_port values match the live console ports."},
    "pattern": {"type": "string", "description": "Regex matched against command output."},
    "interval": {"type": "number", "description": "Seconds between polls. Default 3."},
    "wait_until_absent": {"type": "boolean", "description": "Poll until the pattern disappears instead of appears."},
    "path": {"type": "string", "description": "File to write. Omit to use backups/<device>-<timestamp>.cfg."},
    "limit": {"type": "number", "description": "Maximum characters of output returned per command. Default 6000."},
}

TOOLS = [
    tool_schema(
        "ensp_status",
        "Report the eNSP installation and channel health: which eNSP processes run, which console ports listen, whether VirtualBox devices are up, and which .topo files were found. Read-only and never touches a device, so run it first.",
        {}, [],
    ),
    tool_schema(
        "ensp_list_devices",
        "Inventory every device reachable on eNSP console ports with its name, model, port and live CLI view (user-view / system-view / interface-view). Sends only a carriage return per device, which is what a terminal does, and reuses pooled sessions.",
        {"topology": PROPS["topology"]}, [],
    ),
    tool_schema(
        "ensp_read",
        "Run one read-only VRP command (display / ping / tracert) on a device and return its output with paging handled. Refuses configuration statements - use ensp_exec for those.",
        {"device": PROPS["device"], "command": PROPS["command"],
         "timeout": PROPS["timeout"], "limit": PROPS["limit"]},
        ["device", "command"],
    ),
    tool_schema(
        "ensp_exec",
        "Push a batch of VRP commands to one device, in order. Returns per-command output plus an error flag. Confirm-prompt answers are supplied automatically. Persistent or destructive commands require confirm=true. The session keeps its CLI view between calls, so end in system-view only on purpose.",
        {"device": PROPS["device"], "commands": PROPS["commands"],
         "confirm": PROPS["confirm"], "timeout": PROPS["timeout"], "limit": PROPS["limit"]},
        ["device", "commands"],
    ),
    tool_schema(
        "ensp_backup",
        "Capture a device's full running configuration and write it to a timestamped .cfg file under ensp-mcp/backups. Run this before any ensp_exec batch so the change can be rolled back.",
        {"device": PROPS["device"], "path": PROPS["path"], "timeout": PROPS["timeout"]},
        ["device"],
    ),
    tool_schema(
        "ensp_save_config",
        "Persist the running configuration to flash (VRP \"save\"), answering the confirmation prompts. Requires confirm=true because it writes to the device filesystem.",
        {"device": PROPS["device"], "confirm": PROPS["confirm"]},
        ["device", "confirm"],
    ),
    tool_schema(
        "ensp_wait_for",
        "Poll a read-only command until a regex appears (or disappears), with a timeout. Built for asynchronous checks such as waiting for \"display ap all\" to report an AP in Normal state.",
        {"device": PROPS["device"], "command": PROPS["command"], "pattern": PROPS["pattern"],
         "timeout": PROPS["timeout"], "interval": PROPS["interval"],
         "wait_until_absent": PROPS["wait_until_absent"]},
        ["device", "command", "pattern"],
    ),
    tool_schema(
        "ensp_release",
        "Close pooled console sockets (all devices, or one). Do this before the user works in the eNSP GUI: one console slot per port is shared, and holding it makes GUI typing appear to do nothing.",
        {"device": PROPS["device"]}, [],
    ),
]

# --------------------------------------------------------------------- tool bodies


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars; raise limit for the full text]"


def _devices(topology: str | None = None) -> dict:
    return ec.inventory(topo_path=topology)


def _port_for(target: str, topology: str | None) -> int:
    return ec.resolve(target, _devices(topology)["devices"])


def _timeout(value, default: float = 25.0) -> float:
    try:
        return max(2.0, min(float(value), 180.0))
    except (TypeError, ValueError):
        return default


def _limit(value, default: int = 6000) -> int:
    try:
        return max(300, min(int(value), ec.OUTPUT_CAP))
    except (TypeError, ValueError):
        return default


def tool_status(args: dict) -> dict:
    inv = _devices(args.get("topology"))
    procs = inv["processes"]
    notes = []
    if "client" not in procs:
        notes.append("eNSP_Client.exe is not running - open eNSP and load the topology.")
    if not inv["listening_ports"]:
        notes.append("No console ports listen in 2000-2031; eNSP allocates them when a topology loads.")
    if procs.get("switch") and not procs.get("vboxserver"):
        notes.append("eNSP_VBoxServer is down: AR/AC/AP (VirtualBox) consoles answer ##\\x00. In-process switches/Luoshu devices still work.")
    if not inv["topology"]:
        notes.append("No .topo file matched the live ports, so device names are unknown. Save the topology in eNSP (File > Save) to bind names/models via com_port.")
    if inv["unmapped_ports"]:
        notes.append(f"Ports without a topology name: {inv['unmapped_ports']}. Identify them by reading their prompt with ensp_list_devices.")
    return {**inv, "notes": notes}


def tool_list_devices(args: dict) -> dict:
    inv = _devices(args.get("topology"))
    rows = []
    for dev in inv["devices"]:
        port = dev["port"]
        row = dict(dev)
        try:
            info = ec.POOL.with_console(port, lambda s: s.identify())
            row.update(info)
        except ec.DeviceOffline as exc:
            row.update({"prompt": None, "view": "unknown", "state": "offline",
                        "error": str(exc)})
        except ec.ConsoleError as exc:
            row.update({"prompt": None, "view": "unknown", "state": "unreachable",
                        "error": str(exc)})
        row["sysname"] = ec.sysname_from_prompt(row.get("prompt"))
        if not row.get("name"):
            row["name"] = row["sysname"]
        if row["state"] == "silent":
            row["error"] = (
                "the port answered but produced no prompt and no output - it is a "
                "non-VRP endpoint (PC, Cloud, Hub-like) or the console link is stale"
            )
        rows.append(row)
    online = [r["port"] for r in rows if r["state"] == "online"]
    return {
        "count": len(rows),
        "online_ports": online,
        "devices": rows,
        "topology": inv["topology"],
        "name_source": "topology com_port" if inv["topology"] else "prompt sysname (no .topo matched; save the topology for names and models)",
        "hint": "Address devices by port number when two share a name.",
    }


def tool_read(args: dict) -> dict:
    command = str(args["command"]).strip()
    if not command:
        raise ValueError("command must not be empty")
    if not READ_ONLY_RE.match(command):
        raise ValueError(
            f"ensp_read only accepts display/ping/tracert; {command!r} looks like a "
            "configuration command - send it with ensp_exec"
        )
    port = _port_for(args["device"], args.get("topology"))
    limit = _limit(args.get("limit"))
    result = ec.POOL.with_console(
        port, lambda s: s.script([command], timeout=_timeout(args.get("timeout")),
                                 exit_to_user_view=False)
    )
    item = result["results"][0]
    return {
        "port": port,
        "device": ec.name_for_port(port),
        "command": command,
        "prompt": result["end_prompt"],
        "view": result["end_view"],
        "error": item["error"],
        "output": _cap(item["output"], limit),
    }


def tool_exec(args: dict) -> dict:
    commands = [str(c) for c in args["commands"] if str(c).strip()]
    if not commands:
        raise ValueError("commands must contain at least one line")
    blocking = [c for c in commands if DESTRUCTIVE_RE.match(c)]
    if blocking and not args.get("confirm"):
        raise ValueError(
            "these commands persist to flash or destroy state and need confirm=true: "
            + "; ".join(blocking)
        )
    port = _port_for(args["device"], args.get("topology"))
    limit = _limit(args.get("limit"))
    result = ec.POOL.with_console(
        port, lambda s: s.script(commands, timeout=_timeout(args.get("timeout")))
    )
    rows = [
        {
            "command": r["command"],
            "error": r["error"],
            "answered": r["answered"],
            "prompt_after": r["prompt"],
            "output": _cap(r["output"], limit),
        }
        for r in result["results"]
    ]
    return {
        "port": port,
        "device": ec.name_for_port(port),
        "view_before": ec.prompt_view(result["start_prompt"]),
        "view_after": result["end_view"],
        "prompt_after": result["end_prompt"],
        "errors": [r["command"] for r in rows if r["error"]],
        "results": rows,
    }


def tool_backup(args: dict) -> dict:
    port = _port_for(args["device"], args.get("topology"))
    result = ec.POOL.with_console(
        port,
        lambda s: s.script(["display current-configuration"],
                           timeout=_timeout(args.get("timeout"), 60.0)),
    )
    body = result["results"][0]["output"]
    if not body.strip():
        raise ec.ConsoleError(f"port {port} returned an empty configuration")
    name = ec.name_for_port(port)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = args.get("path") or os.path.join(BACKUP_DIR, f"{name}-{stamp}.cfg")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"! backup of port {port} ({name}) at {stamp}\n{body}\n")
    lines = body.count("\n") + 1
    return {
        "port": port,
        "device": name,
        "path": path,
        "lines": lines,
        "chars": len(body),
        "preview": _cap(body, 1200),
    }


def tool_save(args: dict) -> dict:
    if not args.get("confirm"):
        raise ValueError("ensp_save_config writes to device flash; pass confirm=true")
    port = _port_for(args["device"], args.get("topology"))
    result = ec.POOL.with_console(
        port,
        lambda s: s.script(["save"], timeout=_timeout(args.get("timeout"), 45.0),
                           try_pager_off=False),
    )
    item = result["results"][0]
    return {
        "port": port,
        "prompt_after": result["end_prompt"],
        "answered": item["answered"],
        "error": item["error"],
        "output": _cap(item["output"], 2000),
    }


def tool_wait_for(args: dict) -> dict:
    command = str(args["command"]).strip()
    if not READ_ONLY_RE.match(command):
        raise ValueError("ensp_wait_for must poll a read-only command (display/ping)")
    pattern = re.compile(str(args["pattern"]), re.M | re.I)
    port = _port_for(args["device"], args.get("topology"))
    budget = _timeout(args.get("timeout"), 60.0)
    started = time.monotonic()
    deadline = started + budget
    try:
        interval = max(1.0, float(args.get("interval", 3.0)))
    except (TypeError, ValueError):
        interval = 3.0
    want_absent = bool(args.get("wait_until_absent"))
    limit = _limit(args.get("limit"))
    attempts = 0
    last = ""
    while True:
        attempts += 1
        last = ec.POOL.with_console(
            port,
            lambda s: s.script([command], timeout=20.0, exit_to_user_view=False)["results"][0]["output"],
        )
        matched = bool(pattern.search(last))
        elapsed = round(time.monotonic() - started, 1)
        if matched != want_absent:
            return {"port": port, "matched": True, "attempts": attempts,
                    "elapsed_seconds": elapsed, "output": _cap(last, limit)}
        if time.monotonic() >= deadline:
            return {"port": port, "matched": False, "attempts": attempts,
                    "elapsed_seconds": elapsed, "timed_out": True,
                    "last_output": _cap(last, min(limit, 2500))}
        time.sleep(interval)


def tool_release(args: dict) -> dict:
    if args.get("device"):
        port = _port_for(args["device"], args.get("topology"))
        ec.POOL.with_console(port, lambda s: (s.close(), "closed")[1])
        return {"released": [port]}
    ec.POOL.close_all()
    return {"released": "all"}


HANDLERS = {
    "ensp_status": tool_status,
    "ensp_list_devices": tool_list_devices,
    "ensp_read": tool_read,
    "ensp_exec": tool_exec,
    "ensp_backup": tool_backup,
    "ensp_save_config": tool_save,
    "ensp_wait_for": tool_wait_for,
    "ensp_release": tool_release,
}

# ------------------------------------------------------------------- json-rpc layer


def _result(msg_id, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": payload}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(request: dict):
    method = request.get("method")
    msg_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        wanted = params.get("protocolVersion")
        return _result(msg_id, {
            "protocolVersion": wanted if wanted in SUPPORTED_PROTOCOLS else FALLBACK_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method in ("ping",):
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return _error(msg_id, -32602, f"unknown tool {name!r}")
        arguments = params.get("arguments") or {}
        data = None
        try:
            data = handler(arguments)
            text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
            ok = True
        except (ec.ConsoleError, ValueError, re.error, OSError) as exc:
            text = f"ERROR {type(exc).__name__}: {exc}"
            ok = False
        except Exception as exc:  # keep the server alive; surface the failure to the model
            text = f"ERROR unexpected {type(exc).__name__}: {exc}"
            ok = False
        return _result(msg_id, {
            "content": [{"type": "text", "text": text}],
            "isError": not ok,
            "structuredContent": data if ok else None,
        })
    if method == "shutdown":
        ec.POOL.close_all()
        return _result(msg_id, {})
    if msg_id is None:                      # notifications need no reply
        return None
    return _error(msg_id, -32601, f"method not found: {method}")


def main() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            reply = _error(None, -32700, f"invalid JSON: {exc}")
        else:
            if isinstance(request, list):
                reply = [r for r in (handle(item) for item in request) if r is not None]
                if not reply:
                    continue
            else:
                reply = handle(request)
        if reply is None:
            continue
        payload = json.dumps(reply, ensure_ascii=False)
        stdout.write(payload.encode("utf-8") + b"\n")
        stdout.flush()
    ec.POOL.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
