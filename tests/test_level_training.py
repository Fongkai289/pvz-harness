"""Per-level playbooks: each level trains its own, live play loads the level's, both stores agree."""
import asyncio
import json
from dataclasses import replace

import pytest

from common.protocol import EpisodeEvent, StateMsg, dumps
from rsi.book_sync import mirror_level
from rsi.native_engineer import NativeCoachAction, NativeEngineer
from rsi.native_jobs import LEVEL_SEED, NativeJobs
from rsi.native_player import NativeCodeStore, SEED
from rsi.portable_bridge import inherited_level, isolated_settings
from server.level_books import LevelBooks
from server.playbook import PlaybookStore
from tests.test_native_engineer import CHANGED
from tests.test_native_rsi import report
from tests.test_server import settings
from tests.test_system0_actions import live, session


def new_version(store, **update):
    book = store.load().model_copy(update={"version": store.next_version(), "parent": store.current(), **update})
    store.write(book)
    return book.version


def test_levels_without_their_own_playbook_play_current(settings):
    store, books = PlaybookStore(settings.data_dir), LevelBooks(settings.data_dir)
    assert books.version(store, 4) == store.current() and books.version(store, None) == store.current()
    version = new_version(store)
    books.set(store, 4, version, expected=store.current())
    assert books.version(store, 4) == version and books.version(store, 6) == store.current()
    with pytest.raises(ValueError, match="changed"):
        books.set(store, 4, store.current(), expected=store.current())  # 4 no longer plays CURRENT
    with pytest.raises(FileNotFoundError):
        books.set(store, 6, "v999")


def test_a_new_level_starts_from_the_latest_earlier_ordinary_level():
    books = {2: "v011", 4: "v013", 5: "v014"}
    assert inherited_level(books, 6) == 4  # 1-5 is a conveyor level: skipped
    assert inherited_level(books, 3) == 2 and inherited_level(books, 2) is None


def test_level_playbooks_are_mirrored_between_live_and_training(settings):
    study = isolated_settings(settings).data_dir
    train = PlaybookStore(study)
    parent = train.current()
    code = NativeCodeStore(study).add(SEED)
    version = new_version(train, player_code=code)
    LevelBooks(study).set(train, 3, version, expected=parent)
    assert mirror_level(study, settings.data_dir, 3, version, parent) is None
    live = PlaybookStore(settings.data_dir)
    assert LevelBooks(settings.data_dir).version(live, 3) == version and NativeCodeStore(settings.data_dir).source(code)
    assert live.current() == parent  # other levels keep playing CURRENT
    # Same version as live's CURRENT still gets an explicit entry: CURRENT may move on later.
    LevelBooks(study).set(train, 7, parent, expected=parent)
    assert mirror_level(study, settings.data_dir, 7, parent, parent) is None and LevelBooks(settings.data_dir).get(7) == parent
    other = new_version(train)
    assert "not the parent" in mirror_level(study, settings.data_dir, 3, other, "v999")


def evidence(jobs, level, score, mowers):
    return {"environment": {"backend": "pvz_rl"}, "mean": score, "episodes": [{
        "level": level, "seed": LEVEL_SEED + level, "outcome": "win", "score": score, "progress": 1.0,
        "elapsed_game_s": 200, "mower_times": [30.0] * mowers, "complete": True, "trace": []}]}


@pytest.mark.parametrize("outcome,score,kept,cleared", [("win", 1.0, True, True), ("lose", .1, False, False)])
def test_level_attempt_keeps_a_tested_change_only_if_it_is_better(settings, monkeypatch, outcome, score, kept, cleared):
    jobs = NativeJobs(replace(settings, native_evolution="code"))
    level = 4
    job = jobs.level_attempt("L4-attempt1", level, evidence(jobs, level, .5, 2))
    assert job["step"] == 2 and job["kind"] == "level" and job["parent"] == jobs.store.current()
    assert jobs.level_attempt("L4-attempt1", level, evidence(jobs, level, .5, 2))["id"] == job["id"]  # resumable
    actions = iter([NativeCoachAction(action="write_file", explanation="hold row", path="player/policy.py", content=CHANGED),
                    NativeCoachAction(action="test_candidate", explanation="replay 1-4"),
                    NativeCoachAction(action="finish", explanation="submit", diagnosis="row 2 broke", rationale="wall")])
    briefs = []
    async def model(role, messages, schema, *args, **kwargs):
        briefs.append(messages[1]["content"])
        return next(actions), {"role": role}
    monkeypatch.setattr("rsi.native_engineer.request_role", model)
    async def run():
        driver, current = NativeEngineer(jobs), job
        for _ in range(3):  # start, write_file, test_candidate
            current = await driver.advance(jobs.load(current["id"]), {"source": "fixture"})
        task = jobs.request(current)
        assert task["engineering_test"] == 1 and task["episodes"] == [{"seed": LEVEL_SEED + level, "level": level}]
        replay = report(jobs, current, score, outcome)
        replay["episodes"][0]["progress"] = min(1.0, replay["episodes"][0]["progress"])  # helper scales by score
        current = jobs.accept(current, replay)
        return await driver.advance(current, None)
    done = asyncio.run(run())
    assert "only for level 1-4" in briefs[0]
    assert done["status"] == ("promoted" if kept else "rejected") and done["cleared"] is cleared
    books = LevelBooks(settings.data_dir)
    assert books.get(level) == (done["candidate"] if kept else None)
    assert jobs.store.current() == job["parent"]  # CURRENT and other levels never move


