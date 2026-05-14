#!/usr/bin/env python3
"""Install / uninstall the watcher Stop hook in ~/.claude/settings.json.

The script edits the user-level Claude Code settings file in place. It is
idempotent: re-running with --install never duplicates an entry; --uninstall
is a no-op if the entry is absent.

Usage:
    install-hook.py              # install (default)
    install-hook.py --install
    install-hook.py --uninstall
    install-hook.py --status     # print whether the hook is installed
    install-hook.py --settings PATH    # override ~/.claude/settings.json
    install-hook.py --hook PATH        # override the hook script target

Env overrides (lower precedence than flags):
    WATCHER_HOOK_COMMAND    -> --hook
    CLAUDE_SETTINGS_PATH    -> --settings
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_hook_command() -> str:
    """Hook command must invoke Python explicitly on Windows — `.py` files are
    not directly executable from `~/.claude/settings.json` the way they are
    on POSIX via shebang. Path is quoted so spaces in user paths
    (e.g. `C:\\Users\\First Last\\...`) survive shell splitting.
    """
    env = os.environ.get("WATCHER_HOOK_COMMAND", "").strip()
    if env:
        return env
    hook_path = repo_root() / "hooks" / "claude-stop-notify.py"
    # Forward slashes are accepted by Windows Python and avoid JSON-escaping
    # backslashes in settings.json (cleaner diffs, no \\ confusion).
    return f'python "{hook_path.as_posix()}"'


def default_settings_path() -> Path:
    env = os.environ.get("CLAUDE_SETTINGS_PATH", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude" / "settings.json"


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"settings.json is not valid JSON ({path}): {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"settings.json root must be an object: {path}")
    return data


def write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def stop_hook_entries(data: dict) -> list:
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit('settings.json "hooks" must be an object')
    arr = hooks.setdefault("Stop", [])
    if not isinstance(arr, list):
        raise SystemExit('settings.json "hooks.Stop" must be an array')
    return arr


def hook_matches(entry: dict, hook_cmd: str) -> bool:
    inner = entry.get("hooks")
    if not isinstance(inner, list):
        return False
    for h in inner:
        if isinstance(h, dict) and h.get("type") == "command" and h.get("command") == hook_cmd:
            return True
    return False


def cmd_status(settings_path: Path, hook_cmd: str) -> int:
    data = load_settings(settings_path)
    hooks = data.get("hooks") or {}
    stop = hooks.get("Stop") if isinstance(hooks, dict) else None
    installed = False
    if isinstance(stop, list):
        for entry in stop:
            if isinstance(entry, dict) and hook_matches(entry, hook_cmd):
                installed = True
                break
    print(f"settings: {settings_path}")
    print(f"hook:     {hook_cmd}")
    print(f"status:   {'installed' if installed else 'not installed'}")
    return 0 if installed else 1


def _extract_script_path(hook_cmd: str) -> Path | None:
    """Pull the .py path out of a hook command like `python "C:/.../foo.py"`
    so we can sanity-check it exists before writing the hook entry.
    Returns None if no .py is parsable (custom command — trust the user).
    """
    # crude: find first .py segment
    for seg in hook_cmd.replace('"', " ").split():
        if seg.endswith(".py"):
            return Path(seg)
    return None


def cmd_install(settings_path: Path, hook_cmd: str) -> int:
    script_path = _extract_script_path(hook_cmd)
    if script_path is not None and not script_path.is_file():
        print(f"error: hook script not found: {script_path}", file=sys.stderr)
        return 2
    # chmod intentionally skipped — Windows has no POSIX exec bit and the
    # `python "..."` command form invokes the interpreter directly anyway.

    data = load_settings(settings_path)
    stop = stop_hook_entries(data)

    for entry in stop:
        if isinstance(entry, dict) and hook_matches(entry, hook_cmd):
            print(f"already installed: {hook_cmd}")
            print(f"settings: {settings_path}")
            return 0

    stop.append({
        "matcher": "*",
        "hooks": [{"type": "command", "command": hook_cmd}],
    })
    write_settings(settings_path, data)
    print(f"installed Stop hook: {hook_cmd}")
    print(f"settings: {settings_path}")
    print("note: existing Claude Code conversations must be restarted to pick up the new hook")
    return 0


def cmd_uninstall(settings_path: Path, hook_cmd: str) -> int:
    if not settings_path.exists():
        print(f"settings.json missing, nothing to uninstall: {settings_path}")
        return 0

    data = load_settings(settings_path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        print("no hooks block, nothing to uninstall")
        return 0
    stop = hooks.get("Stop")
    if not isinstance(stop, list):
        print("no Stop hook block, nothing to uninstall")
        return 0

    new_stop = []
    removed = 0
    for entry in stop:
        if isinstance(entry, dict) and hook_matches(entry, hook_cmd):
            inner = [h for h in entry.get("hooks", [])
                     if not (isinstance(h, dict)
                             and h.get("type") == "command"
                             and h.get("command") == hook_cmd)]
            removed += 1
            if inner:
                new_entry = dict(entry)
                new_entry["hooks"] = inner
                new_stop.append(new_entry)
        else:
            new_stop.append(entry)

    if removed == 0:
        print(f"not installed, nothing to remove: {hook_cmd}")
        return 0

    if new_stop:
        hooks["Stop"] = new_stop
    else:
        del hooks["Stop"]
        if not hooks:
            del data["hooks"]

    write_settings(settings_path, data)
    print(f"removed {removed} entry(ies) for: {hook_cmd}")
    print(f"settings: {settings_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Install/uninstall watcher Stop hook in Claude Code settings")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--install", action="store_true", help="install the hook (default)")
    group.add_argument("--uninstall", action="store_true", help="remove the hook")
    group.add_argument("--status", action="store_true", help="report install status only")
    p.add_argument("--settings", help="path to settings.json (default: ~/.claude/settings.json)")
    p.add_argument("--hook", help="path to the hook script (default: <repo>/hooks/claude-stop-notify.py)")
    args = p.parse_args(argv)

    settings_path = Path(args.settings).expanduser() if args.settings else default_settings_path()
    hook_cmd = args.hook or default_hook_command()

    if args.status:
        return cmd_status(settings_path, hook_cmd)
    if args.uninstall:
        return cmd_uninstall(settings_path, hook_cmd)
    return cmd_install(settings_path, hook_cmd)


if __name__ == "__main__":
    sys.exit(main())
