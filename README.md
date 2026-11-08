# ensp-mcp

Give an AI agent (or your shell) a working pair of hands on **Huawei eNSP**.

eNSP has no API, no plugin interface and no remote control. But version 1.3 turns out
to hand every device in a loaded topology a **raw VRP console on `127.0.0.1:2000+i`
with no authentication**, and to record that port as `com_port` inside the saved
`.topo` file. That is the door this project walks through: a stdio MCP server with 8
tools, a Qoder skill that explains how to use them safely, and a shell fallback with
the same behaviour.

Everything here was measured against eNSP **1.3.00.100** on Windows 10 with
VirtualBox 5.2.30. See [Verification status](#verification-status) for what is proven
and what is reasoned.

```
ensp-mcp/
├── ensp_console.py         console channel: pooled per-port sessions, paging, GBK, .topo inventory
├── ensp_mcp_server.py      MCP stdio server (JSON-RPC 2.0, standard library only)
├── install.py              register the server + link the skill into Qoder (dry-run by default)
├── selftest.py             14 regression checks: handshake, gates, live device I/O
└── skill/ensp-lab/
    ├── SKILL.md            the workflow an agent follows
    ├── references/         console-facts.md, vrp-quirks.md  (measured behaviour, not folklore)
    └── scripts/ensp_cli.py the same tools without MCP
```

## Requirements

- Windows with eNSP 1.3 running and a topology **loaded** (devices powered on).
- Python 3.10+ on PATH or a full path to `python.exe`. No PyPI packages - the whole
  toolchain is standard library, so it installs on an offline lab machine.
- Qoder (or any MCP client) if you want the tools inside an agent session.

## Install

```bash
git clone https://github.com/HK035/ensp-mcp.git
cd ensp-mcp
python install.py            # dry run: shows exactly what would change
python install.py --apply    # writes it
```

`install.py` computes *your* paths - it never hardcodes anyone's home directory. It
merges one entry into the `mcpServers` map of `%USERPROFILE%\.qoder-cn\settings.json`
(backing the file up first, leaving your `providers` and `enabledPlugins` untouched)
and links `skill/ensp-lab` into `%USERPROFILE%\.qoder-cn\skills\ensp-lab`.

**Open a new chat session afterwards.** A session only attaches MCP servers when it
starts, so `ensp_*` tools will not appear in the session you installed from.

Manual route, if you prefer the **Settings -> MCP -> Add server** dialog: it writes the
same file, so either way works. The rules that file's validator enforces, read out of
the IDE bundle so a hand-edit does not fail silently:

| Rule | Error if violated |
| --- | --- |
| stdio entry = `command` + optional `args` / `env` / `cwd` | `MCP_CONFIG_TRANSPORT_REQUIRED` |
| `type` omitted or exactly `"stdio"` | `MCP_CONFIG_TYPE_INVALID` |
| no `enabled` key; only `disabled: true` | `MCP_CONFIG_ENABLED_UNSUPPORTED` |
| `cwd` must be absolute at user scope | `MCP_CONFIG_CWD_ABSOLUTE_REQUIRED` |
| settings file must be a real file, not a symlink | `MCP_CONFIG_SYMLINK_FORBIDDEN` |
| server name: Unicode letters/digits/`._`/space/hyphen | `MCP_CONFIG_ENTRY_KEY_INVALID` |

## Verify

```bash
python selftest.py --device 2001
```

14 checks: JSON-RPC handshake, tool advertisement, inventory, the read-only and
`confirm` gates, name resolution, a live `display clock`, a backup that must contain
no `---- More ----` residue, and a **net-zero** configuration batch (re-applying the
device's own `sysname`) that must leave the running config byte-identical.

Without MCP at all:

```bash
python skill/ensp-lab/scripts/ensp_cli.py status
python skill/ensp-lab/scripts/ensp_cli.py devices
python skill/ensp-lab/scripts/ensp_cli.py read 2001 "display vlan"
python skill/ensp-lab/scripts/ensp_cli.py exec 2001 "system-view" "vlan batch 100"
```

## Tools

| Tool | Purpose | Writes |
| --- | --- | --- |
| `ensp_status` | eNSP processes, listening console ports, matched `.topo`, VirtualBox state, and what to fix next | no - reads `tasklist`/`netstat` only |
| `ensp_list_devices` | port -> name / model / sysname / CLI view, plus online-offline-silent | no |
| `ensp_read` | one `display`/`ping`/`tracert`, paged and cleaned | no |
| `ensp_exec` | a batch of VRP lines, per-command output and error flag | yes, gated |
| `ensp_backup` | running config to `~/.ensp-mcp/backups/<name>-<stamp>.cfg` | local file only |
| `ensp_save_config` | VRP `save`, answering confirmation prompts | yes, `confirm=true` |
| `ensp_wait_for` | poll a read-only command until a regex appears or disappears | no |
| `ensp_release` | hand console slots back to the GUI | no |

## Design notes

These are the findings that shaped the code, each one paid for by observation:

- **Long-lived pooled sockets, one per port, serialised by a lock.** While exploring
  the channel, roughly ten rapid connect/disconnect cycles coincided with
  `eNSP_VBoxServer.exe` and every `VBoxHeadless.exe` exiting, which took an AC and two
  APs down for the rest of the session. Cause was not isolated, but the tool never
  churns: sockets are reused, a failed port gets a 5s cooldown, and port discovery
  comes from `netstat` instead of probing.
- **`##\x00` means "powered off"**, not "broken". A bare CR to a down VirtualBox device
  returns nothing at all, so identification falls back to one harmless `display clock`.
- **The console line cannot disable paging.** `screen-length 0 temporary` answers
  `Error: The type of the included user interface must be VTY.` - only VTY lines support
  it. So the pager is answered with a space in-band and its residue stripped; otherwise
  captured configurations come out corrupt.
- **Replies are GBK, the device echoes every command, and prompts are often printed
  twice.** All three are normalised, and completion is detected by silence after a
  prompt rather than by a fixed delay.
- **Device names only exist once the topology is saved**, because eNSP writes
  `com_port`/`name`/`model` on save. Unsaved topologies still work: the tools learn
  sysnames from prompts and cache them as addressable aliases, deduplicating by port so
  one device seen in three CLI views is not "ambiguous".
- **Writes are gated twice**: `ensp_read` refuses anything that is not
  `display`/`ping`/`tracert`, and `ensp_exec`/`ensp_save_config` require `confirm=true`
  for commands that persist to flash or destroy state.
- **Captured configurations stay out of the repo.** They contain device credentials, so
  the backup directory defaults to `~/.ensp-mcp/backups` and `*.cfg` is gitignored.

## Privacy and safety

The console channel is unauthenticated and shared with the eNSP GUI: anyone or anything
with a local socket to port 2000+ has full administrator access to those simulated
devices. That is fine for an offline lab simulator, which is what eNSP is - but do not
expose the port, and do not commit `backups/*.cfg`. Tell your GUI to keep out of the way
while a batch runs (`ensp_release` gives the slot back), and read
[skill/ensp-lab/SKILL.md](skill/ensp-lab/SKILL.md) before letting an agent drive a lab
unattended.

## Verification status

Honest accounting, because a tool that talks to "your" network deserves it:

- **Proven on real hardware-in-simulation**: an in-process S5700 switch (VRP 5.110
  V200R001C00) - reads, backups, paging, the net-zero config batch, name resolution,
  offline detection on three powered-off VirtualBox devices, and the gate rejections.
- **Not proven**: VirtualBox-backed AR/AC/AP consoles were only reachable for a few
  exploratory prompts before those devices went down (see the churn note above), so the
  write path is verified only on the in-process switch. `ensp_save_config` has never
  been run against a device here - saving would have committed someone else's unsaved
  work - so its prompt handling is reasoned from VRP behaviour, not observed.
- **Single-machine, single-version.** Port 2000 allocation, the `##\x00` sentinel and the
  `screen-length` rejection are properties of eNSP 1.3.00.100 as installed here.

## Not covered

- Building or editing a topology. eNSP has no API for it, a hand-written `.topo` is not
  hot-reloaded, and `VBoxManage` competes with eNSP for the same VMs. Draw it by hand.
- PC / Server / station endpoints are GUI plugins without a VRP console; they answer
  `silent`. Test reachability from a router or switch inside the topology instead.

## License

MIT - see [LICENSE](LICENSE).
