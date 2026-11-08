# VRP Behaviour Specific to eNSP

Two kinds of statements below. **Measured** means observed on this machine through
the console channel. **Expected** means standard Huawei VRP behaviour that this
lab has not yet needed to confirm - verify with a read before relying on it.

## Paging (measured)

`screen-length` is a VTY-only command. Over a console port the S5700 answers:

```
Error: The type of the included user interface must be VTY.
```

so `screen-length 0 temporary` and `screen-length disable` both fail there. Long
output must be paged in-band: the device stops at `---- More ----` and waits for
a space. When it resumes it overwrites the marker with spaces and continues the
same logical line, which is why residue looks like:

```
  ---- More ----                                          interface Vlanif1
```

`ensp_console` answers the marker with a space and strips marker plus padding on
the way out. If you ever see a marker inside a tool response, the reader is
broken - report it rather than post-processing the text.

## Command syntax traps (measured)

- `display vlan brief` does not exist on `VRP (R) software, Version 5.110
  (S5700 V200R001C00)`; use `display vlan`. The device replies with a caret
  ruler plus `Error: Wrong parameter found at '^' position.`, and the tools flag
  `error: true` for such a result, so never treat a batch as clean without
  reading `errors`.
- The AC's system view prompt is `[AC1]`; a VLAN view is `[SW1-vlan999]`;
  an interface view is `[SW1-GigabitEthernet0/0/3]`. Only `<name>` is user
  view, and `sysname` extraction must drop the trailing qualifier.
- A freshly booted VirtualBox device prints `Info: You are advised to change the
  password to ensure security.` before its first prompt. That is normal, not a
  failure.
- The console keeps its view between connections. A batch that starts while a
  human left the console in interface view would configure the wrong node, which
  is why `ensp_exec` returns to user view first and reports `view_before`.
- `undo vlan 999` (measured) applied with no confirmation prompt. Do not assume
  `undo` asks; assume `save`, `reset` and `reboot` do.
- First login on an AR router may show the same password advisory; neither
  requires input, so do not wait for a prompt that will not come.

## Sending discipline

- Send complete command lines. Do not send partial lines, backspaces, Tab
  completion or arrow keys - this is a real console, so line editing state
  persists and half-editing corrupts the next command.
- One command per line, no `;` chaining: VRP has no command chaining.
- Nothing on the console is echoed "privately": the device echoes every line, so
  anything a human types in the GUI CLI window while a batch runs appears inside
  the tool's captured output. If output suddenly contains unexpected commands,
  stop and re-read the prompt instead of replaying the batch.

## Verification commands worth using instead of guessing

Read-only, so `ensp_read` accepts them directly:

- `display current-configuration` - what is actually running (compare with a
  backup file rather than with memory).
- `display vlan`, `display interface brief`, `interface X; display this`
- `display ap all` / `display station all` on an AC (**expected** on AC6005/AC6605
  images; if the build rejects them, fall back to `display wlan` and `?` help).
- `display capap statistics` / `display license` when an AP will not join (**expected**).
- `display clock` - cheapest liveness probe, used by device identification.

For a lab hand-in, prefer the manual in the project folder over recall: the
wlan lab keeps `《无线局域网组建与优化》实验指导书.pdf` next to the report, and
eNSP devices speak the same Huawei syntax the guide uses (unlike HCL, which is
Comware and needs command translation).
