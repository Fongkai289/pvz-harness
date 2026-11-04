# Reproducible Portable harness

Upstream: https://github.com/wszqkzqk/PvZ-Portable.git

Pinned revision: `98b659380014c89c3553fbb2839f5e5a84d6df3d`.

`harness.patch` includes the five tracked source edits plus `src/Harness.cpp`, `tools/HARNESS.md`, `tools/build-harness.ps1` and `tools/harness_benchmark.py`. This includes the `dig`, `prepare` and `choose` commands used by shovel and seed-selection training. The manifest records the patch digest and the exact resulting source files. No game executable, artwork or commercial assets are included.

The patch and covered engine additions/modifications are distributed under the upstream LGPL-3.0 license, not the root MIT license. Preserve `LICENSE`, `COPYING`, and upstream third-party notices when building or redistributing. Original upstream attribution remains in the patch context and upstream files; harness changes were made by pvz-harness contributors in 2026.

From the app repository:

```powershell
git clone https://github.com/wszqkzqk/PvZ-Portable.git ../external/PvZ-Portable
git -C ../external/PvZ-Portable checkout 98b659380014c89c3553fbb2839f5e5a84d6df3d
python scripts/apply_portable_patch.py ../external/PvZ-Portable
```

The helper refuses an unexpected revision, checks the patch checksum and validates every modified file. It does not reset or overwrite unrelated upstream changes. See [reproduce.md](../../docs/reproduce.md) for building and supplying your game resources.
