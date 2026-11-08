---
name: ensp-lab
description: Operate Huawei eNSP on this machine through the ensp MCP tools (ensp_status / ensp_list_devices / ensp_read / ensp_exec / ensp_backup / ensp_save_config / ensp_wait_for / ensp_release) or the ensp_cli.py fallback - reading and pushing VRP configuration to live topology devices over their console ports. Use whenever the user mentions eNSP, 华为 eNSP 拓扑, VRP 命令, 路由器/交换机/AC/AP 配置, 无线局域网实验, 设备验证或排错, or asks to check what is running in eNSP.
when_to_use: The task involves a device that is running (or should be running) inside Huawei eNSP on this machine.
argument-hint: <device-name-or-port> <what to do>
---

# eNSP Lab Control

## Overview

eNSP 1.3 gives every device in a loaded topology a raw VRP console on `127.0.0.1:2000+i`
with no authentication, and records that number as `com_port` in the `.topo` file. The
`ensp` MCP tools drive that channel: read state, push configuration, back up, verify.
Everything here is measured on this machine - see `references/console-facts.md` for the
evidence and `references/vrp-quirks.md` for what VRP does differently on eNSP.

Registration lives under `mcpServers` in the Qoder settings file for the current user
(`~/.qoder-cn/settings.json`); `install.py` in the repository writes it and prints the
paths it used. A session only attaches MCP servers when it starts, so if `ensp_*` tools
are missing here, ask the user to open a new session. Otherwise run the identical
handlers from the shell, relative to this skill folder:

```bash
python <skill>/scripts/ensp_cli.py <status|devices|read|exec|backup|save|wait|release> ...
```

## Workflow

1. `ensp_status` first. It reads `tasklist` and `netstat` only, never touches a device.
   It tells you which eNSP processes run, which console ports exist, and whether a
   `.topo` matched. If `eNSP_Client` is missing, ask the user to start eNSP and load the
   topology - there is nothing to attach to until then.
2. `ensp_list_devices` to bind names to ports. Every write is aimed at a port, and two
   devices that were never renamed both answer `<Huawei>`. Confirm the prompt of the
   device you are about to change; if it is not the one the user means, stop and ask.
3. `ensp_backup` before a configuration batch. It returns a `.cfg` path; quote that path
   to the user so a mistake can be rolled back by hand.
4. `ensp_exec` with the whole command list in one call. Order matters to VRP: include
   `system-view` yourself, and end at the view you want to leave the console in.
   Add `confirm=true` only for commands that persist or destroy (`save`, `reset`,
   `delete`, `reboot`, `shutdown`, `clear`); the tool refuses them otherwise.
5. Verify with a read, not with an assumption. `ensp_read` refuses configuration lines,
   so verification cannot accidentally change anything.
6. For asynchronous state (AP joining, BGP up, OSPF full) use `ensp_wait_for` instead of
   sleeping and re-reading by hand.
7. `ensp_release` when finished, and always before the user goes back to the eNSP GUI.

## Rules

- One console slot per port is shared with the GUI CLI window. Tell the user not to type
  in the eNSP CLI windows while a batch runs, and release before handing the mouse back.
- Never open raw sockets to `127.0.0.1:200x` from Bash to "go faster" - the pooled
  sessions exist because rapid connect churn takes `eNSP_VBoxServer.exe` down and kills
  every AR/AC/AP in the topology. Use the tools.
- The console keeps its CLI view between calls, so a batch that starts in `[Device-Vlanif10]`
  continues there. The tools hop back to user view before a batch; if a script assumes a
  view, state it in the command list.
- A device answering `##\x00` is powered off, not broken. Ask the user to power it on in
  eNSP; do not retry in a loop.
- `save` writes to flash and bakes in whatever is currently running, including the user's
  unsaved manual work. Only save when the user asked for it.
- Report what was actually sent and what the device said. When a config must match a lab
  manual, read the manual from disk instead of reconstructing commands from memory.

## Failure triage

| Symptom | Meaning | Do |
| --- | --- | --- |
| `offline sentinel (##\x00)` | VBox device powered off or console link dead | ask the user to power it on, then `ensp_list_devices` |
| `cannot reach console port N` | no device at that port / eNSP closed / port outside 2000-2031 | `ensp_status`, then use a listed port |
| `console port N is busy` | GUI CLI window or another batch holds the slot | ask the user to close that CLI window |
| `no device matches 'X'` | names were never learned | run `ensp_list_devices` or address by port |
| `Wrong parameter found at '^' position` | command syntax not on this VRP build | re-read with `?`; see `references/vrp-quirks.md` |
| output ends mid-line, or is full of prompts | a human typed into the same console | stop, re-read the prompt, re-issue only what is missing |
| `---- More ----` inside output | pager residue | upgrade the toolchain; the reader should page through it |

## Resources

- `references/console-facts.md` - port allocation, `.topo` `com_port`, VirtualBox pipes, the offline sentinel, GUI session sharing, encoding.
- `references/vrp-quirks.md` - eNSP-specific VRP behaviour: console rejects `screen-length`, paging in-band, S5700 command differences, confirmation prompts.
- `scripts/ensp_cli.py` - the same tool handlers without MCP; also the debugging path when a tool errors unexpectedly.
