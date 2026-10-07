#!/usr/bin/env python3
"""Check the self-contained PlatformIO release package without a toolchain."""

from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import build_release


class PlatformioReleaseTests(unittest.TestCase):
    def make_source_tree(self, root: Path) -> Path:
        for name in build_release.PLATFORMIO_PACKAGE_PATHS:
            path = root / name
            if name in {"builder", "boards", "examples/platformio"}:
                path = path / "example.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture: {name}\n", encoding="utf-8")
        (root / "platform.json").write_text(
            json.dumps({"name": "nrf54l15clean", "version": "1.0.20"}),
            encoding="utf-8",
        )
        (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (root / "builder" / "ignored.txt").write_text("not shipped\n", encoding="utf-8")
        (root / "builder" / "__pycache__").mkdir()
        (root / "builder" / "__pycache__" / "stale.pyc").write_bytes(b"not shipped")
        platform = root / "hardware/nrf54l15clean/nrf54l15clean"
        platform.mkdir(parents=True)
        (platform / "platform.txt").write_text("version=1.0.20\n", encoding="utf-8")
        (platform / "keep.txt").write_text("shared framework bytes\n", encoding="utf-8")
        (platform / "private.txt").write_text("excluded framework bytes\n", encoding="utf-8")
        runtime = platform / "tools/runtime/pyocd-site"
        runtime.mkdir(parents=True)
        (runtime / "downloaded-dependency.py").write_text("not shipped\n", encoding="utf-8")
        (platform / "alias.txt").symlink_to("keep.txt")
        (root / "unrelated-secret.txt").write_text("not shipped\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        return platform

    def test_platformio_version_sync_preserves_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = {"name": "nrf54l15clean", "version": "0.1.0", "packages": {}}
            path = root / "platform.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertTrue(build_release.update_platformio_version(root, "1.0.21-rc1"))
            manifest["version"] = "1.0.21-rc1"
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), manifest)

    def test_legacy_arduino_tree_without_platformio_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertFalse(build_release.update_platformio_version(root, "1.0.20"))
            self.assertFalse((root / "platform.json").exists())

    def test_archive_is_deterministic_allowlisted_and_self_contained(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "repo"
            platform = self.make_source_tree(root)
            stage = base / "framework-stage"
            build_release.stage_git_release_tree(root, platform, stage)
            dist = base / "dist"
            dist.mkdir()
            arguments = (
                root, platform, stage, dist, "1.0.20",
                "https://example.com/releases/v1.0.20",
                build_release.GENERATED_PLATFORM_PACKAGE_EXCLUDES + ("private.txt",),
            )
            entry = build_release.build_platformio_archive(*arguments)
            archive = Path(entry["archivePath"])
            original = archive.read_bytes()
            self.assertEqual(entry["checksum"], f"SHA-256:{build_release.sha256_file(archive)}")
            self.assertEqual(entry["size"], len(original))
            self.assertEqual(entry["version"], "1.0.20")
            self.assertEqual(entry["name"], "nrf54l15clean")
            self.assertEqual(entry["url"].rsplit("/", 1)[1], archive.name)
            self.assertEqual(build_release.build_platformio_archive(*arguments), entry)
            self.assertEqual(archive.read_bytes(), original)
            with tarfile.open(archive) as packaged:
                members = packaged.getmembers()
                roots = {member.name.split("/", 1)[0] for member in members}
                self.assertEqual(roots, {"platform-nrf54l15clean-1.0.20"})
                self.assertFalse(any(member.issym() or member.islnk() for member in members))
                payloads = {
                    member.name.split("/", 1)[1]: packaged.extractfile(member).read()
                    for member in members if member.isfile()
                }
            framework_rel = platform.relative_to(root).as_posix()
            self.assertEqual(payloads[f"{framework_rel}/alias.txt"], (stage / "keep.txt").read_bytes())
            self.assertNotIn(f"{framework_rel}/private.txt", payloads)
            self.assertNotIn(f"{framework_rel}/tools/runtime/pyocd-site/downloaded-dependency.py", payloads)
            self.assertNotIn("builder/ignored.txt", payloads)
            self.assertNotIn("unrelated-secret.txt", payloads)
            self.assertFalse(any("__pycache__" in name for name in payloads))
            self.assertIn("platform.json", payloads)
            self.assertIn("LICENSE", payloads)
            arduino_archive = base / "arduino.tar.bz2"
            build_release.build_archive(
                stage, arduino_archive, "arduino",
                excludes=build_release.GENERATED_PLATFORM_PACKAGE_EXCLUDES + ("private.txt",),
            )
            with tarfile.open(arduino_archive) as packaged:
                for member in packaged.getmembers():
                    if member.isfile():
                        relative = member.name.split("/", 1)[1]
                        self.assertEqual(
                            packaged.extractfile(member).read(),
                            payloads[f"{framework_rel}/{relative}"],
                        )

    def test_missing_integration_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "repo"
            platform = self.make_source_tree(root)
            (root / "platform.py").unlink()
            with self.assertRaisesRegex(SystemExit, "PlatformIO package source not found"):
                build_release.build_platformio_archive(
                    root, platform, platform, base, "1.0.20",
                    "https://example.com", (),
                )

    def test_manifest_records_platformio_beside_arduino(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "release-manifest.json"
            entry = {"archiveFileName": "platform.tar.bz2", "version": "1.0.20"}
            build_release.write_release_manifest(
                path, version="1.0.20", platform={"archiveFileName": "arduino.tar.bz2"},
                platform_excludes=(), tools=[], indexes={}, platformio=entry,
            )
            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["platformio"], entry)
            self.assertEqual(manifest["platform"]["archiveFileName"], "arduino.tar.bz2")


if __name__ == "__main__":
    unittest.main()
