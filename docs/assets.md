# Private game artwork

The current Git tree intentionally excludes game screenshots, `knowledge/seed_packets.npz`, and `knowledge/shovel_box.png`. Existing operators keep their local files; `.gitignore` prevents accidental re-addition. Game assets are not covered by the repository license. Use your own game and do not download arbitrary copies from this repository's history as a substitute for obtaining it.

## Menu references

Capture the **800×600 client area** from your own GOTY 1.2.0.1096 installation, without window borders, overlays or DPI scaling. Save the following screens in a private folder:

- `main_menu.png`: main menu, no modal dialog.
- `continue_game.jpg`: saved-game continue confirmation.
- `game_paused.jpg`: in-level pause panel.
- `game_over.jpg`: game-over screen.

Import them with `uv run python scripts/prepare_local_assets.py --screens 'C:/private/pvz-screens'`.
The tool validates dimensions and copies files into ignored `docs/screens/`. It does not operate the game. Reference-based recognition fails with an explicit missing-file error if they are absent.

## Shovel crop

Take an 800×600 gameplay frame with the shovel resting in its box. Determine the left edge of the box from the screenshot (supported positions: 456, 516, 532, 568, 609). For example:

```powershell
uv run python scripts/prepare_local_assets.py --shovel-frame 'C:/private/gameplay.png' --shovel-x 456
```

This crops the 70×72 box into ignored `knowledge/shovel_box.png`. Do not use a frame after the shovel has been picked up.

## Seed card templates

First build and configure the pinned Portable engine using [reproduce.md](reproduce.md). Then run:

```powershell
uv run python scripts/harvest_seed_packets.py
uv run python scripts/build_seed_templates.py
```

The harvesting command runs isolated local engine instances using your game resources, without model inference or control of your personal game. It stores labeled ready/dimmed/recharging/empty card crops in `data/seed_packets_raw/`; the second command builds ignored `knowledge/seed_packets.npz`. Copy this file and the shovel/menu references privately to the Linux app at the same relative paths before live play.

## Tests

The public Linux CI has no proprietary artwork. Tests tied to original labeled game frames are explicitly skipped when those files are absent, while synthetic-image, protocol, state, strategy and gate tests still run. Local users who retain the original private fixtures can run those checks as well. Arbitrary screenshots cannot reproduce the expected zombie positions in the old labeled fixtures; newly captured images need their own labels and assertions.
