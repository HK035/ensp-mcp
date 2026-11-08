"""Register the ensp MCP server and install the ensp-lab skill into Qoder.

    python install.py             # dry run - prints every change, touches nothing
    python install.py --apply     # apply
    python install.py --apply --force   # also replace a foreign skill link

Why a script: the config that has to change lives in the caller's own home
directory, so nothing here may hardcode one machine's paths. It is idempotent -
run it again after moving the folder and it rewrites the paths.

Exit codes: 0 success or nothing to do, 1 refused (needs --force), 2 blocked
(missing file, bad JSON, link conflict).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

REPO = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(REPO, "ensp_mcp_server.py")
SKILL_SRC = os.path.join(REPO, "skill", "ensp-lab")
SKILL_NAME = "ensp-lab"


def qoder_dir() -> str:
    override = os.environ.get("ENSP_QODER_DIR")
    if override:
        return override
    home = os.path.expanduser("~")
    for name in (".qoder-cn", ".qoder"):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(home, ".qoder-cn")


def plan_settings(qdir: str, name: str, python: str) -> tuple[str, dict, list[str], bool]:
    """Return (path, desired full document, human lines, changed)."""
    path = os.path.join(qdir, "settings.json")
    entry = {"command": python, "args": [SERVER]}
    if not os.path.isfile(path):
        doc = {"mcpServers": {name: entry}}
        return path, doc, [f"create {path} with mcpServers.{name}"], True

    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        doc = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"REFUSED {path} is not valid JSON ({exc}); fix it or point "
                         f"--qoder-dir elsewhere")
    if not isinstance(doc, dict):
        raise SystemExit(f"REFUSED {path} is not a JSON object")

    servers = doc.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise SystemExit(f"REFUSED {path} has a non-object mcpServers key")
    servers = dict(servers or {})
    current = servers.get(name)
    if current == entry:
        return path, doc, [f"{path}: mcpServers.{name} already correct"], False
    lines = []
    if current is None:
        lines.append(f"{path}: add mcpServers.{name}")
    else:
        lines.append(f"{path}: update mcpServers.{name} (was {json.dumps(current, ensure_ascii=False)[:120]})")
    if isinstance(current, dict) and "enabled" in current:
        lines.append(f"  note: dropping 'enabled' - the IDE validator rejects it (only 'disabled' is allowed)")
    servers[name] = {k: v for k, v in current.items() if k not in ("command", "args", "enabled")} \
        if isinstance(current, dict) else {}
    servers[name].update(entry)
    doc["mcpServers"] = servers
    other = [k for k in doc if k != "mcpServers"]
    lines.append(f"  preserved top-level keys: {', '.join(sorted(other)) or 'none'}")
    return path, doc, lines, True


def plan_skill(qdir: str, force: bool) -> tuple[str, list[str], bool, int]:
    """Return (link path, lines, should_link, rc)."""
    link = os.path.join(qdir, "skills", SKILL_NAME)
    if os.path.islink(link) or os.path.isdir(link):
        resolved = os.path.realpath(link)
        if os.path.normcase(resolved) == os.path.normcase(os.path.realpath(SKILL_SRC)):
            return link, [f"{link}: skill already linked to this folder"], False, 0
        detail = f"-> {resolved}" if os.path.islink(link) else "a real directory (not a link)"
        if not force:
            return link, [f"REFUSED {link} exists as {detail}; pass --force to replace it"], False, 1
        return link, [f"{link}: replace {detail} with a link to {SKILL_SRC}"], True, 0
    return link, [f"{link}: link -> {SKILL_SRC}"], True, 0


def make_link(target: str, source: str) -> None:
    target = os.path.normpath(target)
    source = os.path.normpath(source)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.islink(target):
        os.unlink(target)
    elif os.path.isdir(target):
        shutil.move(target, target + ".replaced-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    if os.name == "nt":
        # a junction needs no elevated token, unlike a symbolic link; mklink only
        # accepts backslash paths, so normalise before handing it over
        done = subprocess.run(["cmd", "/c", "mklink", "/J", target, source],
                              capture_output=True, text=True)
        if done.returncode != 0:
            try:
                os.symlink(source, target, target_is_directory=True)
            except OSError as exc:
                raise SystemExit(
                    f"FAILED to link {target}\n"
                    f"  mklink /J: {done.stdout.strip() or done.stderr.strip() or 'no output'}\n"
                    f"  symlink:   {exc}\n"
                    f"  Either way, link {SKILL_SRC} into {os.path.dirname(target)} by hand.")
    else:
        os.symlink(source, target)


def apply_settings(path: str, doc: dict, dry: bool) -> None:
    if dry:
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if os.path.isfile(path):
        shutil.copy2(path, f"{path}.ensp-backup-{stamp}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    json.load(open(path, encoding="utf-8"))        # prove we did not corrupt it


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="install the ensp MCP server and skill")
    parser.add_argument("--apply", action="store_true", help="write changes (default is dry run)")
    parser.add_argument("--force", action="store_true", help="replace an existing skill directory")
    parser.add_argument("--name", default="ensp", help="mcpServers key (default: ensp)")
    parser.add_argument("--python", default=sys.executable, help="interpreter to record")
    parser.add_argument("--qoder-dir", help="override ~/.qoder-cn")
    parser.add_argument("--no-skill", action="store_true", help="skip the skill link")
    args = parser.parse_args(argv)

    dry = not args.apply
    qdir = os.path.normpath(args.qoder_dir or qoder_dir())
    print(("DRY RUN - nothing written\n" if dry else "APPLYING\n") + f"repo:      {REPO}")
    print(f"qoder dir: {qdir}\npython:    {args.python}")

    if not os.path.isfile(SERVER):
        print(f"BLOCKED server file missing: {SERVER}")
        return 2
    if not os.path.isdir(SKILL_SRC):
        print(f"BLOCKED skill folder missing: {SKILL_SRC}")
        return 2
    if not os.path.isdir(qdir):
        print(f"BLOCKED {qdir} does not exist - is Qoder installed and started once?")
        return 2

    rc = 0
    should = False
    path, doc, lines, changed = plan_settings(qdir, args.name, args.python)
    for line in lines:
        print("  " + line)
    if changed:
        apply_settings(path, doc, dry)
        print(f"  {'would write' if dry else 'wrote'} {path}")

    if not args.no_skill:
        link, slines, should, rc2 = plan_skill(qdir, args.force)
        for line in slines:
            print("  " + line)
        rc = max(rc, rc2)
        if should and rc == 0:
            if dry:
                print(f"  would link {link}")
            else:
                make_link(link, SKILL_SRC)
                print(f"  linked {link}")

    pending = changed or should
    print()
    if rc:
        print("next: rerun with --force to replace the existing skill directory")
        return rc
    if dry and pending:
        print("next: rerun with --apply to make the changes above")
        return 0
    if not pending:
        print("nothing to do - this repo is already installed")
        return 0
    print("done. Two steps remain:")
    print("  1. open a NEW chat session - MCP servers attach only when a session starts")
    print(f"  2. python {os.path.join(REPO, 'selftest.py')} --device 2001")
    return 0


if __name__ == "__main__":
    sys.exit(main())
