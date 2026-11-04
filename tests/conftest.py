"""Artwork-dependent checks are optional; game assets are intentionally not distributed."""
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
NEEDS={
    'test_menu.py': ['docs/screens/main_menu.png','docs/screens/game_over.jpg',
                     'docs/screens/game_paused.jpg','docs/screens/continue_game.jpg','docs/screens/day_1-6_start.png'],
    'test_seed_packets.py': ['knowledge/seed_packets.npz'],
}
CHOOSER_STATE_ONLY={
    'test_choose_button_only_outside_a_connection_and_once_at_a_time',
    'test_training_asks_qwen_in_words_with_the_level_offer_and_preview',
    'test_bridge_picks_for_training_and_keeps_the_fixed_order_if_qwen_fails',
    'test_training_episode_chooses_seeds_before_the_level_starts',
    'test_portable_arena_speaks_the_harness_prepare_and_choose_commands',
}


def pytest_collection_modifyitems(items):
    for item in items:
        filename=item.path.name
        name=item.originalname or item.name
        needs=list(NEEDS.get(filename, []))
        if filename=='test_scene.py' and name=='test_measure_zombies_on_real_day_frames':
            needs += ['tests/fixtures/'+n for n in ('day_1-6_f0073.jpg','day_1-6_f0405.jpg',
                       'day_1-6_f1022.jpg','day_1-6_run2_f2574.jpg','day_1-6_paused.jpg')]
        if filename=='test_seed_choice.py' and name not in CHOOSER_STATE_ONLY:
            needs += ['tests/fixtures/seed_chooser.jpg','tests/fixtures/day_1-6_f0073.jpg',
                      'knowledge/seed_packets.npz','docs/screens/main_menu.png','docs/screens/continue_game.jpg']
        if filename=='test_shovel.py' and name=='test_shovel_box_is_found_by_code_wherever_the_seed_bank_puts_it':
            needs += ['knowledge/shovel_box.png']
        missing=[p for p in needs if not (ROOT/p).is_file()]
        if missing:
            item.add_marker(pytest.mark.skip(reason='Optional local game artwork missing; see docs/assets.md: '+', '.join(missing)))
