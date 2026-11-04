# Portable Windows build prerequisites

Use MSYS2 UCRT64 at the workspace's `external/msys64`. From its UCRT64 shell, after following MSYS2's normal installation/update instructions:

```sh
pacman -S --needed \
  mingw-w64-ucrt-x86_64-gcc \
  mingw-w64-ucrt-x86_64-cmake \
  mingw-w64-ucrt-x86_64-ninja \
  mingw-w64-ucrt-x86_64-pkgconf \
  mingw-w64-ucrt-x86_64-SDL2 \
  mingw-w64-ucrt-x86_64-SDL2_image \
  mingw-w64-ucrt-x86_64-zlib \
  mingw-w64-ucrt-x86_64-libpng \
  mingw-w64-ucrt-x86_64-libjpeg-turbo \
  mingw-w64-ucrt-x86_64-libopenmpt
```

The engine vendors its SDL-Mixer-X implementation. Optional audio codecs reported as disabled by CMake are not required by the headless harness. The patched `tools/build-harness.ps1` sets the UCRT64 DLL path only for its child processes. It builds `build-harness/pvz-portable.exe`; the worker sets the same DLL path for each private engine process.

The code, resources and resulting binary have independent fingerprints. A successful configure/build proves the engine can be built; it does not establish any model win rate. Do not distribute proprietary assets with a build.