def test_live_session_loads_the_levels_playbook_each_episode(live):
    sess = live()
    store, books = PlaybookStore(sess.base_settings.data_dir), LevelBooks(sess.base_settings.data_dir)
    level_version = new_version(store, player_code=None, timing=sess.playbook.timing.model_copy(update={"planner_seconds": 30}))
    books.set(store, 3, level_version, expected=store.current())
    connected = sess.playbook.version
    asyncio.run(sess.on_text(dumps(StateMsg(t=0, data={"game": "real_pvz", "level": 3}))))
    assert sess.playbook.version == level_version and sess.player_runtime is None
    assert sess.settings.plan_interval == 30
    assert ("level_playbook", {"level": 3, "version": level_version, "previous": connected, "switched": True}) in sess.events
    asyncio.run(sess.on_text(dumps(StateMsg(t=1, data={"game": "real_pvz", "level": 0}))))  # level still loading
    assert sess.playbook.version == level_version
    asyncio.run(sess.on_text(dumps(StateMsg(t=1, data={"game": "real_pvz", "level": 6}))))
    assert sess.playbook.version == store.current()  # 1-6 has no playbook of its own
    sess._book_level = None
    asyncio.run(sess.on_text(dumps(StateMsg(t=2, data={"game": "real_pvz", "level": 6}))))
    # Already on it: still logged, so a game's log always says which playbook it used.
    assert ("level_playbook", {"level": 6, "version": store.current(), "previous": store.current(), "switched": False}) in sess.events


def test_an_attempt_replays_the_seed_its_evidence_was_played_on(settings):
    jobs = NativeJobs(replace(settings, native_evolution="code"))
    game = evidence(jobs, 6, .3, 1)
    game["episodes"][0]["seed"] = 424242  # a single-level run's fresh seed
    job = jobs.level_attempt("L6-fresh", 6, game)
    assert job["suites"]["development"] == [{"seed": 424242, "level": 6}] and "seed 424242" in job["target"]["goal"]


def test_level_attempts_get_one_test_and_new_runs_close_old_leftovers(settings):
    from types import SimpleNamespace
    from rsi.native_engineer import LEVEL_TESTS
    from rsi.portable_bridge import PortableBridge, PortableJobs
    jobs = PortableJobs(isolated_settings(replace(settings, native_evolution="code")))
    old = jobs.level_attempt("train-old-L4-attempt1", 4, evidence(jobs, 4, .5, 2))
    gated = jobs.enqueue("train-old-rsi-1")
    finished = jobs.enqueue("train-old-L2-level-2", book_level=2)
    finished.update(kind="campaign", status="campaign_complete")
    jobs.save(finished)
    mine = jobs.level_attempt("train-new-L4-attempt1", 4, evidence(jobs, 4, .5, 2))
    closed = PortableBridge.close_stale(SimpleNamespace(jobs=jobs), "train-new")["closed"]
    assert sorted(closed) == sorted([old["id"], gated["id"]])
    assert jobs.load(old["id"])["status"] == "stopped" and jobs.load(mine["id"])["status"] == "coaching"
    assert jobs.load(finished["id"])["status"] == "campaign_complete"  # finished game records stay
    assert NativeEngineer(jobs).start(jobs.load(mine["id"]), None)["engineering"]["max_tests"] == LEVEL_TESTS == 1


