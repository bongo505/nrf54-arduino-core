#!/usr/bin/env python3
"""Hardware-free executable contracts for PlatformIO upload and debug routing."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK = ROOT / "hardware" / "nrf54l15clean" / "nrf54l15clean"
SPACE_DIR = ROOT / "test build with spaces"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UPLOAD = load_module("pio_transport_test_adapter", ROOT / "builder" / "upload.py")
HELPER = UPLOAD.load_helper(FRAMEWORK)


class Board:
    MISSING = object()

    def __init__(self, board_id="xiao_nrf54l15", target="nrf54l"):
        self.id = board_id
        self.manifest = {"upload": {"target": target}}

    def get(self, key, default=MISSING):
        value = self.manifest
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                if default is self.MISSING:
                    raise KeyError(key)
                return default
            value = value[part]
        return value


class PlatformBaseStub:
    def __init__(self):
        self.boards = {}
        self.packages = json.loads((ROOT / "platform.json").read_text())["packages"]
        self.pm = MagicMock()

    def get_package_spec(self, name, version=None):
        return types.SimpleNamespace(
            owner=self.packages[name].get("owner"), name=name,
            requirements=version or self.packages[name].get("version"),
        )

    def configure_default_packages(self, options, targets):
        return None

    def get_package_dir(self, name):
        return "/packages/compatible-debugger"

    def get_package(self, name):
        return types.SimpleNamespace(name=name)

    def install_package(self, name):
        raise AssertionError("Test must mock package installation")

    def get_dir(self):
        return str(ROOT)

    def get_lib_storages(self):
        return [{"name": "existing", "path": "/existing"}]

    def get_boards(self, id_=None):
        return self.boards.get(id_) if id_ else self.boards


def load_platform_without_platformio_dependency():
    public = types.ModuleType("platformio.public")
    public.PlatformBase = PlatformBaseStub
    meta = types.ModuleType("platformio.package.meta")
    meta.PackageSpec = types.SimpleNamespace
    with patch.dict(sys.modules, {"platformio.public": public, "platformio.package.meta": meta}):
        return load_module("pio_transport_test_platform", ROOT / "platform.py")


PLATFORM = load_platform_without_platformio_dependency()


class CommandTests(unittest.TestCase):
    def args(self, *extra):
        return UPLOAD.parse_args([
            "--framework-dir", str(FRAMEWORK), "--target", "nrf54l",
            "--hex", str(SPACE_DIR / "firmware.hex"), *extra,
        ])

    def test_host_default_does_not_execute_wrong_architecture(self):
        for host, machine, expected in (
            ("linux", "x86_64", "nrf_ocd"),
            ("linux", "aarch64", "pyocd"),
            ("linux", "armv7l", "pyocd"),
            ("darwin", "x86_64", "pyocd"),
            ("darwin", "arm64", "pyocd"),
            ("win32", "AMD64", "nrf_ocd"),
            ("win32", "x86", "pyocd"),
            ("win32", "ARM64", "pyocd"),
        ):
            with self.subTest(host=host, machine=machine):
                with patch.object(UPLOAD.sys, "platform", host), patch.object(UPLOAD.platform, "machine", return_value=machine):
                    self.assertEqual(UPLOAD.resolve_protocol("auto"), expected)

    def test_unsupported_native_host_requires_explicit_binary_override(self):
        with patch.object(UPLOAD, "native_host_supported", return_value=False), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "compatible executable"):
                UPLOAD.resolve_protocol("nrf_ocd")
            os.environ["NRF54_NRF_OCD"] = "/custom/native binary"
            self.assertEqual(UPLOAD.resolve_protocol("nrf_ocd"), "nrf_ocd")

    def test_upload_protocol_mapping(self):
        for protocol, runner, probe, safe in (
            ("nrf_ocd", "nrf_ocd", "cmsisdap", "auto"),
            ("pyocd", "pyocd", "cmsisdap", "auto"),
            ("pyocd_vm", "pyocd", "cmsisdap", "true"),
            ("jlink", "pyocd", "jlink", "false"),
            ("uf2", "uf2", "cmsisdap", "auto"),
        ):
            with self.subTest(protocol=protocol), patch.object(UPLOAD, "native_host_supported", return_value=True):
                command = UPLOAD.upload_command(self.args("--protocol", protocol), HELPER)
                self.assertEqual(command[command.index("--runner") + 1], runner)
                self.assertEqual(command[command.index("--probe-type") + 1], probe)
                self.assertEqual(command[command.index("--pyocd-safe") + 1], safe)
                self.assertNotIn("erase", command)
                self.assertNotIn("--auto-unlock", command)

    def test_upload_paths_options_and_extra_flags_are_separate_arguments(self):
        args = self.args(
            "--protocol", "pyocd", "--target", "nrf54lm20a",
            "--uf2", str(SPACE_DIR / "firmware.uf2"), "--port", "COM12",
            "--uid", "a1b2c3", "--uf2-drive", str(SPACE_DIR / "UF2 drive"),
            "--uf2-labels", "UF2BOOT,XIAO-SENSE", "--uf2-timeout", "4.5",
            "--pyocd-safe", "true", "--", "--retries", "1", "--retry-delay", "0.2",
        )
        command = UPLOAD.upload_command(args, HELPER)
        for flag, value in (
            ("--hex", str(SPACE_DIR / "firmware.hex")),
            ("--uf2", str(SPACE_DIR / "firmware.uf2")),
            ("--port", "COM12"), ("--uid", "a1b2c3"),
            ("--target", "nrf54lm20a"), ("--uf2-drive", str(SPACE_DIR / "UF2 drive")),
            ("--uf2-timeout", "4.5"), ("--pyocd-safe", "true"),
        ):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(command[-4:], ["--retries", "1", "--retry-delay", "0.2"])

    def test_debug_lm20_target_hook_uid_and_no_implicit_unlock(self):
        command = UPLOAD.debug_command(self.args(
            "--action", "debug", "--protocol", "jlink", "--target", "nrf54lm20a",
            "--uid", "00001234", "--elf", str(SPACE_DIR / "debug app.elf"), "--frequency", "2000000",
        ), HELPER, ["python", "pyocd wrapper.py"])
        self.assertEqual(command[:3], ["python", "pyocd wrapper.py", "gdbserver"])
        self.assertEqual(command[command.index("--uid") + 1], "jlink:1234")
        self.assertEqual(command[command.index("--elf") + 1], str(SPACE_DIR / "debug app.elf"))
        self.assertEqual(command[command.index("--frequency") + 1], "2000000")
        self.assertEqual(Path(command[command.index("--script") + 1]), FRAMEWORK / "tools" / "pyocd_register_lm20b.py")
        self.assertIn("auto_unlock=false", command)
        self.assertIn("jlink.power=false", command)
        self.assertIn("jlink.non_interactive=true", command)
        self.assertNotIn("load", command)

    def test_debug_l15_no_target_script_and_type_qualified_uid(self):
        with patch.dict(os.environ, {}, clear=True):
            command = UPLOAD.debug_command(self.args("--action", "debug", "--protocol", "pyocd"), HELPER, ["pyocd"])
        self.assertNotIn("--script", command)
        self.assertIn("--no-wait", command)
        self.assertEqual(command[command.index("--uid") + 1], "cmsisdap:")

    def test_installed_pyocd_rejects_ambiguous_type_qualified_uid(self):
        try:
            from pyocd.core.helpers import ConnectHelper
        except ImportError:
            self.skipTest("Optional installed-pyOCD behavior check; no hardware is accessed")
        probes = [types.SimpleNamespace(unique_id=uid, description="Mock probe") for uid in ("AA", "BB")]
        with patch.object(ConnectHelper, "get_all_connected_probes", return_value=probes), patch("builtins.input", side_effect=AssertionError("must not prompt")), contextlib.redirect_stdout(io.StringIO()):
            for uid in ("cmsisdap:", "jlink:"):
                self.assertIsNone(ConnectHelper.choose_probe(blocking=False, return_first=False, unique_id=uid))

    def test_debug_inferred_serial_uid_and_explicit_uid_priority(self):
        with patch.object(HELPER, "infer_uid_from_port", return_value="probe-id") as infer:
            command = UPLOAD.debug_command(self.args("--action", "debug", "--port", "COM3"), HELPER, ["pyocd"])
            self.assertEqual(command[command.index("--uid") + 1], "cmsisdap:probe-id")
            infer.assert_called_once_with("COM3")
        with patch.object(HELPER, "infer_uid_from_port", side_effect=AssertionError("must not infer")):
            command = UPLOAD.debug_command(self.args("--action", "debug", "--port", "COM3", "--uid", "explicit"), HELPER, ["pyocd"])
            self.assertEqual(command[command.index("--uid") + 1], "cmsisdap:explicit")

    def test_debug_jlink_environment_uid(self):
        with patch.dict(os.environ, {"NRF54L15_JLINK_UID": "000456"}):
            command = UPLOAD.debug_command(self.args("--action", "debug", "--protocol", "jlink"), HELPER, ["pyocd"])
        self.assertEqual(command[command.index("--uid") + 1], "jlink:456")

    def test_debug_invalid_protocol_or_conflicting_uid(self):
        for protocol in ("nrf_ocd", "uf2", "pyocd_vm"):
            with self.subTest(protocol=protocol), self.assertRaises(ValueError):
                UPLOAD.debug_command(self.args("--action", "debug", "--protocol", protocol), HELPER, ["pyocd"])
        with self.assertRaisesRegex(ValueError, "conflicts"):
            UPLOAD.debug_command(self.args("--action", "debug", "--protocol", "pyocd", "--uid", "jlink:123"), HELPER, ["pyocd"])
        with patch.object(HELPER, "infer_uid_from_port", return_value=None), self.assertRaisesRegex(ValueError, "Cannot map"):
            UPLOAD.debug_command(self.args("--action", "debug", "--port", "COM123"), HELPER, ["pyocd"])

    def test_invalid_cli_values_fail_before_loading_helper(self):
        common = ["--framework-dir", str(FRAMEWORK), "--target", "nrf54l"]
        for suffix in ([], ["--hex", "a.hex", "--frequency", "0"], ["--hex", "a.hex", "--gdb-port", "65536"], ["--hex", "a.hex", "--protocol", "openocd"]):
            with self.subTest(suffix=suffix), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                UPLOAD.parse_args(common + suffix)

    def test_missing_framework_is_actionable(self):
        with self.assertRaisesRegex(ValueError, "upload helper is missing"):
            UPLOAD.load_helper(ROOT / "does-not-exist")

    def test_main_upload_never_installs_debugger_itself(self):
        argv = ["--framework-dir", str(FRAMEWORK), "--target", "nrf54l", "--protocol", "pyocd", "--hex", "test.hex"]
        with patch.object(UPLOAD, "load_helper", return_value=HELPER), patch.object(HELPER, "install_pyocd", side_effect=AssertionError("not debug")), patch.object(UPLOAD.subprocess, "call", return_value=17) as call:
            self.assertEqual(UPLOAD.main(argv), 17)
            self.assertEqual(call.call_args.args[0][1], str(FRAMEWORK / "tools" / "upload.py"))

    def test_main_debug_bootstrap_only_when_needed(self):
        argv = ["--framework-dir", str(FRAMEWORK), "--action", "debug", "--target", "nrf54l", "--protocol", "pyocd"]
        with patch.object(UPLOAD, "load_helper", return_value=HELPER), patch.object(HELPER, "detect_pyocd_command", side_effect=[None, ["pyocd"]]), patch.object(HELPER, "install_pyocd", return_value=True) as install, patch.object(UPLOAD.subprocess, "call", return_value=0):
            self.assertEqual(UPLOAD.main(argv), 0)
            install.assert_called_once_with()
        with patch.object(UPLOAD, "load_helper", return_value=HELPER), patch.object(HELPER, "detect_pyocd_command", return_value=["pyocd"]), patch.object(HELPER, "install_pyocd", side_effect=AssertionError("already installed")), patch.object(UPLOAD.subprocess, "call", return_value=0):
            self.assertEqual(UPLOAD.main(argv), 0)

    def test_main_debug_failed_bootstrap_never_launches(self):
        argv = ["--framework-dir", str(FRAMEWORK), "--action", "debug", "--target", "nrf54l"]
        with patch.object(UPLOAD, "load_helper", return_value=HELPER), patch.object(HELPER, "detect_pyocd_command", return_value=None), patch.object(HELPER, "install_pyocd", return_value=False), patch.object(UPLOAD.subprocess, "call", side_effect=AssertionError("must not launch")), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(UPLOAD.main(argv), 1)
            self.assertIn("Unable to install", err.getvalue())


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.platform = PLATFORM.Nrf54l15cleanPlatform()

    def config(self, **changes):
        config = types.SimpleNamespace(
            tool_name="pyocd", env_options={}, board_config=Board(),
            server={"arguments": ["wrapper.py"]}, speed=None, port=":3333",
            build_data={"gdb_path": "/stale/old-gdb"},
        )
        for key, value in changes.items():
            setattr(config, key, value)
        return config

    def test_embedded_library_storage_keeps_parent_storage(self):
        self.assertTrue(self.platform.is_embedded())
        storages = self.platform.get_lib_storages()
        self.assertEqual(storages[0]["name"], "existing")
        self.assertEqual(storages[1]["path"], str(FRAMEWORK / "libraries"))

    def test_debugger_alias_keeps_compiler_version_separate(self):
        compiler = self.platform.get_package_spec("toolchain-gccarmnoneeabi")
        debugger = self.platform.get_package_spec("tool-nrf54-gdb")
        self.assertEqual(compiler.requirements, "~1.70201.0")
        self.assertEqual(debugger.name, "toolchain-gccarmnoneeabi")
        self.assertEqual(debugger.owner, "platformio")
        self.assertEqual(debugger.requirements, "1.90301.200702")

    def test_debugger_dependency_is_only_required_for_debug(self):
        for options, targets, required in (
            ({"build_type": "release"}, [], False),
            ({"build_type": "debug"}, [], True),
            ({}, ["__debug"], True),
            ({}, ["sizedata"], True),
            ({}, ["upload"], False),
        ):
            with self.subTest(options=options, targets=targets):
                platform = PLATFORM.Nrf54l15cleanPlatform()
                platform.configure_default_packages(options, targets)
                self.assertEqual(platform.packages["tool-nrf54-gdb"]["optional"], not required)

    def test_debugger_install_is_lazy_and_fixes_cached_metadata(self):
        with patch.object(self.platform, "get_package_dir", return_value=None), patch.object(self.platform, "install_package") as install:
            self.assertIsNone(self.platform.get_gdb_path())
            install.assert_not_called()
        config = self.config()
        with patch.object(self.platform, "get_package_dir", side_effect=[None, "/packages/new-gdb"]), patch.object(self.platform, "install_package") as install:
            self.platform.configure_debug_session(config)
            install.assert_called_once_with("tool-nrf54-gdb")
        self.assertEqual(config.build_data["gdb_path"], os.path.join(
            "/packages/new-gdb", "bin", "arm-none-eabi-gdb" + (".exe" if os.name == "nt" else "")))

    def test_package_updates_do_not_replace_debugger_with_compiler(self):
        self.platform.update_packages()
        versions = [call.kwargs["to_spec"].requirements for call in self.platform.pm.update.call_args_list]
        self.assertEqual(versions, ["~1.70201.0", "1.90301.200702"])
        self.platform.pm.outdated.return_value.is_outdated.return_value = False
        self.assertFalse(self.platform.are_outdated_packages())
        versions = [call.args[1].requirements for call in self.platform.pm.outdated.call_args_list]
        self.assertEqual(versions, ["~1.70201.0", "1.90301.200702"])

    def test_boards_get_debug_tools_without_installation(self):
        l15 = Board()
        lm20 = Board("xiao_nrf54lm20a", "nrf54lm20a")
        self.platform.boards = {l15.id: l15, lm20.id: lm20}
        with patch.object(HELPER, "install_pyocd", side_effect=AssertionError("metadata only")):
            self.assertIs(self.platform.get_boards(l15.id), l15)
            self.assertEqual(len(self.platform.get_boards()), 2)
        tools = lm20.manifest["debug"]["tools"]
        self.assertEqual(set(tools), {"pyocd", "jlink"})
        self.assertTrue(tools["pyocd"]["default"])
        self.assertFalse(tools["jlink"]["default"])
        self.assertEqual(tools["pyocd"]["server"]["executable"], "$PYTHONEXE")
        self.assertIn("nrf54lm20a", tools["pyocd"]["server"]["arguments"])
        self.assertNotIn("monitor init", tools["pyocd"]["init_cmds"])
        self.assertIn("$LOAD_CMDS", tools["pyocd"]["init_cmds"])
        self.assertIn("GDB server listening", tools["pyocd"]["server"]["ready_pattern"])
        self.assertIsNone(self.platform.get_boards("missing"))

    def test_existing_board_debug_tool_is_not_overwritten(self):
        board = Board()
        existing = {"server": {"executable": "custom-server"}}
        board.manifest["debug"] = {"tools": {"pyocd": existing}}
        self.platform._add_debug_tools(board)
        self.assertIs(board.manifest["debug"]["tools"]["pyocd"], existing)

    def test_probe_uid_speed_and_tcp_port(self):
        config = self.config(env_options={"board_upload.uid": "probe123", "upload_port": "COM7"}, speed="4000", port="localhost:3344")
        self.platform.configure_debug_session(config)
        self.assertEqual(config.server["arguments"], ["wrapper.py", "--uid", "probe123", "--frequency", "4000000", "--gdb-port", "3344"])

    def test_serial_port_and_numeric_debug_port(self):
        config = self.config(env_options={"upload_port": "/dev/ttyACM1"}, port="3345")
        self.platform.configure_debug_session(config)
        self.assertEqual(config.server["arguments"], ["wrapper.py", "--port", "/dev/ttyACM1", "--gdb-port", "3345"])
        self.assertEqual(config.port, ":3345")

    def test_custom_server_and_other_tool_are_untouched(self):
        for overrides in ({"debug_server": ["custom-server"]}, {"debug_server": ""}):
            config = self.config(env_options=overrides, speed="1000", port="remote:4444")
            self.platform.configure_debug_session(config)
            self.assertEqual(config.server["arguments"], ["wrapper.py"])
        config = self.config(tool_name="custom", port="remote:4444")
        self.platform.configure_debug_session(config)
        self.assertEqual(config.server["arguments"], ["wrapper.py"])
        self.platform.configure_debug_session(self.config(server=None))

    def test_remote_debug_port_requires_external_server(self):
        with self.assertRaisesRegex(ValueError, "remote GDB server"):
            self.platform.configure_debug_session(self.config(port="192.0.2.1:3333"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
