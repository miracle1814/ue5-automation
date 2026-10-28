#!/usr/bin/env python3
"""One-click MCP client registration for ue5-automation.

Place this file NEXT TO ue5_mcp_stdio.py (skill/ue5-automation/scripts/), then run:

    python install_mcp.py                 # register into every detected client
    python install_mcp.py --client cursor claude-desktop
    python install_mcp.py --list          # show what would be written, change nothing
    python install_mcp.py --remove        # remove the entry from every client
    python install_mcp.py --project .     # also write project-local configs (.mcp.json, .vscode/mcp.json)

Every config file is backed up to <name>.bak-<timestamp> before the first write in a run.
Existing "ue5-automation" entries are replaced (idempotent); all other keys are preserved.
No third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

ENTRY_KEY = "ue5-automation"
_PROXY_OVERRIDE: Optional[Path] = None


def proxy_command() -> dict:
    """Build the mcpServers entry. Uses the interpreter running this script so the
    registered command works even if 'python' is not on the client's PATH."""
    proxy = _PROXY_OVERRIDE or (Path(__file__).resolve().parent / "ue5_mcp_stdio.py")
    if not proxy.is_file():
        sys.exit(f"ERROR: {proxy} not found. Place install_mcp.py next to ue5_mcp_stdio.py "
                 f"inside the ue5-automation repo, or pass --proxy.")
    return {"command": sys.executable, "args": [str(proxy)]}


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments outside strings (Cursor's mcp.json allows them)."""
    out, i, n, in_str, esc = [], 0, len(text), False, False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def load_jsonc(path: Path) -> dict:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(strip_jsonc(text) or "{}")


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def backup(path: Path, done: set) -> None:
    if path.is_file() and path not in done:
        bak = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, bak)
        done.add(path)


def upsert(entry: dict, remove: bool) -> dict:
    servers = entry.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        entry["mcpServers"] = servers
    if remove:
        servers.pop(ENTRY_KEY, None)
    else:
        servers[ENTRY_KEY] = proxy_command()
    return entry


def upsert_vscode(entry: dict, remove: bool) -> dict:
    servers = entry.get("servers")
    if not isinstance(servers, dict):
        servers = {}
        entry["servers"] = servers
    if remove:
        servers.pop(ENTRY_KEY, None)
    else:
        servers[ENTRY_KEY] = proxy_command()
    return entry


def upsert_codex(remove: bool) -> Optional[str]:
    """Codex uses TOML at ~/.codex/config.toml. Insert/replace a [mcp_servers.ue5-automation]
    block textually to avoid rewriting the rest of the file."""
    path = Path.home() / ".codex" / "config.toml"
    cmd, args = proxy_command()["command"], proxy_command()["args"]
    block = (f'[mcp_servers.{ENTRY_KEY}]\ncommand = "{cmd}"\n'
             f'args = [{", ".join(json.dumps(a) for a in args)}]\n\n')
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines, out, skip = text.splitlines(keepends=True), [], False
    for line in lines:
        if line.strip().startswith(f"[mcp_servers.{ENTRY_KEY}]"):
            skip = True
            if not remove:
                out.append(block)
            continue
        if skip:
            if line.strip().startswith("["):
                skip = False
            else:
                continue
        out.append(line)
    if not remove and not any(f"[mcp_servers.{ENTRY_KEY}]" in l for l in out):
        out.append("\n" + block if out and out[-1].strip() else block)
    if remove and not any(f"[mcp_servers.{ENTRY_KEY}]" in l for l in out):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(out), encoding="utf-8")
    return str(path)


