#!/usr/bin/env python3
"""Install the SpriteLoom MCP server and skill for local agent clients.

The default mode is a dry run.  The installer changes only the selected
client configuration files and skill directories when ``--apply`` is given.
It does not contact an MCP server or edit a real configuration during tests.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


CLIENTS = ("codex", "claude-code", "claude-desktop")
MISSING = object()


class InstallerError(RuntimeError):
    """A user-fixable installer error."""


class CollisionError(InstallerError):
    """A skill or MCP entry already exists with different contents."""


@dataclass(frozen=True)
class Paths:
    root: Path
    home: Path
    engine_root: Path | None = None
    service_url: str | None = None
    platform: str = sys.platform

    @property
    def skill_source(self) -> Path:
        return self.root / "skills" / "spriteloom"

    @property
    def python(self) -> Path:
        if self.platform == "win32":
            return self.root / ".venv" / "Scripts" / "python.exe"
        return self.root / ".venv" / "bin" / "python"

    @property
    def mcp_entry(self) -> Path:
        return self.root / "mcp_entry.py"

    def skill_target(self, client: str) -> Path:
        if client == "codex":
            return self.home / ".codex" / "skills" / "spriteloom"
        if client == "claude-code":
            return self.home / ".claude" / "skills" / "spriteloom"
        raise ValueError(f"{client} does not use an automatically installed skill")

    def config_target(self, client: str) -> Path:
        if client == "codex":
            return self.home / ".codex" / "config.toml"
        if client == "claude-code":
            return self.home / ".claude.json"
        if client == "claude-desktop":
            if self.platform == "win32":
                app_data = (
                    Path(os.environ["APPDATA"])
                    if self.home == Path.home() and os.environ.get("APPDATA")
                    else self.home / "AppData" / "Roaming"
                )
                return app_data / "Claude" / "claude_desktop_config.json"
            return (
                self.home
                / "Library"
                / "Application Support"
                / "Claude"
                / "claude_desktop_config.json"
            )
        raise ValueError(f"unknown client: {client}")


@dataclass(frozen=True)
class Operation:
    """A preflighted mutation or no-op."""

    client: str
    kind: str
    path: Path
    action: str
    desired: Mapping[str, Any] | None = None


def _resolve_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def _selected_clients(args: argparse.Namespace) -> list[str]:
    selected: list[str] = []
    if args.all_clients:
        selected.extend(CLIENTS)
    selected.extend(args.client or [])
    result: list[str] = []
    for client in selected:
        if client not in result:
            result.append(client)
    if not result:
        raise InstallerError("select at least one client with --client or --all")
    return result


def _server_entry(paths: Paths, client: str) -> dict[str, Any]:
    base = {
        "command": str(paths.python),
        "args": [str(paths.mcp_entry)],
    }
    if client == "codex":
        # Generation requests can queue behind the resident model.  Keep the
        # client timeout above the measured single-request time.
        base.update({"tool_timeout_sec": 600, "startup_timeout_sec": 30})
    environment: dict[str, str] = {}
    if paths.engine_root is not None:
        environment["SPRITELOOM_ENGINE_ROOT"] = str(paths.engine_root)
    if paths.service_url is not None:
        environment["SPRITELOOM_URL"] = paths.service_url
    if environment:
        base["env"] = environment
    return base


def _lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | str | None]]:
    """Return a content snapshot without following links below ``root``."""

    if not root.is_dir():
        return {}
    result: dict[str, tuple[str, bytes | str | None]] = {}
    for base, dir_names, file_names in os.walk(root, followlinks=False):
        base_path = Path(base)
        # A symlinked directory is reported as a link, and is not traversed.
        for name in list(dir_names):
            path = base_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
                dir_names.remove(name)
            else:
                result[relative] = ("dir", None)
        for name in file_names:
            path = base_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
            else:
                result[relative] = ("file", path.read_bytes())
    return result


def _same_skill(source: Path, target: Path) -> bool:
    """Check a target, including a symlinked skill, against the source."""

    if not _lexists(target):
        return False
    source_real = source.resolve()
    target_real = target.resolve(strict=False)
    if target_real == source_real:
        return True
    if not target_real.is_dir():
        return False
    return _tree_snapshot(source) == _tree_snapshot(target_real)


def _timestamp() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).strftime(
        "%Y%m%d%H%M%S%f"
    )


def _backup_path(path: Path) -> Path:
    candidate = path.with_name(f"{path.name}.bak-{_timestamp()}")
    while _lexists(candidate):
        candidate = path.with_name(f"{path.name}.bak-{_timestamp()}")
    return candidate


def _backup_file(path: Path) -> Path:
    backup = _backup_path(path)
    shutil.copy2(path, backup, follow_symlinks=False)
    return backup


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old_mode: int | None = None
    if _lexists(path) and not path.is_symlink():
        old_mode = stat.S_IMODE(path.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        if old_mode is not None:
            os.chmod(temp_path, old_mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if _lexists(temp_path):
            temp_path.unlink()


def _install_skill(source: Path, target: Path, replace: bool) -> str:
    if _same_skill(source, target):
        return "unchanged"
    if _lexists(target) and not replace:
        raise CollisionError(
            f"skill already exists with different contents: {target}; "
            "use --replace to replace it"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_parent = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(target.parent))
    )
    temp_target = temp_parent / target.name
    moved_backup: Path | None = None
    try:
        shutil.copytree(source, temp_target, symlinks=True)
        if _lexists(target):
            moved_backup = _backup_path(target)
            os.replace(target, moved_backup)
        try:
            os.replace(temp_target, target)
        except Exception:
            if moved_backup is not None and not _lexists(target):
                os.replace(moved_backup, target)
            raise
    finally:
        shutil.rmtree(temp_parent, ignore_errors=True)
    return "replaced" if moved_backup is not None else "installed"


def _read_json_object(path: Path) -> dict[str, Any]:
    if not _lexists(path):
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallerError(f"cannot read {path}: {exc}") from exc
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallerError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallerError(f"top-level JSON value in {path} must be an object")
    return value


def _json_operation(
    client: str,
    path: Path,
    desired: Mapping[str, Any],
    replace: bool,
) -> Operation:
    document = _read_json_object(path)
    servers = document.get("mcpServers", MISSING)
    if servers is MISSING:
        existing = MISSING
    elif not isinstance(servers, dict):
        raise InstallerError(f"mcpServers in {path} must be an object")
    else:
        existing = servers.get("spriteloom", MISSING)

    if existing is not MISSING and existing == desired:
        action = "unchanged"
    elif existing is not MISSING and not replace:
        raise CollisionError(
            f"MCP server 'spriteloom' in {path} differs; use --replace to replace it"
        )
    else:
        action = "replaced" if existing is not MISSING else "installed"
    return Operation(client, "json", path, action, dict(desired))


def _read_toml(path: Path) -> tuple[dict[str, Any], str]:
    if not _lexists(path):
        return {}, ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallerError(f"cannot read {path}: {exc}") from exc
    try:
        value = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise InstallerError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallerError(f"top-level TOML value in {path} must be a table")
    return value, raw


def _toml_operation(
    client: str,
    path: Path,
    desired: Mapping[str, Any],
    replace: bool,
) -> Operation:
    document, raw = _read_toml(path)
    servers = document.get("mcp_servers", MISSING)
    if servers is MISSING:
        existing = MISSING
    elif not isinstance(servers, dict):
        raise InstallerError(f"mcp_servers in {path} must be a table")
    else:
        existing = servers.get("spriteloom", MISSING)

    if existing is not MISSING and existing == desired:
        action = "unchanged"
    elif existing is not MISSING and not replace:
        raise CollisionError(
            f"MCP server 'spriteloom' in {path} differs; use --replace to replace it"
        )
    else:
        action = "replaced" if existing is not MISSING else "installed"
        if action == "replaced" and not _toml_owned_ranges(raw):
            raise InstallerError(
                f"cannot safely replace the SpriteLoom TOML block in {path}"
            )
    return Operation(client, "toml", path, action, dict(desired))


def _toml_basic(value: str) -> str:
    # JSON string escaping is valid for TOML basic strings and keeps paths with
    # spaces, quotes, and backslashes safe.
    return json.dumps(value, ensure_ascii=False)


def _toml_block(desired: Mapping[str, Any]) -> str:
    lines = [
        "[mcp_servers.spriteloom]",
        f"command = {_toml_basic(str(desired['command']))}",
        "args = ["
        + ", ".join(_toml_basic(str(item)) for item in desired["args"])
        + "]",
        f"tool_timeout_sec = {int(desired['tool_timeout_sec'])}",
        f"startup_timeout_sec = {int(desired['startup_timeout_sec'])}",
    ]
    environment = desired.get("env")
    if isinstance(environment, Mapping) and environment:
        lines.extend(
            [
                "",
                "[mcp_servers.spriteloom.env]",
                *(
                    f"{key} = {_toml_basic(str(value))}"
                    for key, value in environment.items()
                ),
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _toml_header(line: str) -> tuple[bool, str] | None:
    stripped = line.strip()
    array_match = re.match(r"^\[\[(.+)\]\]\s*(?:#.*)?$", stripped)
    if array_match:
        return True, array_match.group(1).strip()
    table_match = re.match(r"^\[(.+)\]\s*(?:#.*)?$", stripped)
    if table_match:
        return False, table_match.group(1).strip()
    return None


def _toml_owned_ranges(raw: str) -> list[tuple[int, int]]:
    """Return line ranges for the SpriteLoom table and its child tables."""

    lines = raw.splitlines(keepends=True)
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate(lines):
        header = _toml_header(line)
        if header is None:
            continue
        name = header[1]
        is_owned = name == "mcp_servers.spriteloom" or name.startswith(
            "mcp_servers.spriteloom."
        )
        if is_owned:
            if start is not None:
                ranges.append((start, index))
            start = index
        elif start is not None:
            ranges.append((start, index))
            start = None
    if start is not None:
        ranges.append((start, len(lines)))
    return ranges


def _replace_toml_block(raw: str, desired: Mapping[str, Any]) -> str:
    lines = raw.splitlines(keepends=True)
    ranges = _toml_owned_ranges(raw)
    if not ranges:
        raise InstallerError("SpriteLoom TOML table is not a replaceable section")
    removed: set[int] = set()
    for start, end in ranges:
        removed.update(range(start, end))
    kept = "".join(line for index, line in enumerate(lines) if index not in removed)
    kept = kept.rstrip()
    if kept:
        kept += "\n\n"
    return kept + _toml_block(desired)


def _append_toml_block(raw: str, desired: Mapping[str, Any]) -> str:
    if not raw.strip():
        return _toml_block(desired)
    return raw.rstrip() + "\n\n" + _toml_block(desired)


def _json_text(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _apply_json(operation: Operation) -> Path | None:
    document = _read_json_object(operation.path)
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallerError(f"mcpServers in {operation.path} must be an object")
    servers["spriteloom"] = dict(operation.desired or {})
    backup = _backup_file(operation.path) if _lexists(operation.path) else None
    _atomic_write(operation.path, _json_text(document))
    return backup


def _apply_toml(operation: Operation) -> Path | None:
    _document, raw = _read_toml(operation.path)
    if operation.action == "replaced":
        text = _replace_toml_block(raw, operation.desired or {})
    else:
        text = _append_toml_block(raw, operation.desired or {})
    # Validate the final content before it reaches the user's config path.
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InstallerError(f"generated TOML for {operation.path} is invalid: {exc}") from exc
    backup = _backup_file(operation.path) if _lexists(operation.path) else None
    _atomic_write(operation.path, text)
    return backup


def _preflight(paths: Paths, clients: Iterable[str], replace: bool) -> list[Operation]:
    if not paths.skill_source.is_dir():
        raise InstallerError(f"skill source not found: {paths.skill_source}")
    operations: list[Operation] = []
    for client in clients:
        if client in ("codex", "claude-code"):
            skill_target = paths.skill_target(client)
            skill_action = (
                "unchanged"
                if _same_skill(paths.skill_source, skill_target)
                else "replaced"
                if _lexists(skill_target) and replace
                else "installed"
            )
            if _lexists(skill_target) and skill_action == "installed":
                raise CollisionError(
                    f"skill already exists with different contents: {skill_target}; "
                    "use --replace to replace it"
                )
            operations.append(Operation(client, "skill", skill_target, skill_action))

        config = paths.config_target(client)
        desired = _server_entry(paths, client)
        if client == "codex":
            operations.append(_toml_operation(client, config, desired, replace))
        else:
            operations.append(_json_operation(client, config, desired, replace))
    return operations


def _label(operation: Operation) -> str:
    if operation.kind == "skill":
        return f"{operation.client} skill {operation.path}"
    return f"{operation.client} MCP config {operation.path}"


def _run(paths: Paths, clients: list[str], *, apply: bool, replace: bool) -> int:
    operations = _preflight(paths, clients, replace)
    mode = "apply" if apply else "dry-run"
    print(f"SpriteLoom agent integration ({mode})")
    for operation in operations:
        if not apply or operation.action == "unchanged":
            print(f"  {operation.action}: {_label(operation)}")
            continue
        if operation.kind == "skill":
            action = _install_skill(paths.skill_source, operation.path, replace)
            print(f"  {action}: {_label(operation)}")
        elif operation.kind == "json":
            backup = _apply_json(operation)
            print(f"  {operation.action}: {_label(operation)}")
            if backup:
                print(f"    backup: {backup}")
        elif operation.kind == "toml":
            backup = _apply_toml(operation)
            print(f"  {operation.action}: {_label(operation)}")
            if backup:
                print(f"    backup: {backup}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install SpriteLoom MCP integration for local agent clients."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the selected skill and client configuration (default: dry-run)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace a same-name skill or MCP entry after preflight",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="absolute or relative SpriteLoom repository root (default: script repository)",
    )
    parser.add_argument(
        "--engine-root",
        type=Path,
        help="separate model-engine repository; passed as SPRITELOOM_ENGINE_ROOT",
    )
    parser.add_argument(
        "--service-url",
        help="connect to an existing WebSocket engine; passed as SPRITELOOM_URL",
    )
    parser.add_argument(
        "--home",
        type=Path,
        help="home directory override for tests or a controlled install",
    )
    parser.add_argument(
        "--client",
        action="append",
        choices=CLIENTS,
        help="target client; repeat for more than one client",
    )
    parser.add_argument(
        "--all",
        dest="all_clients",
        action="store_true",
        help="target Codex, Claude Code, and Claude Desktop",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root = _resolve_path(args.root or Path(__file__).resolve().parents[1])
        home = _resolve_path(args.home or Path.home())
        engine_root = _resolve_path(args.engine_root) if args.engine_root else None
        clients = _selected_clients(args)
        return _run(
            Paths(
                root=root,
                home=home,
                engine_root=engine_root,
                service_url=args.service_url,
            ),
            clients,
            apply=args.apply,
            replace=args.replace,
        )
    except InstallerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
