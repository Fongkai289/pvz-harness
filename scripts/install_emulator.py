"""Build the pinned native emulator into this Python environment.

Linux: python scripts/install_emulator.py (requires a C++17 g++ and Python headers).
Windows: --download-toolchain uses a checksum-verified portable GCC under .cache.
Only .cache/pvz-emulator-source is patched; no remote machine is used.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"
SOURCE = CACHE / "pvz-emulator-source"
REVISION = "bb6347233fbbf9964fa6dd4a15f721262eda15ce"
TOOL_URL = "https://github.com/skeeto/w64devkit/releases/download/v2.11.0/w64devkit-x64-2.11.0.7z.exe"
TOOL_SHA256 = "54c2bb9962e8a30c8270e34c5efc3d5fee579f53087fa7d435b95fbba2af7b2b"


def run(args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def ensure_pybind11():
    """uv-created environments may already have pybind11 but intentionally no pip."""
    try:
        if version("pybind11") == "2.13.6":
            return
    except PackageNotFoundError:
        pass
    if shutil.which("uv"):
        run([shutil.which("uv"), "pip", "install", "--python", sys.executable, "pybind11==2.13.6"])
    else:
        import importlib.util
        if importlib.util.find_spec("pip") is None:
            run([sys.executable, "-m", "ensurepip"])
        run([sys.executable, "-m", "pip", "install", "pybind11==2.13.6"])


def patch():
    # Re-derive just these files from the pinned commit: repeated installs are idempotent.
    replacements = {
        "object/scene.h": [("scene(scene_type t) : type(t),\n        rng(std::random_device()()),",
                            "scene(scene_type t, unsigned int seed = std::random_device()()) : type(t),\n        rng(seed),")],
        "world.h": [("world(object::scene_type t):\n\t\tscene(t),",
                     "world(object::scene_type t, unsigned int seed = std::random_device()()):\n\t\tscene(t, seed),")],
        "system/sun.h": [("data.natural_sun_countdown = gen_nature_sun_countdown();",
                          "if (data.natural_sun_countdown == 0) data.natural_sun_countdown = gen_nature_sun_countdown();")],
        "pybind.cpp": [(".def(py::init<scene_type>())", ".def(py::init<scene_type>())\n        .def(py::init<scene_type, unsigned int>())"),
                       ('.def_readonly("dead", &decltype(plant::countdown)::dead)',
                        '.def_readonly("dead", &decltype(plant::countdown)::dead);')],
    }
    for relative, changes in replacements.items():
        original = subprocess.check_output(["git", "show", f"{REVISION}:{relative}"], cwd=SOURCE).decode("utf-8")
        for old, new in changes:
            if old not in original:
                raise RuntimeError(f"Patch anchor missing in {relative}")
            original = original.replace(old, new, 1)
        if relative == "pybind.cpp":
            original = original.replace('#include "world.h"', '#include "world.h"\n#include "system/util.h"')
            original = original.replace('PYBIND11_MODULE(pvzemu, m) {', '''PYBIND11_MODULE(pvzemu, m) {
    m.attr("training_patch") = "pvz-agent-v1";
    m.def("_configure_training", [](world& w, unsigned int initial_sun) {
        if (initial_sun > 9990) throw std::invalid_argument("sun out of bounds");
        w.scene.sun.sun = initial_sun;
        w.scene.stop_spawn = true;
    });
    m.def("_spawn_training", [](world& w, zombie_type type, unsigned int row, unsigned int seed) {
        if (row >= w.scene.rows || (type != zombie_type::zombie &&
            type != zombie_type::conehead && type != zombie_type::buckethead))
            throw std::invalid_argument("unsupported training spawn");
        // Each event has its own random stream, independent of candidate actions.
        auto saved_rng = w.scene.rng;
        w.scene.rng.seed(seed);
        auto& z = w.zombie_factory.create(type, 0);
        z.row = row;
        z.y = system::zombie_init_y(w.scene.type, z, row);
        z.int_y = static_cast<int>(z.y);
        w.scene.rng = saved_rng;
    });''')
        (SOURCE / relative).write_text(original, encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-toolchain", action="store_true")
    parser.add_argument("--compiler")
    args = parser.parse_args()
    CACHE.mkdir(exist_ok=True)
    if not SOURCE.exists():
        run(["git", "clone", "https://github.com/dnartz/PvZ-Emulator.git", SOURCE])
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()
    if revision != REVISION:
        raise RuntimeError(f"Expected upstream {REVISION}, got {revision}; use a fresh cache checkout")
    patch()
    ensure_pybind11()
    import pybind11
    compiler = args.compiler or shutil.which("g++")
    if compiler is None and os.name == "nt":
        compiler_path = CACHE / "toolchains" / "w64devkit" / "bin" / "g++.exe"
        if not compiler_path.exists() and args.download_toolchain:
            archive = CACHE / "w64devkit-x64-2.11.0.7z.exe"
            if not archive.exists():
                urllib.request.urlretrieve(TOOL_URL, archive)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != TOOL_SHA256:
                raise RuntimeError("Portable compiler checksum mismatch")
            dest = CACHE / "toolchains"
            dest.mkdir(exist_ok=True)
            run([archive, "-y", f"-o{dest}"], creationflags=subprocess.CREATE_NO_WINDOW)
        if compiler_path.exists():
            compiler = str(compiler_path)
    if compiler is None:
        raise RuntimeError("Install a C++17 g++, or on Windows pass --download-toolchain")
    sources = sorted(SOURCE.glob("object/*.cpp")) + sorted(SOURCE.glob("system/**/*.cpp"))
    sources += sorted(SOURCE.glob("learning/*.cpp")) + [SOURCE / "world.cpp", SOURCE / "pybind.cpp"]
    output = CACHE / ("pvzemu" + sysconfig.get_config_var("EXT_SUFFIX"))
    command = [compiler, "-std=c++17", "-O2", "-shared", "-DNDEBUG", "-include", "cstring",
               "-I" + str(SOURCE), "-I" + str(SOURCE / "lib"),
               "-I" + pybind11.get_include(), "-I" + sysconfig.get_path("include")]
    if os.name == "nt":
        command += ["-static-libgcc", "-static-libstdc++"]
    else:
        command += ["-fPIC", "-pthread"]
    command += sources
    if os.name == "nt":
        command += ["-L" + str(Path(sys.base_prefix) / "libs"), f"-lpython{sys.version_info.major}{sys.version_info.minor}"]
    command += ["-o", output]
    run(command, cwd=SOURCE)
    target = Path(sysconfig.get_path("platlib")) / output.name
    shutil.copy2(output, target)
    # GCC builds may depend on winpthread; keep native dependencies alongside the extension.
    if os.name == "nt":
        for dll in Path(compiler).parent.glob("libwinpthread-1.dll"):
            shutil.copy2(dll, target.parent / dll.name)
    run([sys.executable, "-c", "import pvzemu; assert pvzemu.training_patch == 'pvz-agent-v1'; print(pvzemu.__file__)"])
    print(f"Installed {REVISION}, training patch pvz-agent-v1")


if __name__ == "__main__":
    main()
