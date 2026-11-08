"""Handshake and channel selftest for the eNSP MCP server.

Run it any time the server stops answering in Qoder:
    python selftest.py [--device 2001] [--command "display version"]

Speaks the same newline-delimited JSON-RPC a MCP client speaks, so a failure
here localises to the server rather than to the IDE.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "ensp_mcp_server.py")


class Client:
    def __init__(self, python: str | None = None):
        self.proc = subprocess.Popen(
            [python or sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
        )
        self._id = 0

    def request(self, method: str, params: dict | None = None, timeout: float = 90.0):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed the pipe")
            reply = json.loads(line.decode("utf-8"))
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply["result"]
        raise TimeoutError(f"{method} timed out after {timeout}s")

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def call(self, tool: str, arguments: dict | None = None, timeout: float = 90.0):
        result = self.request("tools/call", {"name": tool, "arguments": arguments or {}}, timeout)
        text = result["content"][0]["text"]
        return result.get("isError", False), text

    def close(self) -> None:
        try:
            self.request("shutdown", timeout=5)
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", help="console port or name to run a live read against")
    parser.add_argument("--command", default="display clock")
    args = parser.parse_args()

    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"\n        {detail}" if detail else ""))

    client = Client()
    try:
        init = client.request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "selftest", "version": "1"},
        })
        check("initialize", init.get("serverInfo", {}).get("name") == "ensp",
              f"protocol={init.get('protocolVersion')} tools-advertised="
              f"{'tools' in init.get('capabilities', {})}")
        client.notify("notifications/initialized")

        tools = client.request("tools/list")["tools"]
        names = [t["name"] for t in tools]
        expected = {"ensp_status", "ensp_list_devices", "ensp_read", "ensp_exec",
                    "ensp_backup", "ensp_save_config", "ensp_wait_for", "ensp_release"}
        check("tools/list advertises every tool", expected <= set(names), f"got {names}")
        schemas_ok = all(t.get("inputSchema", {}).get("type") == "object" for t in tools)
        check("every tool carries an object inputSchema", schemas_ok)

        err, status_text = client.call("ensp_status")
        check("ensp_status", not err, status_text[:600])

        err, text = client.call("ensp_list_devices")
        check("ensp_list_devices", not err, text[:800])

        err, text = client.call("ensp_read", {"device": "9999", "command": "display clock"})
        check("unknown device is refused, not crashed", err, text[:200])
        err, text = client.call("ensp_read", {"device": args.device or "2001", "command": "interface GigabitEthernet0/0/1"})
        check("ensp_read rejects a configuration command", err, text[:200])
        err, text = client.call("ensp_exec", {"device": args.device or "2001", "commands": ["save"]})
        check("ensp_exec requires confirm for save", err, text[:200])

        try:
            live = json.loads(status_text)
        except json.JSONDecodeError:
            live = {}
        ports = live.get("listening_ports") or []
        target = args.device or (str(ports[0]) if ports else "2001")
        err, text = client.call("ensp_read", {"device": target, "command": args.command})
        blocked = err and ("offline" in text.lower() or "cannot reach console" in text.lower())
        if blocked:
            print("[SKIP] 5 device-dependent checks - "
                  + (f"no console ports are listening: start eNSP and load a topology"
                     if not ports else f"port {target} is powered off"))
            print(f"       ({text.strip()[:160]})")
        else:
            check(f"live read '{args.command}' on {target}", not err, text[:700])
            err, text = client.call("ensp_backup", {"device": target}, timeout=180)
            if err:
                check("ensp_backup", False, text[:300])
            else:
                info = json.loads(text)
                body = open(info["path"], encoding="utf-8").read()
                check("ensp_backup writes a config with no pager residue",
                      info["lines"] > 5 and "More" not in body,
                      f"{info['device']} -> {info['path']} ({info['lines']} lines, "
                      f"{body.count('More')} More artifacts)")
                named = info["device"]
                if named and not named.startswith("port"):
                    err, text = client.call("ensp_read", {"device": named, "command": args.command})
                    check(f"addressing by learned name {named!r}", not err, text[:200])

            # config path, net-zero: re-apply the sysname the device already has
            err, text = client.call("ensp_read", {"device": target,
                                                  "command": "display current-configuration | include sysname"})
            if err:
                check("config path precondition (read sysname)", False, text[:200])
            else:
                found = re.search(r"^\s*sysname\s+(\S+)", json.loads(text)["output"], re.M)
                if not found:
                    check("config path precondition (sysname present in config)", False,
                          json.loads(text)["output"][:200])
                else:
                    current = found.group(1)
                    err, text = client.call("ensp_exec", {
                        "device": target, "commands": ["system-view", f"sysname {current}"]},
                        timeout=120)
                    if err:
                        check("ensp_exec batch (net-zero sysname re-apply)", False, text[:400])
                    else:
                        run = json.loads(text)
                        ok = (not run["errors"] and run["view_after"] == "system-view"
                              and run["prompt_after"] == f"[{current}]")
                        check("ensp_exec batch (net-zero sysname re-apply)", ok,
                              f"view {run['view_before']} -> {run['view_after']}, "
                              f"prompt {run['prompt_after']}, errors {run['errors']}")
                        err, text = client.call("ensp_read", {
                            "device": target,
                            "command": "display current-configuration | include sysname"})
                        check("running config unchanged after the batch",
                              not err and f"sysname {current}" in json.loads(text)["output"],
                              text[:200])

        err, text = client.call("ensp_release")
        check("ensp_release", not err, text[:120])
    finally:
        client.close()

    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
