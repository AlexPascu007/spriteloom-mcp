#!/usr/bin/env python3
"""Temporary-home tests for install-agent-integration.py."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tomllib


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-agent-integration.py"


class InstallerTests(unittest.TestCase):
    def make_repo(self, parent: Path, *, spaced: bool = False) -> Path:
        name = "SpriteLoom test repo" if spaced else "repo"
        root = parent / name
        (root / "skills").mkdir(parents=True)
        shutil.copytree(ROOT / "skills" / "spriteloom", root / "skills" / "spriteloom")
        (root / "mcp_entry.py").write_text("# test entry\n", encoding="utf-8")
        return root

    def run_installer(
        self,
        home: Path,
        *arguments: str,
        root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(root or ROOT),
            "--home",
            str(home),
            *arguments,
        ]
        return subprocess.run(command, check=False, text=True, capture_output=True)

    def test_dry_run_does_not_create_any_selected_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            result = self.run_installer(home, "--all")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("dry-run", result.stdout)
            self.assertFalse(home.exists())

    def test_apply_merges_all_clients_and_repeat_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            home = parent / "home with spaces"
            root = self.make_repo(parent, spaced=True)

            codex_config = home / ".codex" / "config.toml"
            codex_config.parent.mkdir(parents=True)
            codex_config.write_text(
                '# keep this comment\n[other]\nsecret = "keep"\n', encoding="utf-8"
            )
            claude_config = home / ".claude.json"
            claude_config.parent.mkdir(parents=True, exist_ok=True)
            claude_config.write_text(
                json.dumps(
                    {"theme": "dark", "mcpServers": {"other": {"command": "keep"}}}
                ),
                encoding="utf-8",
            )
            desktop_config = (
                home
                / "Library"
                / "Application Support"
                / "Claude"
                / "claude_desktop_config.json"
            )
            desktop_config.parent.mkdir(parents=True)
            desktop_config.write_text(
                json.dumps({"telemetry": False}), encoding="utf-8"
            )

            result = self.run_installer(home, "--all", "--apply", root=root)
            self.assertEqual(result.returncode, 0, result.stderr)

            codex = tomllib.loads(codex_config.read_text(encoding="utf-8"))
            self.assertEqual(codex["other"]["secret"], "keep")
            self.assertEqual(
                codex["mcp_servers"]["spriteloom"]["command"],
                str(root.resolve() / ".venv" / "bin" / "python"),
            )
            self.assertEqual(
                codex["mcp_servers"]["spriteloom"]["args"],
                [str(root.resolve() / "mcp_entry.py")],
            )
            self.assertEqual(
                codex["mcp_servers"]["spriteloom"]["tool_timeout_sec"], 600
            )
            claude = json.loads(claude_config.read_text(encoding="utf-8"))
            self.assertEqual(claude["theme"], "dark")
            self.assertEqual(claude["mcpServers"]["other"]["command"], "keep")
            self.assertEqual(
                claude["mcpServers"]["spriteloom"]["args"],
                [str(root.resolve() / "mcp_entry.py")],
            )
            desktop = json.loads(desktop_config.read_text(encoding="utf-8"))
            self.assertFalse(desktop["telemetry"])
            self.assertIn("spriteloom", desktop["mcpServers"])

            self.assertTrue((home / ".codex" / "skills" / "spriteloom" / "SKILL.md").is_file())
            self.assertTrue((home / ".claude" / "skills" / "spriteloom" / "SKILL.md").is_file())
            self.assertFalse(
                (
                    home
                    / "Library"
                    / "Application Support"
                    / "Claude"
                    / "skills"
                    / "spriteloom"
                ).exists()
            )
            backups_before = sorted(str(path) for path in home.rglob("*.bak-*"))
            contents_before = {
                path: path.read_bytes()
                for path in (codex_config, claude_config, desktop_config)
            }

            result = self.run_installer(home, "--all", "--apply", root=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(backups_before, sorted(str(path) for path in home.rglob("*.bak-*")))
            self.assertEqual(
                contents_before,
                {path: path.read_bytes() for path in (codex_config, claude_config, desktop_config)},
            )

    def test_collision_is_safe_until_replace_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            home = parent / "home"
            root = self.make_repo(parent)
            config = home / ".claude.json"
            config.parent.mkdir(parents=True)
            original = {"mcpServers": {"spriteloom": {"command": "/user/tool"}}}
            config.write_text(json.dumps(original), encoding="utf-8")
            skill = home / ".claude" / "skills" / "spriteloom"
            skill.parent.mkdir(parents=True)
            shutil.copytree(root / "skills" / "spriteloom", skill)
            (skill / "user-file.txt").write_text("keep me", encoding="utf-8")

            result = self.run_installer(home, "--client", "claude-code", root=root)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(config.read_text(encoding="utf-8")), original)
            self.assertTrue((skill / "user-file.txt").exists())
            self.assertFalse(list(home.rglob("*.bak-*")))

            result = self.run_installer(
                home, "--client", "claude-code", "--apply", "--replace", root=root
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(installed["mcpServers"]["spriteloom"]["command"], str(root.resolve() / ".venv" / "bin" / "python"))
            self.assertFalse((skill / "user-file.txt").exists())
            self.assertTrue(list(home.rglob("*.bak-*")))

    def test_symlink_to_source_skill_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            home = parent / "home"
            root = self.make_repo(parent)
            skill = home / ".codex" / "skills" / "spriteloom"
            skill.parent.mkdir(parents=True)
            skill.symlink_to(root / "skills" / "spriteloom", target_is_directory=True)
            result = self.run_installer(home, "--client", "codex", "--apply", root=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(skill.is_symlink())
            self.assertTrue((home / ".codex" / "config.toml").exists())

    def test_separate_engine_root_is_passed_without_changing_command_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            home = parent / "home"
            mcp_root = self.make_repo(parent, spaced=True)
            engine_root = (parent / "model engine").resolve()
            engine_root.mkdir()
            result = self.run_installer(
                home,
                "--client",
                "codex",
                "--apply",
                "--engine-root",
                str(engine_root),
                root=mcp_root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            config = tomllib.loads(
                (home / ".codex" / "config.toml").read_text(encoding="utf-8")
            )
            entry = config["mcp_servers"]["spriteloom"]
            self.assertEqual(entry["command"], str(mcp_root.resolve() / ".venv" / "bin" / "python"))
            self.assertEqual(entry["args"], [str(mcp_root.resolve() / "mcp_entry.py")])
            self.assertEqual(entry["env"], {"SPRITELOOM_ENGINE_ROOT": str(engine_root)})

    def test_codex_replace_preserves_unrelated_toml_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            home = parent / "home"
            root = self.make_repo(parent)
            config = home / ".codex" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                """[other]\nsecret = \"keep\"\n\n[mcp_servers.spriteloom]\ncommand = \"/old/python\"\nargs = [\"/old/entry.py\"]\ntool_timeout_sec = 1\nstartup_timeout_sec = 1\n\n[mcp_servers.other]\nvalue = \"keep\"\n""",
                encoding="utf-8",
            )

            result = self.run_installer(
                home, "--client", "codex", "--apply", "--replace", root=root
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(parsed["other"]["secret"], "keep")
            self.assertEqual(parsed["mcp_servers"]["other"]["value"], "keep")
            self.assertEqual(
                parsed["mcp_servers"]["spriteloom"]["command"],
                str(root.resolve() / ".venv" / "bin" / "python"),
            )
            self.assertTrue(list(home.rglob("config.toml.bak-*")))


if __name__ == "__main__":
    unittest.main()
