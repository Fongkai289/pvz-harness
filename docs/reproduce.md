# Reproduce the project

This is a model-driven harness, not a pretrained PvZ policy. Coach changes strategy and player code; it does not update model weights. The public repository includes the Portable harness source patch, but no commercial game resources, credentials or model weights.

## 1. Source and Python environment

Use Python 3.11+ and [uv](https://docs.astral.sh/uv/). Clone into this layout:

```text
pvz-harness/
  app/       # this Git repository
  external/  # separately obtained engine and build dependencies
```

```powershell
git clone https://github.com/Fongkai289/pvz-harness.git pvz-harness/app
cd pvz-harness/app
uv sync --locked --extra client --extra native
Copy-Item local.example.json local.json
```

Edit `local.json`: SSH destination (user@host or alias), port, private-key path and absolute remote project directory. The real file is ignored. `PVZ_LOCAL_CONFIG` can select another private JSON file. The SSH key stays outside Git. Connection, deployment, live review and research use this shared configuration; optional per-worker `ssh` values override it. Do not upload `local.json` to a public repository.

Linux offline tests need only `uv sync --locked --python 3.11 --group dev` and `uv run --locked pytest -q -ra`. Windows, optional native engines, and private-artwork tests report explicit skips if their prerequisites are missing. The test count varies by platform and installed assets; a passing CI run does not demonstrate game performance.

## 2. Model services and Linux application

Use a dedicated project directory chosen by the operator; it need not match this machine's layout. Install Python/uv and tmux, copy the source, run `uv sync --locked`, and create `.env` from `.env.example` with a new `PVZ_TOKEN`.

- **Vision/Planner:** configure `PVZ_QWEN_URL` and `PVZ_QWEN_MODEL` for your own OpenAI-compatible **chat-completions** service. It must accept image messages and the JSON-schema/structured-output requests used in `server/scene.py` and `server/planner.py`. Merely having an OpenAI-style URL is not sufficient. Qwen is the current adapter name, not a requirement to use a specific private server.
- **Jev:** the default vLLM adapter requires the exact `AutoTrust-JEV-27B-v1.1` protocol, not any chat model. Obtain the [AutoTrust model package](https://huggingface.co/autotrust/AutoTrust-JEV-27B-v1.1) and its `vllm_readout.json`; set `PVZ_JEV_MODEL_DIR` to that package directory. Set `PVZ_ONEJEV_URL` to `/v1/completions` and `PVZ_ONEJEV_MODEL=AutoTrust-JEV-27B-v1.1`. The service must support `/tokenize`, the model chat template, constrained token IDs, `logprobs`, and `return_tokens_as_token_ids`. See `server/jev_vllm.py`. Legacy adapters remain explicit alternatives; errors do not silently switch models.
- **Coach is optional:** leave `PVZ_COACH_PROVIDER=qwen` for the configured model provider. Codex Coach is opt-in: it needs a signed-in Codex desktop installation on Windows, `PVZ_COACH_PROVIDER=codex` on Linux, and the local `scripts/codex_coach.ps1 -Mode Start` worker. It is not required to run the offline tests or live client.

Model services are managed independently. The deploy scripts manage only the harness, not shared inference servers. Before starting, generate the local image templates in the next section and copy those private files to the same relative locations on the server. For an existing deployment, `scripts/deploy.ps1 -Test` syncs code and local assets, tests, and restarts the harness. It excludes `local.json`, `.env`, data and local Python environments; configure the initial server `.env` yourself. The Python deployment helpers are for existing initialized deployments, not fresh model installation.

The harness listens on loopback `127.0.0.1:8765`; Windows reaches it via SSH. Use `scripts/connect_server.ps1 -Mode Probe` before starting the client. Do not expose it directly without supplying your own appropriate authentication/network setup.

## 3. Real game

The verified live memory-reader layout is **Windows Steam GOTY 1.2.0.1096**, with an **800×600 game client area**. Other releases are not interchangeable: the read-only offsets and image geometry must be validated before use. Game assets and an installation must be obtained separately. The `pvz_rl` legacy route requires a different complete 1.2.0.1065 game and is not the current Portable route.

Create your private menu/card/shovel templates following [assets.md](assets.md). Then run `scripts/start_client.cmd`. Start/pause/resume/end control the client; game menu actions remain under the operator's control. The dashboard is at `http://127.0.0.1:8766/`.

## 4. Portable engine and game-state training

```powershell
git clone https://github.com/wszqkzqk/PvZ-Portable.git ../external/PvZ-Portable
git -C ../external/PvZ-Portable checkout 98b659380014c89c3553fbb2839f5e5a84d6df3d
uv run python scripts/apply_portable_patch.py ../external/PvZ-Portable
```

The patch includes `Harness.cpp`, all five edits to upstream files, and build/benchmark helpers, including shovel (`dig`) and seed-selection (`prepare`/`choose`). `third_party/pvz-portable/manifest.json` records all nine resulting files. The engine patch is LGPL-3.0; see [third-party notices](../THIRD_PARTY_NOTICES.md).

Install [MSYS2](https://www.msys2.org/) at `external/msys64`, then install the UCRT64 GCC, CMake, Ninja, SDL2, SDL2_image, zlib, libpng, libjpeg-turbo, libopenmpt and pkgconf packages required by upstream's CMake configuration. Exact package names are listed in [portable-build.md](portable-build.md). From PowerShell:

```powershell
../external/PvZ-Portable/tools/build-harness.ps1
uv run python scripts/configure_training.py --resources 'E:/YourGame/Plants Vs Zombies'
```

The resource folder must contain your own `main.pak` and `properties/`. The generated `data/portable_rsi/config.json` points to the engine, runtime DLLs, resources and private worker directory; SSH settings come from `local.json`. No game or model starts during configuration.

Once both model services, the Linux harness and local assets are ready, use the dashboard's training controls or:

```powershell
uv run python -m rsi.portable_loop --name my-first-run --mode train
```

The Portable game clock freezes while models infer, so wall time and game time differ. Engine-only WAIT benchmarks are not model wins. Existing results (clean training wins on 1-2 through 1-7 and recorded live wins on 1-7/1-8) are limited observations, not a claim of full Adventure completion or broad RSI generalization.
