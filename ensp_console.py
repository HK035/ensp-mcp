"""Console channel for Huawei eNSP: one VRP console per TCP port on 127.0.0.1:2000+.

Stdlib only. eNSP 1.3 allocates a console port per topology device (recorded as
com_port in the .topo XML) and proxies it without authentication, so a console
session here is the same byte pipe the GUI CLI window uses.

Channel facts this module is built around (measured on eNSP 1.3.00.100):
  - console replies are GBK, echo is on, and the prompt is often repeated
  - AR/AC/AP run in VirtualBox; while the VM is down the proxy answers "##\\x00"
  - the proxy is fragile under connection churn, so sessions are long-lived
"""

from __future__ import annotations

import csv
import io
import os
import re
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

HOST = os.environ.get("ENSP_HOST", "127.0.0.1")
PORT_BASE = int(os.environ.get("ENSP_PORT_BASE", "2000"))
PORT_SPAN = int(os.environ.get("ENSP_PORT_SPAN", "32"))
IDLE_ROUNDS = 3          # ~0.75s of silence means the device stopped talking
MAX_IDLE_ROUNDS = 12
MAX_PAGES = 600          # guard against a device that never stops paginating
RECV_SLICE = 0.25
OUTPUT_CAP = 20000

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[0-9A-Za-z]|\x1b[()][0-9A-Za-z]|\x1b[=>]")
MORE_MARKER = r"[ \t]*-{2,}[ \t]*[Mm]ore[ \t]*-{2,}[ \t]*"
MORE_ANYWHERE_RE = re.compile(MORE_MARKER)
PROMPT_NAME = r"[A-Za-z0-9_.\-:/\\]+"
PROMPT_TAIL_RE = re.compile(rf"(?:<({PROMPT_NAME})>|\[({PROMPT_NAME})\])[ \t\r\n]*$")
CONFIRM_TAIL_RE = re.compile(
    r"(?:\[(?:Y/N|y/n|yes/no)\]|\(y/c\)|[Aa]re you sure[^\n\r]*)[^\n\r]*$"
)
ERROR_RE = re.compile(
    r"^\s*(?:Error|%%|Unrecognized command|Wrong parameter|Incomplete command|"
    r"Illegal input|Too many parameters|Too few parameters|Permission denied|"
    r"Bad command|Invalid|Error too many|Warning: This command is not allowed)",
    re.M,
)
# one attempt only: the console line rejects it, and _read pages in-band anyway
PAGER_CANDIDATES = ("screen-length 0 temporary",)


class ConsoleError(RuntimeError):
    """Channel level failure: unreachable port, dead socket, wedged session."""


class DeviceOffline(ConsoleError):
    """eNSP answered the offline sentinel: the device is not powered on."""

    def __init__(self, port: int):
        super().__init__(
            f"port {port} returned the offline sentinel (##\\x00): the device is "
            "powered off or its console link is down. Power it on in the eNSP GUI "
            "(or reboot the device) and retry."
        )
        self.port = port


def decode(raw: bytes) -> str:
    return raw.decode("gbk", "replace")