def claude_code_cli(remove: bool) -> Optional[str]:
    """Prefer the official `claude mcp` CLI for user-scope Claude Code registration;
    returns None when the CLI is unavailable so the caller can fall back to JSON editing."""
    if shutil.which("claude") is None:
        return None
    import subprocess
    if remove:
        subprocess.run(["claude", "mcp", "remove", "-s", "user", ENTRY_KEY],
                       capture_output=True, text=True)
        return f"removed via `claude mcp remove -s user {ENTRY_KEY}`"
    pc = proxy_command()
    cmd = ["claude", "mcp", "add", "-s", "user", ENTRY_KEY, "--", pc["command"], *pc["args"]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        return f"added via `claude mcp add -s user`"
    return None


def upsert_zcode(entry: dict, remove: bool) -> dict:
    """ZCode: ~/.zcode/cli/config.json → {"mcp": {"servers": {...}}}。"""
    mcp = entry.get("mcp")
    if not isinstance(mcp, dict):
        mcp = {}
        entry["mcp"] = mcp
    servers = mcp.get("servers")
    if not isinstance(servers, dict):
        servers = {}
        mcp["servers"] = servers
    if remove:
        servers.pop(ENTRY_KEY, None)
    else:
        servers[ENTRY_KEY] = proxy_command()
    return entry


def user_clients() -> dict:
    """client name -> (config path, mutator, label)."""
    home, appdata = Path.home(), os.environ.get("APPDATA")
    cfg: dict[str, tuple] = {}
    if appdata:
        cfg["claude-desktop"] = (Path(appdata) / "Claude" / "claude_desktop_config.json", upsert, "Claude Desktop")
    if sys.platform == "darwin":
        cfg["claude-desktop"] = (home / "Library/Application Support/Claude/claude_desktop_config.json", upsert, "Claude Desktop")
    cfg["cursor"] = (home / ".cursor" / "mcp.json", upsert, "Cursor")
    cfg["windsurf"] = (home / ".codeium" / "windsurf" / "mcp_config.json", upsert, "Windsurf")
    cfg["claude-code-user"] = (home / ".claude.json", upsert, "Claude Code (user scope)")
    cfg["zcode"] = (home / ".zcode" / "cli" / "config.json", upsert_zcode, "ZCode")
    return cfg


def project_clients(project: str) -> dict:
    root = Path(project).resolve()
    return {
        "claude-code-project": (root / ".mcp.json", upsert, "Claude Code (project .mcp.json)"),
        "vscode": (root / ".vscode" / "mcp.json", upsert_vscode, "VS Code (.vscode/mcp.json)"),
    }


def _target_exists(name: str, path) -> bool:
    """客户端是否“存在”：配置文件或其父目录存在（避免给没装的客户端写垃圾配置）。"""
    if name == "codex":
        return (Path.home() / ".codex").exists()
    return path is not None and (path.exists() or path.parent.exists())


def main() -> int:
    global _PROXY_OVERRIDE
    ap = argparse.ArgumentParser(description="Register the ue5-automation stdio MCP proxy into MCP clients.")
    ap.add_argument("--client", nargs="*", default=None,
                    help="subset of clients (default: all detected). names: claude-desktop cursor windsurf claude-code-user claude-code-project vscode codex")
    ap.add_argument("--project", default=None, help="project dir for project-local configs (claude-code-project, vscode)")
    ap.add_argument("--proxy", default=None, help="explicit path to ue5_mcp_stdio.py (overrides auto-detection)")
    ap.add_argument("--list", action="store_true", help="show targets and exit")
    ap.add_argument("--all", action="store_true",
                    help="write every known client even if not detected (default: detected only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be written, change nothing")
    ap.add_argument("--remove", action="store_true", help="remove the ue5-automation entry instead of adding it")
    args = ap.parse_args()

    if args.proxy:
        _PROXY_OVERRIDE = Path(args.proxy).resolve()

    targets = dict(user_clients())
    if args.project:
        targets.update(project_clients(args.project))
    targets["codex"] = (None, None, "Codex (~/.codex/config.toml)")

    if args.list:
        for name, (path, _, label) in targets.items():
            det = "yes" if _target_exists(name, path) else "no"
            print(f"{name:20s} {label:38s} "
                  f"{path if path else '~/.codex/config.toml'}  detected={det}")
        return 0

    if args.client:
        selected = args.client
    elif args.all:
        selected = list(targets.keys())
    else:
        selected = [n for n in targets if _target_exists(n, targets[n][0])]
        if not selected:
            print("未检测到任何 MCP 客户端配置目录；用 --all 强制写入，或 --list 查看目标。")
            return 2
    backed_up: set = set()
    results = []
    for name in selected:
        if name not in targets:
            results.append((name, "SKIP unknown client (see --list)"))
            continue
        path, mutator, label = targets[name]
        try:
            if name == "codex":
                changed = upsert_codex(args.remove)
                results.append((name, f"OK {changed}" if changed else "OK (entry absent, nothing written)" if args.remove else "OK ~/.codex/config.toml"))
                continue
            if name == "claude-code-user" and not args.list:
                via_cli = claude_code_cli(args.remove)
                if via_cli:
                    results.append((name, f"OK {via_cli}"))
                    continue
                # CLI unavailable: fall through to direct JSON edit below
            if not path.is_file() and args.remove:
                results.append((name, "SKIP no config file"))
                continue
            if args.dry_run:
                results.append((name, f"DRY {path}"))
                continue
            entry = load_jsonc(path)
            mutator(entry, args.remove)
            backup(path, backed_up)
            save_json(path, entry)
            results.append((name, f"OK {path}"))
        except Exception as e:  # noqa: BLE001 - report and continue with other clients
            results.append((name, f"FAIL {type(e).__name__}: {e}"))

    print("ue5-automation MCP %s:" % ("removed from" if args.remove else "registered into"))
    for name, status in results:
        head, _, detail = status.partition(" ")
        print("  [%-4s] %-20s %s" % (head, name, detail))
    if args.dry_run:
        print("")
        print("[dry-run] 未写入任何文件；去掉 --dry-run 即执行。")
    elif not args.remove:
        print("")
        print("Restart the client(s), then the ue5-automation tools appear automatically.")
        print("UE 5.1-5.5: start the editor first (bridge auto-starts on first write).")
        print("UE 5.8: enable the official MCP server in Project Settings (bAutoStartServer=True).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
