#!/usr/bin/env python3
"""Generate PlatformIO board metadata from the authoritative Arduino board file."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "builder"))
from board_config import FRAMEWORK_RELATIVE, board_ids, resolve_board


def board_manifest(framework, board_id):
    props = resolve_board(framework, board_id)
    vendor = ("Seeed Studio" if board_id.startswith("xiao_") else
              "HOLYIOT" if board_id.startswith("holyiot_") else
              "Nordic Semiconductor" if board_id.startswith("nrf54l15dk_") else "Generic")
    build = {
        "arduino": {"ldscript": props["build.ldscript"]},
        "core": props["build.core"],
        "cpu": props["build.mcu"],
        "f_cpu": props["build.f_cpu"],
        "mcu": "nrf54lm20a" if props["build.core"] == "nrf54lm20b" else "nrf54l15",
        "variant": props["build.variant"],
    }
    if "vid.0" in props:
        build["hwids"] = [[props["vid.0"], props["pid.0"]]]
    return {
        "build": build,
        "debug": {"default_tool": "pyocd", "onboard_tools": [], "svd_path": ""},
        "frameworks": ["arduino"],
        "name": props["name"],
        "upload": {
            "maximum_ram_size": int(props["upload.maximum_data_size"]),
            "maximum_size": int(props["upload.maximum_size"]),
            "protocol": "auto",
            "protocols": ["auto", "nrf_ocd", "pyocd", "pyocd_vm", "jlink", "uf2", "custom"],
            "require_upload_port": False,
            "target": props["upload.target"],
        },
        "url": "https://github.com/lolren/nrf54-arduino-core",
        "vendor": vendor,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    framework = ROOT / FRAMEWORK_RELATIVE
    board_dir = ROOT / "boards"
    ids = board_ids(framework)
    failed = []
    for board_id in ids:
        path = board_dir / (board_id + ".json")
        content = json.dumps(board_manifest(framework, board_id), indent=2) + "\n"
        if args.check:
            if not path.is_file() or path.read_text() != content:
                failed.append(str(path.relative_to(ROOT)))
        else:
            board_dir.mkdir(exist_ok=True)
            path.write_text(content)
    stale = [path.name for path in board_dir.glob("*.json") if path.stem not in ids]
    if failed or stale:
        parser.exit(1, "PlatformIO boards out of sync: {}. Run scripts/sync_platformio_boards.py\n".format(
            ", ".join(failed + stale)))
    print("PlatformIO board metadata {}: {} boards".format("verified" if args.check else "generated", len(ids)))


if __name__ == "__main__":
    main()
