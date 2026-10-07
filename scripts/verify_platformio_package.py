#!/usr/bin/env python3
"""Build real sketches with PlatformIO, including from the exact release package."""

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK_RELATIVE = "hardware/nrf54l15clean/nrf54l15clean"
HAL_EXAMPLES = "libraries/Nrf54L15-Clean-Implementation/examples/"


def run(command, cwd, environment, timeout):
    result = subprocess.run(command, cwd=cwd, env=environment, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=timeout)
    if result.returncode:
        raise RuntimeError("Command failed: {}\n{}".format(" ".join(command), result.stdout))
    return result.stdout


def extract_package(archive, destination):
    with tarfile.open(archive) as package:
        for member in package.getmembers():
            path = (destination / member.name).resolve()
            if (not path.is_relative_to(destination.resolve()) or
                    not (member.isfile() or member.isdir())):
                raise ValueError("Unsafe or linked PlatformIO archive entry: " + member.name)
        package.extractall(destination)
    candidates = list(destination.glob("*/platform.json"))
    if len(candidates) != 1:
        raise ValueError("PlatformIO archive must have exactly one root platform.json")
    return candidates[0].parent


def verify_manifest(archive, manifest):
    entry = json.loads(manifest.read_text())["platformio"]
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if (entry["archiveFileName"] != archive.name or
            entry["checksum"] != "SHA-256:" + actual or
            int(entry["size"]) != archive.stat().st_size):
        raise ValueError("PlatformIO archive disagrees with release manifest")


def verify_artifacts(build_dir, metadata, board, has_hal):
    for suffix in ("elf", "hex", "bin", "uf2", "map"):
        path = build_dir / ("firmware." + suffix)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Missing build artifact: " + str(path))
    uf2 = (build_dir / "firmware.uf2").read_bytes()
    family = 0xADA54B20 if board == "xiao_nrf54lm20b" else 0xADA54B15
    if len(uf2) % 512:
        raise ValueError("Truncated UF2")
    for offset in range(0, len(uf2), 512):
        header = struct.unpack_from("<8I", uf2, offset)
        if (header[0:2] != (0x0A324655, 0x9E5D5157) or header[7] != family or
                struct.unpack_from("<I", uf2, offset + 508)[0] != 0x0AB16F30):
            raise ValueError("Invalid UF2 header/family")
    compiler = Path(metadata["cc_path"])
    nm = compiler.with_name("arm-none-eabi-nm" + compiler.suffix)
    symbols = subprocess.check_output([str(nm), "-C", str(build_dir / "firmware.elf")], text=True)
    core = "nrf54lm20" if board == "xiao_nrf54lm20b" else "nrf54l15"
    if "__nrf54_core_target_" + core not in symbols:
        raise ValueError("Missing SoC link guard")
    if not re.search(r"\bT initVariant$", symbols, re.MULTILINE):
        raise ValueError("Variant initialization was lost during linking")
    if has_hal and not re.search(r"\bT RADIO_0_IRQHandler$", symbols, re.MULTILINE):
        raise ValueError("HAL strong radio IRQ handler was lost during linking")
    for flags in (metadata["cc_flags"], metadata["cxx_flags"]):
        for header in ("CoreVersionGenerated.h", "BuildTargetGuard.h"):
            if not any(flag.endswith(header) for flag in flags):
                raise ValueError("Missing forced header: " + header)


def cases(platform, extended):
    result = []
    for board in ("xiao_nrf54l15", "xiao_nrf54lm20b"):
        result.extend([
            (board, "libraries/Bluefruit52Lib/examples/HID/blehid_mouse", {}, True),
            (board, "examples/Wire/WireScanner", {}, False),
        ])
    result.append(("xiao_nrf54l15", None, {"build_type": "debug"}, False))
    if extended:
        for board in sorted((platform / "boards").glob("*.json")):
            result.append((board.stem, None, {}, False))
        result.append(("xiao_nrf54l15", None, {"clean_vpr": "off", "clean_ble": "off", "cpu_freq": "128m"}, False))
        for board, role in (("xiao_nrf54l15", "Initiator"), ("xiao_nrf54lm20b", "Reflector")):
            result.append((board, HAL_EXAMPLES + "BLE/ChannelSounding/BleChannelSounding" + role,
                           {"cpu_freq": "128m"}, True))
            result.append((board, HAL_EXAMPLES + "Thread/OpenThreadCoreStageProbe",
                           {"clean_thread": "stage"}, True))
            result.append((board, HAL_EXAMPLES + "Matter/MatterOnOffLightFoundationCompileTarget",
                           {"clean_thread": "stage", "clean_matter": "stage"}, True))
    return result


def verify(platform, package_spec, work, extended, timeout):
    environment = dict(os.environ)
    # Keep the compiler download cache, but never reuse an installed platform.
    environment["PLATFORMIO_PLATFORMS_DIR"] = str(work / "platforms")
    environment["PLATFORMIO_GLOBALLIB_DIR"] = str(work / "global-libraries")
    pio = [sys.executable, "-m", "platformio"]
    for index, (board, sketch, options, has_hal) in enumerate(cases(platform, extended)):
        project = work / ("p" + str(index))
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        config = "[env:verify]\nplatform = {}\nboard = {}\nframework = arduino\n".format(package_spec, board)
        for key, value in options.items():
            config += "{}{} = {}\n".format("" if key == "build_type" else "board_build.", key, value)
        (project / "platformio.ini").write_text(config)
        if sketch:
            source = platform / FRAMEWORK_RELATIVE / sketch
            for path in source.iterdir():
                if path.suffix in (".ino", ".cpp", ".c", ".h", ".hpp"):
                    shutil.copy2(path, src / path.name)
        else:
            shutil.copy2(platform / "examples/platformio/blink/src/main.cpp", src / "main.cpp")
        output = run(pio + ["run", "-j", "4"], project, environment, timeout)
        (project / "build.log").write_text(output)
        metadata = json.loads(run(pio + ["project", "metadata", "--json-output"],
                                  project, environment, timeout))["verify"]
        verify_artifacts(project / ".pio/build/verify", metadata, board, has_hal)
        if options.get("build_type") == "debug":
            if "-Os" in metadata["cxx_flags"] or "-Og" not in metadata["cxx_flags"]:
                raise ValueError("Debug build did not retain PlatformIO debug optimization")
            debugger = run([metadata["gdb_path"], "--batch", "-ex", "set architecture armv8-m.main",
                            "-ex", "show architecture"], project, environment, timeout)
            if "armv8-m.main" not in debugger or "Undefined" in debugger:
                raise ValueError("Debugger does not support Cortex-M33 register descriptions")
        print("PlatformIO OK: {} {} {}".format(board, sketch or "Blink", options), flush=True)
    print("PlatformIO build, library discovery, ELF guards/IRQ/variant and UF2 verification passed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", type=Path)
    source.add_argument("--platform", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--extended", action="store_true", help="Also build all boards and staged protocol targets")
    parser.add_argument("--work-dir", type=Path, help="Retain projects and logs in this directory")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="p54-") as temporary:
        work = args.work_dir.resolve() if args.work_dir else Path(temporary)
        work.mkdir(parents=True, exist_ok=True)
        if args.archive:
            archive = args.archive.resolve()
            if args.manifest:
                verify_manifest(archive, args.manifest)
            platform = extract_package(archive, work / "unpacked")
            package_spec = archive.as_uri()
        else:
            platform = args.platform.resolve()
            package_spec = "symlink://" + platform.as_posix()
        verify(platform, package_spec, work, args.extended, args.timeout)


if __name__ == "__main__":
    main()
