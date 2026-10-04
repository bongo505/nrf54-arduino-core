"""Build the embedded Arduino framework using its canonical board properties."""

from pathlib import Path

from SCons.Script import DefaultEnvironment


env = DefaultEnvironment()
board = env.BoardConfig()
properties = env["NRF54_BOARD_PROPERTIES"]
framework = Path(env["NRF54_FRAMEWORK_DIR"])
core = framework / "cores" / properties["build.core"]
variant = framework / "variants" / properties["build.variant"]
machine_flags = ["-mcpu=" + properties["build.mcu"], "-mthumb", "-mfloat-abi=soft"]
# PlatformIO de-duplicates flag tokens when applying its debug profile. Joined
# arguments keep both forced includes intact through that transformation.
forced_headers = ["-include" + str(core / name) for name in (
    "CoreVersionGenerated.h", "BuildTargetGuard.h")]

env.Append(
    CFLAGS=["-std=gnu11"],
    CCFLAGS=["-g", "-Os", "-ffunction-sections", "-fdata-sections", "-Wall"]
            + machine_flags + forced_headers,
    CXXFLAGS=["-std=gnu++17", "-fpermissive", "-fno-exceptions", "-fno-rtti",
              "-fno-threadsafe-statics", "-fno-use-cxa-atexit", "-fno-sized-deallocation"],
    ASPPFLAGS=["-x", "assembler-with-cpp"] + machine_flags + forced_headers,
    CPPDEFINES=[("ARDUINO", 10819), ("F_CPU", properties["build.f_cpu"]),
                "ARDUINO_" + properties["build.board"], "ARDUINO_ARCH_NRF54L15CLEAN"],
    CPPPATH=[str(core), str(variant)],
    LINKFLAGS=machine_flags + ["-Wl,--gc-sections", "--specs=nano.specs", "--specs=nosys.specs",
                              "-Wl,-Map," + env.subst("$BUILD_DIR/${PROGNAME}.map")],
    LIBSOURCE_DIRS=[str(framework / "libraries")],
)

ldscript = board.get("build.ldscript", None) or str(core / properties["build.ldscript"])
env.Replace(LDSCRIPT_PATH=ldscript)
sdc = (framework / "libraries" / "Nrf54L15-Clean-Implementation" / "third_party" /
       "nordic_sdc" / "lib" / properties["build.nordic_sdc_arch"])
env.Append(LIBS=[env.File(str(sdc / filename)) for filename in (
    "libsoftdevice_controller_multirole.a", "libmpsl_fem_common.a", "libmpsl.a")])
env.Append(LIBS=["m"])

# The weak initVariant in main.cpp cannot pull a standalone variant archive.
env.BuildSources("$BUILD_DIR/FrameworkArduinoVariant", str(variant))
env.Append(LIBS=[env.BuildLibrary("$BUILD_DIR/FrameworkArduino", str(core))])
