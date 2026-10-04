"""PlatformIO integration for the self-contained nRF54 Arduino platform."""

import os

from platformio.package.meta import PackageSpec
from platformio.public import PlatformBase


class Nrf54l15cleanPlatform(PlatformBase):
    def get_package_spec(self, name, version=None):
        if name == "tool-nrf54-gdb":
            # PlatformIO distributes Arm GDB inside the toolchain archive. Keep
            # its dependency separate from the core's GCC 7.2.1 compiler pin.
            return PackageSpec(
                owner="platformio", name="toolchain-gccarmnoneeabi",
                requirements=version or self.packages[name]["version"],
            )
        return super().get_package_spec(name, version)

    def configure_default_packages(self, options, targets):
        result = super().configure_default_packages(options, targets)
        if options.get("build_type") == "debug" or set(targets or ()) & {"__debug", "sizedata"}:
            self.packages["tool-nrf54-gdb"]["optional"] = False
        return result

    def update_packages(self):
        # The debugger's registry package has the compiler's name but a distinct
        # version. Resolve updates through manifest keys, not package names.
        for name in self.packages:
            pkg = self.get_package(name)
            if pkg:
                self.pm.update(pkg, to_spec=self.get_package_spec(name))

    def are_outdated_packages(self):
        return any(
            self.pm.outdated(pkg, self.get_package_spec(name)).is_outdated(allow_incompatible=False)
            for name in self.packages
            for pkg in [self.get_package(name)] if pkg
        )

    def get_gdb_path(self, install=False):
        package = self.get_package_dir("tool-nrf54-gdb")
        if package is None and install:
            self.install_package("tool-nrf54-gdb")
            package = self.get_package_dir("tool-nrf54-gdb")
        if package is None:
            return None
        return os.path.join(package, "bin", "arm-none-eabi-gdb" + (".exe" if os.name == "nt" else ""))

    def is_embedded(self):
        return True

    def framework_dir(self):
        return os.path.join(self.get_dir(), "hardware", "nrf54l15clean", "nrf54l15clean")

    def get_lib_storages(self):
        storages = super().get_lib_storages()
        storages.append({
            "name": "framework-arduino-nrf54l15clean",
            "path": os.path.join(self.framework_dir(), "libraries"),
        })
        return storages

    def get_boards(self, id_=None):
        boards = super().get_boards(id_)
        if not boards:
            return boards
        if id_:
            return self._add_debug_tools(boards)
        for board in boards.values():
            self._add_debug_tools(board)
        return boards

    def _add_debug_tools(self, board):
        debug = board.manifest.setdefault("debug", {})
        tools = debug.setdefault("tools", {})
        target = board.get("debug.pyocd_target", board.get("upload.target", "nrf54l"))
        for protocol in ("pyocd", "jlink"):
            tools.setdefault(protocol, {
                "default": protocol == "pyocd",
                "onboard": protocol == "pyocd" and board.id.startswith("xiao_"),
                "port": ":3333",
                "init_break": "tbreak setup",
                "init_cmds": [
                    "define pio_reset_halt_target", "monitor reset halt", "end",
                    "define pio_reset_run_target", "monitor reset", "end",
                    "target extended-remote $DEBUG_PORT",
                    "monitor reset halt", "$LOAD_CMDS", "monitor reset halt", "$INIT_BREAK",
                ],
                "server": {
                    "executable": "$PYTHONEXE",
                    "arguments": [
                        os.path.join(self.get_dir(), "builder", "upload.py"),
                        "--framework-dir", self.framework_dir(),
                        "--action", "debug", "--protocol", protocol,
                        "--target", target, "--elf", "$PROG_PATH",
                    ],
                    "ready_pattern": "GDB server listening on port",
                },
            })
        return board

    def configure_debug_session(self, debug_config):
        # Cached IDE metadata can predate installation of the debugger package.
        debug_config.build_data["gdb_path"] = self.get_gdb_path(install=True)
        if "debug_server" in debug_config.env_options or not debug_config.server:
            return
        if debug_config.tool_name not in ("pyocd", "jlink"):
            return
        args = debug_config.server["arguments"]
        uid = debug_config.env_options.get("board_upload.uid") or debug_config.board_config.get("upload.uid", None)
        if uid:
            args.extend(["--uid", str(uid)])
        elif debug_config.env_options.get("upload_port"):
            args.extend(["--port", debug_config.env_options["upload_port"]])
        if debug_config.speed:
            args.extend(["--frequency", str(int(debug_config.speed) * 1000)])
        port = debug_config.port
        if port:
            # The server always binds locally; debug_port changes its TCP port,
            # while board_upload.uid identifies the physical debug probe.
            host, separator, number = str(port).rpartition(":")
            if separator and host in ("", "localhost", "127.0.0.1") and number.isdecimal():
                args.extend(["--gdb-port", number])
            elif str(port).isdecimal():
                args.extend(["--gdb-port", str(port)])
                debug_config.port = ":" + str(port)
            else:
                raise ValueError(
                    "The bundled pyOCD server requires a local debug_port, e.g. localhost:3333. "
                    "Set debug_server explicitly to use a remote GDB server."
                )