def scrub(text: str) -> str:
    """Remove ANSI/More artifacts and normalise line endings."""
    text = ANSI_RE.sub("", text).replace("\x08", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return MORE_ANYWHERE_RE.sub("", text)


def waiting_for_more(text: str) -> bool:
    """True when the last line is a pager prompt the device is blocked on."""
    return bool(MORE_ANYWHERE_RE.search(text.rsplit("\n", 1)[-1]))


def split_lines(text: str) -> list[str]:
    return [ln for ln in text.split("\n")]


def drop_trailing_prompt(text: str) -> tuple[str, str | None]:
    """Return (body, prompt) after removing the trailing prompt from text."""
    m = PROMPT_TAIL_RE.search(text)
    if not m:
        return text, None
    prompt = m.group(0).strip()
    body = text[: m.start()]
    # the device often prints the prompt twice in a row
    while True:
        m2 = PROMPT_TAIL_RE.search(body)
        if m2 and m2.group(0).strip() == prompt:
            body = body[: m2.start()]
            continue
        break
    return body.rstrip("\n"), prompt


def drop_echo(text: str, command: str) -> str:
    """Remove the echoed command (VRP echoes input on the console)."""
    cmd = command.strip()
    if not cmd:
        return text.lstrip("\n")
    lines = split_lines(text)
    for i, line in enumerate(lines[:4]):
        if line.strip().endswith(cmd) or line.strip() == cmd:
            return "\n".join(lines[i + 1 :]).lstrip("\n")
    return text.lstrip("\n")


IFACE_IN_PROMPT_RE = re.compile(
    r"-(GigabitEthernet|GE|XGE|Eth-Trunk|Port-group|Vlanif|LoopBack|"
    r"WLAN|NULL|Pos|Serial|MEth|Bri|Virtual-Template|Cellular|vlan\s*\d+)\S*$",
    re.I,
)


def sysname_from_prompt(prompt: str | None) -> str:
    """Pull the device name out of a prompt, dropping any interface suffix."""
    if not prompt:
        return ""
    inner = prompt.strip("<>[]").strip()
    if prompt.startswith("<"):
        return inner
    return IFACE_IN_PROMPT_RE.sub("", inner)


def prompt_view(prompt: str | None) -> str:
    if not prompt:
        return "unknown"
    if prompt.startswith("<"):
        return "user-view"
    if prompt.startswith("["):
        inner = prompt.strip("[]")
        if re.search(r"(GigabitEthernet|GE|XGE|Eth-Trunk|Vlanif|LoopBack|WLAN|NULL|Pos|Serial|MEth)", inner, re.I):
            return "interface-view"
        return "system-view"
    return "unknown"


def _run(args: list[str]) -> str:
    try:
        p = subprocess.run(args, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (p.stdout or b"").decode("gbk", "replace") + (p.stderr or b"").decode("gbk", "replace")


# ------------------------------------------------------------------------ inventory


def processes() -> dict[str, int]:
    out = _run(["tasklist", "/FO", "CSV", "/NH"])
    wanted = {"ensp_client": "client", "ensp_switch": "switch", "ensp_vboxserver": "vboxserver"}
    found: dict[str, int] = {}
    for parts in csv.reader(io.StringIO(out), delimiter=",", quotechar='"'):
        if len(parts) < 2:
            continue
        key = parts[0].strip().lower()[:-4]
        if key in wanted and wanted[key] not in found:
            found[wanted[key]] = _int(parts[1])
    if "VBoxHeadless.exe" in out:
        found["vms"] = out.lower().count("vboxheadless.exe")
    return found


def _int(value: str) -> int:
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else 0


def listening_console_ports() -> list[int]:
    """Console ports that are LISTENING, read from netstat only.

    Never probe by connecting: a stray handshake knocks the GUI's own console
    session off the device and can take the VirtualBox server down.
    """
    out = _run(["netstat", "-ano", "-p", "tcp"])
    ports: set[int] = set()
    for line in out.splitlines():
        m = re.match(r"\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", line, re.I)
        if not m:
            continue
        port = int(m.group(1))
        if PORT_BASE <= port < PORT_BASE + PORT_SPAN:
            ports.add(port)
    return sorted(ports)


def _vboxmanage() -> str | None:
    """Locate VBoxManage without assuming one machine's install folder."""
    candidates = [os.environ.get("ENSP_VBOXMANAGE")]
    base = os.environ.get("VBoxInstallPath", r"C:\Program Files\Oracle\VirtualBox")
    candidates += [os.path.join(base, "VBoxManage.exe"),
                   r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def vbox_running_vms() -> list[str]:
    vbmanage = _vboxmanage()
    if not vbmanage:
        return []
    out = _run([vbmanage, "list", "runningvms"])
    return re.findall(r'"([^"]+)"', out)


def find_topo_files(limit: int = 10) -> list[str]:
    roots = [p for p in (os.environ.get("ENSP_TOPO"), os.environ.get("USERPROFILE", "")) if p]
    found: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if os.path.isfile(root):
            if root.lower().endswith(".topo") and root not in seen:
                seen.add(root)
                found.append(root)
            continue
        if not os.path.isdir(root):
            continue
        for path in _walk_topo(root):
            if path not in seen:
                seen.add(path)
                found.append(path)
    found.sort(key=_mtime, reverse=True)
    return found[:limit]


def _walk_topo(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath[len(root) :].count(os.sep) >= 4:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d.lower() not in {
            "appdata", "node_modules", ".git", ".venv", "venv", "__pycache__", ".qoder-cn",
        }]
        out += [os.path.join(dirpath, fn) for fn in filenames if fn.lower().endswith(".topo")]
    return out


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def parse_topo(path: str) -> dict[str, object]:
    """Map console port -> device from a .topo file (<dev com_port=.. name=.. model=..>)."""
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8", "replace")
    text = re.sub(r'encoding="[^"]*"', 'encoding="utf-8"', text, count=1)
    root = ET.fromstring(text)
    devices: dict[int, dict[str, str]] = {}
    for dev in root.iter("dev"):
        port = _int(dev.get("com_port", ""))
        if not port:
            continue
        devices[port] = {
            "name": dev.get("name", ""),
            "model": dev.get("model", ""),
            "id": dev.get("id", ""),
        }
    lines = sum(1 for _ in root.iter("line"))
    return {"path": path, "version": root.get("version", ""), "devices": devices, "links": lines}


def inventory(topo_path: str | None = None) -> dict[str, object]:
    """Device inventory: listening ports plus names/models from the newest topology."""
    ports = listening_console_ports()
    topo = None
    if topo_path:
        topo = parse_topo(topo_path)
    else:
        for candidate in find_topo_files(limit=3):
            try:
                parsed = parse_topo(candidate)
            except Exception:
                continue
            if any(p in ports for p in parsed["devices"]):
                topo = parsed
                break
    unmapped = list(ports)
    devices = []
    if topo:
        for port, meta in sorted(topo["devices"].items()):
            if port in ports:
                devices.append({"port": port, **meta, "listening": True})
                if port in unmapped:
                    unmapped.remove(port)
    for port in unmapped:
        devices.append({"port": port, "name": "", "model": "", "id": "", "listening": True})
    return {
        "processes": processes(),
        "listening_ports": ports,
        "topology": {k: v for k, v in (topo or {}).items() if k != "devices"} if topo else None,
        "devices": devices,
        "unmapped_ports": unmapped,
        "running_vms": vbox_running_vms(),
    }


# ------------------------------------------------------------------------ sessions


class Console:
    def __init__(self, port: int, host: str = HOST):
        self.port = port
        self.host = host
        self.sock: socket.socket | None = None
        self.last_prompt: str | None = None
        self.pager_checked = False
        self.pager_ok = False

    # -- plumbing

    def connect(self) -> None:
        if self.sock is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port), timeout=RECV_SLICE)
        except OSError as exc:
            raise ConsoleError(f"cannot reach console port {self.port}: {exc}") from exc
        sock.settimeout(RECV_SLICE)
        self.sock = sock
        banner = self._read(timeout=1.5, stop_on_idle=True)
        self._check_offline(banner)
        _, prompt = drop_trailing_prompt(banner)
        self._note_prompt(prompt)
        # wake the line: a bare CR is what a human terminal would send
        self._send_raw(b"\r")
        wake = self._read(timeout=2.0, stop_on_idle=True)
        self._check_offline(wake)
        _, prompt = drop_trailing_prompt(wake)
        self._note_prompt(prompt)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None
                self.pager_checked = False
                self.pager_ok = False
                self.last_prompt = None

    def _check_offline(self, text: str) -> None:
        if "\x00" in text:
            raise DeviceOffline(self.port)

    def _send_raw(self, data: bytes) -> None:
        if self.sock is None:
            raise ConsoleError(f"session on port {self.port} is not open")
        try:
            self.sock.sendall(data)
        except OSError as exc:
            self.close()
            raise ConsoleError(f"send failed on port {self.port}: {exc}") from exc

    def _read(self, timeout: float, stop_on_idle: bool) -> str:
        assert self.sock is not None
        buf = bytearray()
        pages = 0
        deadline = time.monotonic() + timeout
        idle = 0
        while time.monotonic() < deadline:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                chunk = b""
            except OSError as exc:
                self.close()
                raise ConsoleError(f"recv failed on port {self.port}: {exc}") from exc
            if chunk:
                buf += chunk
                idle = 0
                if waiting_for_more(decode(bytes(buf))[-140:]) and pages < MAX_PAGES:
                    pages += 1
                    self._send_raw(b" ")        # console lines cannot disable the pager
                continue
            idle += 1
            if idle >= MAX_IDLE_ROUNDS:
                break
            if idle >= IDLE_ROUNDS and stop_on_idle:
                text = decode(bytes(buf))
                if text.strip() and PROMPT_TAIL_RE.search(text.rstrip()):
                    break
            if idle >= 2 * IDLE_ROUNDS and stop_on_idle and not buf.strip():
                break
        return scrub(decode(bytes(buf)))

    # -- behaviour

    def _note_prompt(self, prompt: str | None) -> str | None:
        if prompt:
            self.last_prompt = prompt
            remember_alias(sysname_from_prompt(prompt), self.port)
        return prompt

    def read_prompt(self, timeout: float = 3.0) -> str | None:
        if self.sock is None:
            self.connect()
        else:
            self._send_raw(b"\r")
        text = self._read(timeout=timeout, stop_on_idle=True)
        self._check_offline(text)
        _, prompt = drop_trailing_prompt(text)
        return self._note_prompt(prompt)

    def identify(self, timeout: float = 6.0) -> dict:
        """Classify a port without configuring anything.

        A bare CR is not enough: while a VirtualBox device is powered off the
        proxy stays silent on CR and only answers the offline sentinel once a
        command line is sent, so fall back to one harmless read-only command.
        """
        if self.sock is None:
            self.connect()
        prompt = self.read_prompt(timeout=min(2.0, timeout))
        if prompt:
            return {"prompt": prompt, "view": prompt_view(prompt),
                    "state": "online", "probe": "carriage-return"}
        result = self.send("display clock", timeout=timeout)
        prompt = result["prompt"] or self.read_prompt(timeout=2.0)
        if not prompt:
            return {"prompt": None, "view": "unknown", "state": "silent", "probe": "display clock"}
        return {"prompt": prompt, "view": prompt_view(prompt),
                "state": "online", "probe": "display clock"}

    def try_disable_pager(self) -> bool:
        """Best effort: eNSP's console line rejects screen-length, so _read pages in-band.

        Measured on eNSP 1.3: "screen-length 0 temporary" over a console port answers
        "Error: The type of the included user interface must be VTY." - only VTY lines
        support it. Trying once per session keeps that noise off every command.
        """
        if self.pager_checked:
            return self.pager_ok
        self.pager_checked = True
        prompt = self.last_prompt or self.read_prompt()
        if not prompt or prompt.startswith("["):
            return False                       # user-view command only
        for command in PAGER_CANDIDATES:
            if not ERROR_RE.search(self.send(command)["output"]):
                self.pager_ok = True
                break
        return self.pager_ok

    def to_user_view(self, max_quit: int = 8) -> str | None:
        prompt = self.last_prompt or self.read_prompt()
        quits = 0
        while prompt and prompt.startswith("[") and quits < max_quit:
            result = self.send("quit", wait_for_prompt=True)
            prompt = result["prompt"] or self.read_prompt()
            quits += 1
        return self._note_prompt(prompt) or self.last_prompt

    def send(self, command: str, timeout: float = 20.0, wait_for_prompt: bool = True,
             answer: str | None = "y") -> dict[str, object]:
        """Run one line and collect everything the device says back."""
        if self.sock is None:
            self.connect()
        self._send_raw((command + "\r\n").encode("gbk", "replace"))
        text = self._read(timeout=timeout, stop_on_idle=wait_for_prompt)
        self._check_offline(text)
        answered = []
        # a config line can end in a confirmation prompt instead of a prompt
        for _ in range(3):
            if not answer or not CONFIRM_TAIL_RE.search(text.rstrip()[-160:]):
                break
            tail = text.rstrip()
            answered.append(CONFIRM_TAIL_RE.search(tail[-160:]).group(0).strip())
            self._send_raw((answer + "\r\n").encode("gbk", "replace"))
            text += self._read(timeout=timeout, stop_on_idle=wait_for_prompt)
        body, prompt = drop_trailing_prompt(text)
        body = drop_echo(body, command)
        self._note_prompt(prompt)
        return {
            "command": command,
            "output": body.strip("\n"),
            "prompt": prompt,
            "answered": answered,
            "error": bool(ERROR_RE.search(body)),
        }

    def script(self, commands: list[str], timeout: float = 20.0,
               exit_to_user_view: bool = True, try_pager_off: bool = True) -> dict[str, object]:
        if self.sock is None:
            self.connect()
        start_prompt = self.last_prompt or self.read_prompt()
        if exit_to_user_view:
            self.to_user_view()
        if try_pager_off:
            self.try_disable_pager()
        results = [self.send(cmd, timeout=timeout) for cmd in commands]
        return {
            "port": self.port,
            "start_prompt": start_prompt,
            "end_prompt": self.last_prompt,
            "end_view": prompt_view(self.last_prompt),
            "results": results,
        }


class Pool:
    """One long-lived console per port, serialised by a lock.

    Churn is what wedges eNSP: never open a second socket to a port that already
    has one, and cool down after a failure instead of retrying in a loop.
    """

    def __init__(self, cooldown: float = 5.0):
        self.cooldown = cooldown
        self._sessions: dict[int, Console] = {}
        self._locks: dict[int, threading.Lock] = {}
        self._failed_at: dict[int, float] = {}
        self._guard = threading.Lock()

    def _lock(self, port: int) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(port, threading.Lock())

    def with_console(self, port: int, fn):
        lock = self._lock(port)
        if not lock.acquire(timeout=90):
            raise ConsoleError(
                f"console port {port} is busy (another command, or the eNSP GUI "
                "CLI window, still holds it)"
            )
        try:
            failed_at = self._failed_at.get(port, 0.0)
            wait = self.cooldown - (time.monotonic() - failed_at)
            if wait > 0:
                time.sleep(wait)
            session = self._sessions.get(port)
            if session is None:
                session = Console(port)
                self._sessions[port] = session
            try:
                return fn(session)
            except ConsoleError as exc:
                # always free the slot: a lingering socket blocks the GUI console
                # of a device that comes back up later
                session.close()
                if not isinstance(exc, DeviceOffline):
                    self._failed_at[port] = time.monotonic()
                raise
        finally:
            lock.release()

    def close_all(self) -> None:
        for session in list(self._sessions.values()):
            session.close()
        self._sessions.clear()


POOL = Pool()


_ALIASES: dict[str, int] = {}
_ALIAS_DISPLAY: dict[str, str] = {}
_ALIAS_LOCK = threading.Lock()


def remember_alias(name: str, port: int) -> None:
    """Cache a name seen on the wire, so a device is addressable without a .topo."""
    if not name:
        return
    with _ALIAS_LOCK:
        _ALIASES.setdefault(name.lower(), port)
        _ALIAS_DISPLAY.setdefault(name.lower(), name)


def alias_table() -> dict[str, int]:
    with _ALIAS_LOCK:
        return dict(_ALIASES)


def name_for_port(port: int, devices: list[dict] | None = None) -> str:
    """Human label for a port: topology name, then a name seen on the wire."""
    for dev in devices or []:
        if int(dev.get("port", -1)) == port and dev.get("name"):
            return str(dev["name"])
    with _ALIAS_LOCK:
        candidates = [(_ALIAS_DISPLAY.get(key, key), key)
                      for key, target in _ALIASES.items() if target == port]
    if not candidates:
        return f"port{port}"
    return min(candidates, key=len)[0]


def resolve(target: str | int, devices: list[dict]) -> int:
    """Accept a port number, an exact device name, or a unique name substring."""
    text = str(target).strip()
    if re.fullmatch(r"\d{4,5}", text):
        return int(text)
    named: dict[str, int] = {}
    for dev in devices:
        if dev.get("name"):
            named.setdefault(str(dev["name"]).lower(), int(dev["port"]))
    for name, port in alias_table().items():
        named.setdefault(name, port)
    ports = listening_console_ports()

    def describe(matches: dict[str, int]) -> str:
        return ", ".join(f"{name}@{port}" for name, port in sorted(matches.items()))

    def pick(matches: dict[str, int]) -> tuple[bool, int]:
        """True when every candidate name points at one and the same device."""
        ports = sorted(set(matches.values()))
        return (len(ports) == 1), (ports[0] if len(ports) == 1 else -1)

    exact = {n: p for n, p in named.items() if n == text.lower()}
    ok, port = pick(exact)
    if ok:
        return port
    partial = {n: p for n, p in named.items() if text.lower() in n}
    ok, port = pick(partial)
    if ok:
        return port
    if partial:
        raise ConsoleError(
            f"{text!r} matches more than one device: {describe(partial)}; "
            "use the port number"
        )
    raise ConsoleError(
        f"no device matches {text!r}. Known names: {describe(named) or 'none yet'}. "
        f"Listening console ports: {ports}. Run ensp_list_devices to bind names to "
        "ports, or save the topology in eNSP so com_port supplies them."
    )
