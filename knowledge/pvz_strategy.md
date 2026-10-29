# Plants vs. Zombies strategy knowledge (seed, human-curated)

Summarised in our own words from community guides (sources at the end), 2026-10-28.
Only the block between the PLANNER markers is given to the planner; the rest is for the
coach and for people. Load it into a new playbook version with:

    uv run python -m rsi.human_edit --planner-knowledge knowledge/pvz_strategy.md --note "..."

<!-- PLANNER KNOWLEDGE START -->
Economy
- Sun is the only currency. Early in a level, buy Sunflowers (50 sun) before attackers, in the
  leftmost columns (0-1), one per row that zombies use; aim for about two columns of Sunflowers.
- At night (dark lawn with graves) Sun-shrooms (25) replace Sunflowers.
- Only place a plant you can afford and whose seed packet is not on cooldown (greyed out).
- Keep 150 sun in reserve for an emergency instant kill (Cherry Bomb) once the attackers are up.

Placement
- Attackers (Peashooter 100; later Repeater 200, Snow Pea 175) go in columns 2-4.
- A wall (Wall-nut 50, Tall-nut 125) goes in front of the attackers, around column 5-7.
- Every row zombies walk in needs at least one attacker; an undefended row reaches the house.
- Add damage to the row under the most pressure first; spread the rest evenly.
- Do not plant on a cell a zombie already occupies, except instant kills.

Threats
- A flag-carrying zombie means a large wave: make sure every row has damage and a wall.
- Conehead / Buckethead: armoured; stack more damage in that row or slow it with Snow Pea.
- Pole Vaulting: jumps over the first plant it meets; a Tall-nut stops the jump.
- Newspaper: runs fast once its newspaper is destroyed; keep extra damage in that row.
- Screen Door: its door blocks straight shots; use Fume-shroom, lobbed plants or Magnet-shroom.
- Football: fast and very tough; use an instant kill, Squash or Hypno-shroom.
- Clusters of zombies, or the final wave: Cherry Bomb (150) clears a 3x3 area.

Level types
- Night: mushrooms are cheap; graves spawn extra zombies in the final wave.
- Pool: 6 rows, the middle two are water; plants there need a Lily Pad (25) first.
- Fog: the right side is hidden; zombies appear late, so keep walls up early.
- Roof: every plant needs a Flower Pot (25); straight shooters are blocked by the slope,
  so use lobbed plants (Cabbage-pult, Kernel-pult, Melon-pult).
<!-- PLANNER KNOWLEDGE END -->

## Notes for the coach and people

**What each rule needs the player to see.** Code perception currently recognises only sun blobs.
With screenshots (Qwen vision) the planner can read most of this itself; in text-only mode it
cannot act on anything below except the general economy and placement rules.

| Rule group | Needs to be seen |
|---|---|
| Economy | sun count, seed packet cost and cooldown (greyed out) |
| Placement | which rows have zombies, which cells are free |
| Threats | zombie types (flag, cone, bucket, pole, newspaper, screen door, football) |
| Level types | level type (night, pool, fog, roof) |

**Known gaps.**
- Costs are the standard PC values; confirm them on the installed game version.
- The planner contract and `server/geometry.py` assume a 5-row lawn. Pool and fog levels have 6
  rows; geometry must be extended before those levels are played.
- Per-level specifics (e.g. how many Sunflowers before the first attacker) vary by level and are
  not encoded; the coach can add them once real games show where the defaults fail.

**Sources** (opened 2026-10-28)
- [PvZ Wiki: Adventure Mode strategy guide](https://plantsvszombies.wiki.gg/wiki/Adventure_Mode/Strategy_guide)
- [PvZ Wiki: Zombies (PvZ)](https://plantsvszombies.wiki.gg/wiki/Zombies_(PvZ))
- [TapTap: 新手教程](https://www.taptap.cn/moment/313449830204575613) (search summary only)
- [18183: 全关卡图文攻略](https://www.18183.com/gonglue/202208/4080495.html) (search summary only)
