"""Native PlatformIO build targets for the nRF54 Arduino core."""

import os
import subprocess
import sys
from pathlib import Path

from SCons.Script import AlwaysBuild, Builder, COMMAND_LINE_TARGETS, Default, DefaultEnvironment


env = DefaultEnvironment()
platform = env.PioPlatform()
platform_dir = Path(platform.get_dir())
sys.path.insert(0, str(platform_dir / "builder"))
from board_config import global_menu_ids, resolve_board


framework_dir = platform_dir / "hardware" / "nrf54l15clean" / "nrf54l15clean"
board = env.BoardConfig()
options = {}
menus = global_menu_ids(framework_dir)
for key in board.get("build", {}):
    if key.startswith("clean_") and key not in menus:
        raise ValueError("Unknown nRF54 board menu: board_build." + key)
for menu in menus:
    value = board.get("build." + menu, None)
    if value is not None:
        options[menu] = str(value)

# Accept the conventional PlatformIO frequency override without letting it
# diverge from the core's compile-time clock selection.
frequency = env.GetProjectOption("board_build.f_cpu", None)
if frequency is not None:
    frequencies = {"64000000": "64m", "128000000": "128m"}
    selected = frequencies.get(str(frequency).rstrip("Ll"))
    if selected is None:
        raise ValueError("nRF54 supports board_build.f_cpu = 64000000L or 128000000L")
    if "cpu_freq" in options and options["cpu_freq"] != selected:
        raise ValueError("board_build.f_cpu conflicts with board_build.cpu_freq")
    options["cpu_freq"] = selected

properties = resolve_board(framework_dir, board.id, options)
for key in ("build.core", "build.variant", "build.f_cpu"):
    board.update(key, properties[key])
board.update("upload.maximum_size", int(properties["upload.maximum_size"]))
board.update("upload.maximum_ram_size", int(properties["upload.maximum_data_size"]))
runtime_path = properties["runtime.platform.path"]
board.update("build.extra_flags", properties["build.extra_flags"].replace(
    runtime_path, '"' + runtime_path + '"'))

toolchain_dir = Path(platform.get_package_dir("toolchain-gccarmnoneeabi")) / "bin"


def compiler_tool(name):
    return str(toolchain_dir / ("arm-none-eabi-" + name + (".exe" if os.name == "nt" else "")))


# The optional compatible GDB package also contains a newer compiler. Absolute
# tool paths prevent its PATH entry from changing firmware code generation.
env.Replace(
    NRF54_FRAMEWORK_DIR=str(framework_dir),
    NRF54_BOARD_PROPERTIES=properties,
    BOARD_F_CPU=properties["build.f_cpu"],
    AR=[compiler_tool("ar")],
    AS=[compiler_tool("gcc")],
    CC=[compiler_tool("gcc")],
    CXX=[compiler_tool("g++")],
    LINK="$CC",
    GDB=platform.get_gdb_path() or compiler_tool("gdb"),
    OBJCOPY=compiler_tool("objcopy"),
    RANLIB=[compiler_tool("ranlib")],
    SIZETOOL=[compiler_tool("size")],
    ARFLAGS=["rcs"],
    PROGSUFFIX=".elf",
    SIZEPROGREGEXP=r"^(?:\.isr_vector|\.text|\.rodata|\.data|\.ARM|\.ARM\.extab|\.ARM\.exidx|\.preinit_array|\.init_array|\.fini_array|\.nrf54_core_target_guard)\s+(\d+).*",
    SIZEDATAREGEXP=r"^(?:\.data|\.bss|\.noinit|\._user_heap_stack)\s+(\d+).*",
    SIZECHECKCMD="$SIZETOOL -A -d $SOURCES",
    SIZEPRINTCMD="$SIZETOOL -B -d $SOURCES",
)
if env.get("PROGNAME", "program") == "program":
    env.Replace(PROGNAME="firmware")


def is_flash_section(env, section):
    # Constructor tables and ARM exception indexes are stored in RRAM too,
    # although ELF does not classify them as ordinary PROGBITS sections.
    return "A" in section.get("flags", "") and section.get("type") != "SHT_NOBITS"


def is_ram_section(env, section):
    address = section.get("start_addr", 0)
    return ("A" in section.get("flags", "") and
            0x20000000 <= address < 0x20000000 + int(properties["upload.maximum_data_size"]))


env.AddMethod(is_flash_section, "pioSizeIsFlashSection")
env.AddMethod(is_ram_section, "pioSizeIsRamSection")

if env.get("PIOFRAMEWORK") != ["arduino"]:
    raise ValueError("This platform supports framework = arduino only")

# Arduino links library objects directly. Lazy archive extraction can lose
# strong IRQ handlers whose references already have weak startup definitions.
config = env.GetProjectConfig()
section = "env:" + env["PIOENV"]
# HAL implementation units reference Preferences without exposing it in their
# public headers. Use all-source scanning; PIO's conditional scanner can miss
# this dependency behind the staged Thread compile-time guards.
if config.getraw(section, "lib_ldf_mode", None) is None:
    config.set(section, "lib_ldf_mode", "deep")
