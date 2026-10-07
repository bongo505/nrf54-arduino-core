"""Resolve Arduino board menus without importing PlatformIO or SCons."""

import re
from pathlib import Path


FRAMEWORK_RELATIVE = "hardware/nrf54l15clean/nrf54l15clean"
_REFERENCE = re.compile(r"\{([^{}]+)\}")


def read_properties(path):
    properties = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("{}:{}: expected key=value".format(path, number))
        key, value = line.split("=", 1)
        key = key.strip()
        if key in properties:
            raise ValueError("{}:{}: duplicate property {}".format(path, number, key))
        properties[key] = value.strip()
    return properties


def board_ids(framework_dir):
    return [key[:-5] for key in read_properties(Path(framework_dir) / "boards.txt")
            if key.endswith(".name") and key.count(".") == 1]


def global_menu_ids(framework_dir):
    return [key[5:] for key in read_properties(Path(framework_dir) / "boards.txt")
            if key.startswith("menu.")]


def menu_options(framework_dir, board_id):
    properties = read_properties(Path(framework_dir) / "boards.txt")
    if board_id + ".name" not in properties:
        raise ValueError("Unknown nRF54 board: {}".format(board_id))
    prefix = board_id + ".menu."
    menus = {}
    for key in properties:
        if key.startswith(prefix):
            parts = key[len(prefix):].split(".")
            if len(parts) == 2:
                menus.setdefault(parts[0], []).append(parts[1])
    return menus


def resolve_board(framework_dir, board_id, options=None):
    framework_dir = Path(framework_dir).resolve()
    platform = read_properties(framework_dir / "platform.txt")
    boards = read_properties(framework_dir / "boards.txt")
    menus = menu_options(framework_dir, board_id)
    options = options or {}
    for menu, choice in options.items():
        if menu not in menus:
            raise ValueError("{} does not support menu {}".format(board_id, menu))
        if choice not in menus[menu]:
            raise ValueError("Invalid {}={!r} for {}; choose {}".format(
                menu, choice, board_id, ", ".join(menus[menu])))

    prefix = board_id + "."
    properties = dict(platform)
    properties.update({key[len(prefix):]: value for key, value in boards.items()
                       if key.startswith(prefix) and not key.startswith(prefix + "menu.")})
    for menu, choices in menus.items():
        choice_prefix = "{}menu.{}.{}.".format(prefix, menu, options.get(menu, choices[0]))
        properties.update({key[len(choice_prefix):]: value for key, value in boards.items()
                           if key.startswith(choice_prefix)})
    properties["runtime.platform.path"] = framework_dir.as_posix()

    def expand(key, parents=(), strict=True):
        if key in parents:
            raise ValueError("Cyclic Arduino property: " + " -> ".join(parents + (key,)))
        if key not in properties:
            if strict:
                raise ValueError("Unresolved Arduino property: {}".format(key))
            return "{" + key + "}"
        return _REFERENCE.sub(lambda match: expand(match.group(1), parents + (key,), strict),
                              properties[key])

    # Recipe strings also contain runtime-only placeholders such as build.path.
    # Resolve the hardware settings, not those Arduino CLI command templates.
    for key in tuple(properties):
        if key.startswith(("build.", "upload.")):
            properties[key] = expand(key, strict=key.startswith("build."))
    return properties
