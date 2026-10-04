#!/usr/bin/env python3
"""Host-side PlatformIO board/menu parity tests; no tool downloads or hardware."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "builder"))
from board_config import (FRAMEWORK_RELATIVE, board_ids, global_menu_ids,
                          menu_options, read_properties, resolve_board)

FRAMEWORK = ROOT / FRAMEWORK_RELATIVE


class BoardConfigurationTests(unittest.TestCase):
    def test_generated_manifests(self):
        subprocess.run([sys.executable, str(ROOT / "scripts/sync_platformio_boards.py"),
                        "--check"], check=True)

    def test_default_parity(self):
        ids = board_ids(FRAMEWORK)
        self.assertEqual(len(ids), 6)
        for board in ids:
            props = resolve_board(FRAMEWORK, board)
            manifest = json.loads((ROOT / "boards" / (board + ".json")).read_text())
            self.assertEqual(manifest["upload"]["target"], props["upload.target"])
            self.assertEqual(manifest["build"]["core"], props["build.core"])
            self.assertNotIn("{", props["build.extra_flags"])
            self.assertTrue((FRAMEWORK / "cores" / props["build.core"] /
                             props["build.ldscript"]).is_file())

    def test_every_menu_choice_resolves(self):
        for board in board_ids(FRAMEWORK):
            for menu, choices in menu_options(FRAMEWORK, board).items():
                for choice in choices:
                    with self.subTest(board=board, menu=menu, choice=choice):
                        props = resolve_board(FRAMEWORK, board, {menu: choice})
                        for key, value in props.items():
                            if key.startswith("build."):
                                self.assertNotIn("{", value)

    def test_invalid_configuration_rejected(self):
        for board, options in (("missing", {}), ("xiao_nrf54l15", {"cpu_freq": "200m"}),
                               ("xiao_nrf54lm20b", {"clean_vpr": "on"}),
                               ("xiao_nrf54l15", {"clean_bel": "on"})):
            with self.assertRaises(ValueError):
                resolve_board(FRAMEWORK, board, options)

    def test_memory_and_soc_switches(self):
        default = resolve_board(FRAMEWORK, "xiao_nrf54l15")
        without_vpr = resolve_board(FRAMEWORK, "xiao_nrf54l15", {"clean_vpr": "off", "cpu_freq": "128m"})
        lm20 = resolve_board(FRAMEWORK, "xiao_nrf54lm20b")
        self.assertEqual(default["upload.maximum_data_size"], "155648")
        self.assertEqual(without_vpr["upload.maximum_data_size"], "261632")
        self.assertIn("no_vpr", without_vpr["build.ldscript"])
        self.assertEqual(without_vpr["build.f_cpu"], "128000000L")
        self.assertEqual(lm20["build.nordic_sdc_arch"], "nrf54lm")
        self.assertEqual(lm20["upload.maximum_data_size"], "523584")

    def test_staged_flags_and_disabled_ble(self):
        props = resolve_board(FRAMEWORK, "xiao_nrf54l15", {
            "clean_thread": "stage", "clean_matter": "stage", "clean_ble": "off"})
        for flag in ("NRF54L15_CLEAN_OPENTHREAD_CORE_ENABLE=1",
                     "NRF54L15_CLEAN_MATTER_CORE_ENABLE=1", "NRF54L15_CLEAN_BLE_DISABLED"):
            self.assertIn(flag, props["build.extra_flags"])
        self.assertIn("cpu_freq", global_menu_ids(FRAMEWORK))

    def test_duplicate_properties_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "properties.txt"
            path.write_text("test=1\ntest=2\n")
            with self.assertRaises(ValueError):
                read_properties(path)

    def test_version_and_embedded_framework(self):
        manifest = json.loads((ROOT / "platform.json").read_text())
        self.assertEqual(manifest["version"], read_properties(FRAMEWORK / "platform.txt")["version"])
        self.assertNotIn("package", manifest["frameworks"]["arduino"])
        self.assertEqual(manifest["packages"]["toolchain-gccarmnoneeabi"]["version"], "~1.70201.0")


if __name__ == "__main__":
    unittest.main()
