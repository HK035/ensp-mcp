# eNSP Console Channel Facts

All of this was measured on this machine (eNSP 1.3.00.100, Windows 10 19045,
VirtualBox 5.2.30) rather than taken from documentation.

## Process model

| Process | Role | Network surface |
| --- | --- | --- |
| `eNSP_Client.exe` | GUI + console proxy | listens `127.0.0.1:2000..2000+n-1`, `192.168.56.1:63174` |
| `eNSP_Switch.exe` | in-process LSW/CE simulation | `127.0.0.1:10198` |
| `eNSP_VBoxServer.exe` | starts/clones VirtualBox devices | dials each guest `192.168.56.x:40000` |
| `VBoxHeadless.exe` | one per AR/AC/AP/AD/SAP VM | serial console on `\\.\pipe\<device-GUID>` |

AR, AC, AP, AD, SAP and firewall devices are VirtualBox guests; switches and
hubs are simulated inside eNSP. Templates live in
`C:\Program Files\Huawei\eNSP\vboxserver\` (`AR_Base`, `WLAN_AC_Base`,
`WLAN_AP_Base`, `WLAN_AD_Base`, `WLAN_SAP_Base`) and running devices appear as
clones, e.g. `WLAN_AC_Clone_788BF86F-29F9-40d9-9862-40F91F496DC5`. The guest
agent dials the client on `192.168.56.1:63174`; the pipe and that socket are
owned by eNSP, so the only externally usable door is the console proxy.

## Console ports

- Allocated sequentially from 2000 for the devices of the loaded topology, and
  they keep listening while eNSP runs even if the device is powered off.
- No authentication, no telnet negotiation: a plain TCP socket lands directly on
  the VRP console. The GUI's own CLI window is just another client of the same
  port (`eNSP_Client` connects to itself), so slots are shared with the human.
- `##\x00` (two hashes and a NUL) is the proxy's reply when the device has no
  live console. It appears when a command line is sent; a bare CR to a powered-off
  VirtualBox device simply returns nothing, which is why `ensp_list_devices`
  falls back to one `display clock` probe per port.

## Device names: the .topo file

`File > Save` writes XML where each device carries its console port:

```xml
<topo version="1.2.00.300">
  <devices>
    <dev id="228888F9-F114-4439-9A0F-E708132E4B39" name="R1" model="AR2220"
         settings="" com_port="2000" bootmode="1" cx="297.0" cy="368.0">
      <slot number="slot17" isMainBoard="1">
        <interface sztype="Ethernet" interfacename="GE" count="1" />
      </slot>
    </dev>
  </devices>
  <lines>
    <line srcDeviceID="..." destDeviceID="...">
      <interfacePair lineName="Copper" srcIndex="0" tarIndex="0" ... />
    </line>
  </lines>
</topo>
```

The declaration says `encoding="UNICODE"` but the bytes are plain ASCII/UTF-8;
`ensp_console.parse_topo` rewrites the declaration before feeding ElementTree.
Without a saved topology there is no name/model data, so tools fall back to the
sysname read out of each prompt and cache it as an addressable alias. Ask the
user to save the topology once per lab: it makes `AC1`, `LSW1` and models appear
in every tool response and makes the same session resumable after a restart.

Example files with the schema: `C:\Program Files\Huawei\eNSP\examples\**\*.topo`.

## Wire behaviour

- Replies are GBK; Chinese text and box-drawing lines decode with `errors="replace"`.
- The device echoes the command it received before printing output.
- The prompt is often printed twice in a row (`\r\n<Huawei>\r\n<Huawei>`); the
  reader removes a repeated trailing prompt.
- A console that is idle for ~0.75s after a prompt is done talking. Long output
  keeps flowing, so silence (not a fixed delay) is the completion signal.
- Paging cannot be turned off on a console line, so `---- More ----` must be
  answered with a space, see `vrp-quirks.md`.

## Fragility - read this before debugging

While probing with roughly ten rapid connect/disconnect cycles on ports
2000-2003, `eNSP_VBoxServer.exe` and all `VBoxHeadless.exe` processes exited;
the three running VirtualBox devices (an AC and two APs) went down and stayed
down, while the in-process switch survived. The cause was not isolated, but the
lesson is load-bearing for the tool design:

- sessions are pooled and long-lived, one socket per port;
- a failed port gets a 5s cooldown instead of immediate retries;
- `ensp_list_devices` never opens a socket it can avoid (ports come from `netstat`);
- `ensp_release` closes sockets so the GUI can reclaim a slot;
- per-port locks serialise batches, and `console port N is busy` is reported
  rather than interleaving two writers.

## Knobs (environment variables read by ensp_console.py)

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENSP_HOST` | `127.0.0.1` | console proxy host |
| `ENSP_PORT_BASE` | `2000` | first console port |
| `ENSP_PORT_SPAN` | `32` | how far above the base to look |
| `ENSP_TOPO` | unset | a `.topo` path or folder to search first |
| `ENSP_BACKUP_DIR` | `~/.ensp-mcp/backups` | where `ensp_backup` writes - outside the repo, since configs carry credentials |
| `ENSP_VBOXMANAGE` | autodetected | `VBoxManage.exe`, used only to list running VMs |