if config.getraw(section, "lib_archive", None) is not None and env.GetProjectOption("lib_archive"):
    print("Warning: nRF54 requires lib_archive = no to retain interrupt handlers; overriding lib_archive.")
config.set(section, "lib_archive", False)


def emit_uf2(target, source, env):
    command = [
        env.subst("$PYTHONEXE"),
        str(framework_dir / "tools" / "uf2" / "uf2_emit.py"),
        "--input", str(source[0]),
        "--output", str(target[0]),
        "--family", properties["build.uf2_family"],
        "--base-address", properties["build.uf2_base_address"],
        "--uf2conv", str(framework_dir / "tools" / "uf2" / "uf2conv.py"),
    ]
    return subprocess.call(command)


env.Append(BUILDERS={
    "ElfToHex": Builder(action=env.VerboseAction(
        '"$OBJCOPY" -O ihex "$SOURCE" "$TARGET"', "Building $TARGET"), suffix=".hex"),
    "ElfToBin": Builder(action=env.VerboseAction(
        '"$OBJCOPY" -O binary "$SOURCE" "$TARGET"', "Building $TARGET"), suffix=".bin"),
    "HexToUf2": Builder(action=env.VerboseAction(emit_uf2, "Building $TARGET"), suffix=".uf2"),
})

if "nobuild" in COMMAND_LINE_TARGETS:
    target_elf = env.File("$BUILD_DIR/${PROGNAME}.elf")
    target_hex = env.File("$BUILD_DIR/${PROGNAME}.hex")
    target_bin = env.File("$BUILD_DIR/${PROGNAME}.bin")
    target_uf2 = env.File("$BUILD_DIR/${PROGNAME}.uf2")
else:
    target_elf = env.BuildProgram()
    env.Depends(target_elf, env.File(env.subst("$LDSCRIPT_PATH")))
    target_hex = env.ElfToHex("$BUILD_DIR/${PROGNAME}", target_elf)
    target_bin = env.ElfToBin("$BUILD_DIR/${PROGNAME}", target_elf)
    target_uf2 = env.HexToUf2("$BUILD_DIR/${PROGNAME}", target_hex)
    env.Depends(target_hex, "checkprogsize")
    env.Depends(target_uf2, [
        env.File(str(framework_dir / "tools" / "uf2" / "uf2_emit.py")),
        env.File(str(framework_dir / "tools" / "uf2" / "uf2conv.py")),
    ])

AlwaysBuild(env.Alias("nobuild", target_hex))
target_build = env.Alias("buildprog", [target_hex, target_bin, target_uf2])
target_size = env.AddPlatformTarget(
    "size", target_elf, env.VerboseAction("$SIZEPRINTCMD", "Calculating size $SOURCE"),
    "Program Size", "Calculate program size")
env.AddPlatformTarget("uf2", target_uf2, None, "Build UF2", "Generate address-preserving UF2 image")

explicit_protocol = env.GetProjectOption("upload_protocol", None)
protocol = explicit_protocol or options.get("clean_upload", "auto")
if options.get("clean_upload") == "pyocd_vm" and not explicit_protocol:
    protocol = "pyocd_vm"
elif options.get("clean_upload") == "jlink" and not explicit_protocol:
    protocol = "jlink"
env.Replace(UPLOAD_PROTOCOL=protocol)


def upload_firmware(target, source, env):
    command = [
        env.subst("$PYTHONEXE"), str(platform_dir / "builder" / "upload.py"),
        "--framework-dir", str(framework_dir), "--action", "upload",
        "--protocol", protocol, "--target", properties["upload.target"],
        "--hex", env.subst("$BUILD_DIR/${PROGNAME}.hex"),
        "--uf2", env.subst("$BUILD_DIR/${PROGNAME}.uf2"),
        "--pyocd-safe", str(board.get("upload.pyocd_safe", properties.get("upload.pyocd_safe", "auto"))),
        "--uf2-drive", str(board.get("upload.uf2_drive", properties.get("upload.uf2_drive", "auto"))),
        "--uf2-labels", str(board.get("upload.uf2_labels", properties["upload.uf2_labels"])),
        "--uf2-timeout", str(board.get("upload.uf2_timeout", properties.get("upload.uf2_timeout", "12"))),
    ]
    port = env.subst("$UPLOAD_PORT")
    if port:
        command.extend(["--port", port])
    uid = board.get("upload.uid", properties.get("upload.uid", ""))
    if uid:
        command.extend(["--uid", str(uid)])
    flags = env.get("UPLOAD_FLAGS", [])
    if flags:
        command.extend(["--"] + [env.subst(flag) for flag in flags])
    return subprocess.call(command, env=dict(os.environ, PATH=str(env["ENV"]["PATH"])))


upload_action = "$UPLOADCMD" if protocol == "custom" else upload_firmware
env.AddPlatformTarget("upload", [target_hex, target_uf2],
                      env.VerboseAction(upload_action, "Uploading nRF54 firmware"), "Upload")
Default([target_build, target_size])