def test_parallel_variants_keep_only_the_best_and_spot_duplicates(settings):
    jobs = NativeJobs(replace(settings, native_evolution="code"))
    store, books = jobs.store, LevelBooks(settings.data_dir)
    game = evidence(jobs, 4, .5, 2)
    a = jobs.level_attempt("g-a", 4, game, group="g", angle="a")
    b = jobs.level_attempt("g-b", 4, game, group="g", angle="b")
    assert "Suggested starting point" in a["target"]["goal"] and "player code's tactics" in a["target"]["goal"]
    assert "playbook text" in b["target"]["goal"]
    with pytest.raises(ValueError):
        jobs.level_attempt("g-z", 4, game, group="g", angle="z")
    for job, (outcome, score, mowers) in ((a, ("win", .8, 1)), (b, ("win", 1.0, 0))):
        job["candidate"] = new_version(store, notes=job["id"])
        replay = {"episodes": [{"outcome": outcome, "score": score, "mower_times": [20.0] * mowers}]}
        settled = jobs.settle_level(job, replay)
        assert settled["status"] == "tested" and books.get(4) is None  # nothing kept until all variants are in
    best, after = jobs.keep_best(["g-a", "g-b"])
    assert best == "g-b" and books.get(4) == jobs.load("g-b")["candidate"]
    assert [(j["id"], j["status"]) for j in after] == [("g-a", "rejected"), ("g-b", "promoted")]
    assert jobs.load("g-a")["reason"] == "another variant did better" and jobs.load("g-b")["cleared"]
    # Identical changes: the second variant is told it duplicates the first instead of replaying the game.
    c = jobs.level_attempt("h-a", 5, evidence(jobs, 5, .4, 1), group="h", angle="a")
    d = jobs.level_attempt("h-b", 5, evidence(jobs, 5, .4, 1), group="h", angle="b")
    c["engineering"] = {"tested_version": "same-identity"}
    jobs.save(c)
    assert NativeEngineer(jobs).duplicate_of(d, "same-identity") == "h-a"
    assert NativeEngineer(jobs).duplicate_of(d, "other-identity") is None


def test_no_better_variant_keeps_the_old_playbook(settings):
    jobs = NativeJobs(replace(settings, native_evolution="code"))
    game = evidence(jobs, 6, .7, 1)
    for name in ("k-a", "k-b"):
        job = jobs.level_attempt(name, 6, game, group="k", angle=name[-1])
        job["candidate"] = new_version(jobs.store, notes=name)
        jobs.settle_level(job, {"episodes": [{"outcome": "lose", "score": .1, "mower_times": [5.0]}]})
    best, after = jobs.keep_best(["k-a", "k-b"])
    assert best is None and LevelBooks(settings.data_dir).get(6) is None
    assert all(j["status"] == "rejected" and "not better" in j["reason"] for j in after)


def test_an_unfinished_round_on_the_current_playbook_is_offered_for_resuming(settings):
    from types import SimpleNamespace
    from rsi.portable_bridge import PortableBridge, PortableJobs
    jobs = PortableJobs(isolated_settings(replace(settings, native_evolution="code")))
    game = evidence(jobs, 3, .5, 2)
    old = [jobs.level_attempt(f"r1-{x}", 3, game, group="r1", angle=x, evidence_job="game-1") for x in "ab"]
    new = [jobs.level_attempt(f"r2-{x}", 3, game, group="r2", angle=x, evidence_job="game-1") for x in "ab"]
    new[0].update(status="tested", better=False, result={"outcome": "lose", "score": .1, "mowers": 1})
    jobs.save(new[0])  # one variant finished its test, the other was cut off mid-way
    assert PortableBridge.close_stale(SimpleNamespace(jobs=jobs), "train-new")["closed"] == []  # rounds wait for resuming
    round_ = jobs.resumable_round(3)
    assert round_ == {"group": "r2", "variants": {"a": "r2-a", "b": "r2-b"}, "evidence_job": "game-1",
                      "reason": "mowers", "mowers": 2}
    assert all(jobs.load(j["id"])["status"] == "stopped" for j in old)  # an older open round is closed
    assert jobs.load("r2-a")["status"] == "tested" and jobs.load("r2-b")["status"] == "coaching"
    # Once the level's playbook moves on (or the server code changes), the round is outdated.
    store = jobs.store
    LevelBooks(jobs.settings.data_dir).set(store, 3, new_version(store), expected=store.current())
    assert jobs.resumable_round(3) is None and jobs.load("r2-b")["status"] == "stopped"
