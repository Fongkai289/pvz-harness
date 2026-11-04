# Third-party components

The root MIT license covers original pvz-harness code, not the following third-party works or game assets.

| Component | Source and revision | License / scope |
|---|---|---|
| PvZ-Portable | [upstream](https://github.com/wszqkzqk/PvZ-Portable), `98b659380014c89c3553fbb2839f5e5a84d6df3d` | LGPL-3.0; the engine modifications distributed in `third_party/pvz-portable/harness.patch` retain LGPL-3.0. Both LGPL and incorporated GPL texts are included there. Upstream dependencies retain their own notices. |
| PvZ-Emulator | [upstream](https://github.com/dnartz/PvZ-Emulator), `bb6347233fbbf9964fa6dd4a15f721262eda15ce` | BSD-2-Clause, copyright 2020 testla; license copied to `third_party/licenses/PvZ-Emulator.txt`. Optional legacy tests only; not the current training backend. |
| AutoTrust-JEV-27B-v1.1 protocol | [model package](https://huggingface.co/autotrust/AutoTrust-JEV-27B-v1.1), `jev_format.py`, `jev_vllm_engine.py`, `vllm_readout.json` | Apache-2.0; `server/jev_vllm.py` implements the model-shipped wire contract and retains source attribution. License in `third_party/licenses/Apache-2.0.txt`. No weights/tokenizer/readout package is redistributed. |
| pvz_rl | [upstream](https://github.com/yych00/pvz_rl) | Optional separately obtained interface. Consult its own license before redistribution; its source and DLL are not vendored here. |

Plants vs. Zombies names, artwork, screenshots, card crops, and game resources belong to their respective owners, including EA/PopCap. They are not licensed under this repository's MIT license. The current source tree does not distribute game images, templates, executables, or `main.pak`. Use your own legally obtained game and generate local templates as described in [assets.md](docs/assets.md).

The Portable patch adds the private stdin/stdout harness, state/render/reset/step support, deterministic lifecycle handling, shovel and seed-selection commands, and build/benchmark helpers. The nine modified/added files and normalized SHA-256 digests are recorded in its manifest. It does not include proprietary game resources.
