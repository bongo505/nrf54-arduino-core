#!/usr/bin/env python3
"""PlatformIO transport adapter for the Arduino core's checked upload helper."""

import argparse
import importlib.util
import os
from pathlib import Path
import platform
import subprocess
import sys


PROTOCOLS = ("auto", "nrf_ocd", "pyocd", "pyocd_vm", "jlink", "uf2")


def native_host_supported():
    machine = platform.machine().lower()
    return (
        sys.platform.startswith("linux") and machine in ("x86_64", "amd64")
    ) or (
        sys.platform.startswith("win") and machine in ("amd64", "x86_64")
    )


def load_helper(framework_dir):
    path = Path(framework_dir).resolve() / "tools" / "upload.py"
    if not path.is_file():
        raise ValueError("The bundled upload helper is missing: %s" % path)
    spec = importlib.util.spec_from_file_location("nrf54_arduino_upload", str(path))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def resolve_protocol(protocol):
    if protocol == "auto":
        return "nrf_ocd" if native_host_supported() else "pyocd"
    if protocol == "nrf_ocd" and not native_host_supported():
        if not (os.environ.get("NRF54_NRF_OCD") or os.environ.get("OPEN_NRF_OCD")):
            raise ValueError(
                "The bundled nrf_ocd executable does not support this host. "
                "Use upload_protocol = pyocd, or set NRF54_NRF_OCD to a compatible executable."
            )
    return protocol


def upload_command(args, helper):
    protocol = resolve_protocol(args.protocol)
    runner = "pyocd" if protocol in ("pyocd_vm", "jlink") else protocol
    command = [
        sys.executable, str(Path(helper.__file__).resolve()),
        "--hex", str(Path(args.hex).resolve()),
        "--runner", runner,
        "--target", args.target,
        "--probe-type", "jlink" if protocol == "jlink" else "cmsisdap",
        "--pyocd-safe", (
            "true" if protocol == "pyocd_vm" else
            "false" if protocol == "jlink" else args.pyocd_safe
        ),
    ]
    for name in ("uf2", "port", "uid", "uf2_drive", "uf2_labels", "uf2_timeout"):
        value = getattr(args, name)
        if value is not None and str(value):
            command.extend(["--" + name.replace("_", "-"), str(value)])
    command.extend(args.extra)
    return command


def debug_command(args, helper, pyocd_command):
    if args.protocol not in ("auto", "pyocd", "jlink"):
        raise ValueError("Debugging supports pyocd or jlink; nrf_ocd and UF2 are upload-only.")
    probe_type = "jlink" if args.protocol == "jlink" else "cmsisdap"
    uid = helper.normalize_uid(args.uid)
    if uid is None and probe_type == "jlink":
        uid = helper.normalize_uid(os.environ.get("NRF54L15_JLINK_UID"))
    if uid is None and args.port:
        uid = helper.infer_uid_from_port(args.port)
        if uid is None:
            raise ValueError(
                "Cannot map the selected serial port to a debug probe. "
                "Set board_upload.uid to the intended probe's USB serial."
            )
    # The type-qualified empty UID also makes pyOCD reject ambiguous probe
    # selection instead of presenting an interactive picker inside the IDE.
    uid = helper.qualify_pyocd_probe_uid(uid, probe_type)
    command = list(pyocd_command) + [
        "gdbserver", "--no-wait", "--no-config", "--target", args.target,
        "--port", str(args.gdb_port), "--erase", "chip", "--uid", uid,
        "--frequency", str(args.frequency),
        "-O", "auto_unlock=false",
    ]
    command = helper.append_pyocd_target_script(command, args.target)
    command = helper.append_pyocd_probe_options(command, probe_type)
    if args.elf:
        command.extend(["--elf", str(Path(args.elf).resolve())])
    command.extend(args.extra)
    return command


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework-dir", required=True)
    parser.add_argument("--action", choices=("upload", "debug"), default="upload")
    parser.add_argument("--target", choices=("nrf54l", "nrf54lm20a"), required=True)
    parser.add_argument("--protocol", choices=PROTOCOLS, default="auto")
    parser.add_argument("--hex")
    parser.add_argument("--uf2")
    parser.add_argument("--port")
    parser.add_argument("--uid")
    parser.add_argument("--pyocd-safe", choices=("auto", "true", "false"), default="auto")
    parser.add_argument("--uf2-drive")
    parser.add_argument("--uf2-labels")
    parser.add_argument("--uf2-timeout", type=float)
    parser.add_argument("--frequency", type=int, default=1000000, help="Debug SWD clock in Hz")
    parser.add_argument("--gdb-port", type=int, default=3333)
    parser.add_argument("--elf")
    parser.add_argument("extra", nargs=argparse.REMAINDER, help="Transport arguments after --")
    args = parser.parse_args(argv)
    if args.extra[:1] == ["--"]:
        args.extra = args.extra[1:]
    if args.action == "upload" and not args.hex:
        parser.error("--hex is required for upload")
    if args.frequency <= 0 or not 1 <= args.gdb_port <= 65535:
        parser.error("--frequency must be positive and --gdb-port must be in 1..65535")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        helper = load_helper(args.framework_dir)
        if args.action == "upload":
            command = upload_command(args, helper)
        else:
            if args.protocol not in ("auto", "pyocd", "jlink"):
                raise ValueError("Debugging requires --protocol pyocd or jlink.")
            pyocd = helper.detect_pyocd_command()
            if pyocd is None:
                if not helper.install_pyocd():
                    raise ValueError("Unable to install the bundled pyOCD requirements.")
                pyocd = helper.detect_pyocd_command()
            if pyocd is None:
                raise ValueError("pyOCD is not available after installation.")
            command = debug_command(args, helper, pyocd)
        return subprocess.call(command)
    except (OSError, ValueError) as error:
        print("Error: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
