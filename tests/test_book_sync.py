"""Live play and Portable training run one strategy: promotions and human edits reach both stores."""
import pytest

from rsi import human_edit
from rsi.book_sync import STUDY, mirror, peer_root
from rsi.native_jobs import NativeJobs, STEPS
from rsi.native_player import SEED, NativeCodeStore
from rsi.portable_bridge import isolated_settings
from server.playbook import PlaybookStore
from tests.test_native_rsi import attach_candidate, report
from tests.test_server import settings


def promote(settings, name):
    jobs = NativeJobs(settings)
    job = jobs.enqueue(name)
    for index, (_, mode, side) in enumerate(STEPS):
        if index == 2:
            attach_candidate(jobs, job)
        job = jobs.accept(job, report(jobs, job, .18 if side == "candidate" else .1))
    assert job["promoted"]
    return jobs, job


def test_peer_of_live_is_the_study_store_and_back(tmp_path):
    assert peer_root(tmp_path / "data") == tmp_path / "data" / STUDY
    assert peer_root(tmp_path / "data" / STUDY) == tmp_path / "data"


def test_live_promotion_becomes_training_current_with_its_code(settings):
    study = isolated_settings(settings).data_dir  # Portable copies the live CURRENT once
    jobs, job = promote(settings, "live-promotion")
    assert job["mirrored"] and PlaybookStore(study).current() == job["candidate"]
    assert PlaybookStore(study).load() == jobs.store.load()
    assert any('"mirrored_promotion"' in line for line in (study / "ledger.jsonl").read_text().splitlines())


def test_a_diverged_peer_is_left_alone_and_the_reason_recorded(settings):
    study = isolated_settings(settings).data_dir
    store = PlaybookStore(study)
    other = store.load().model_copy(update={"version": store.next_version(), "notes": "training moved on"})
    store.write(other)
    store.set_current(other.version)
    _, job = promote(settings, "diverged")
    # Live numbers its candidate past training's newest version, so the names cannot clash;
    # training has moved on from the shared parent, so nothing there is overwritten.
    assert job["candidate"] != other.version
    assert not job["mirrored"] and "not the parent" in job["mirror_skipped"]
    assert PlaybookStore(study).current() == other.version  # nothing overwritten


def test_training_promotion_reaches_live_and_code_is_copied(settings):
    study = isolated_settings(settings).data_dir
    store = PlaybookStore(study)
    parent = store.current()
    code = NativeCodeStore(study).add(SEED)
    book = store.load().model_copy(update={"version": store.next_version(), "parent": parent, "player_code": code})
    store.write(book)
    store.set_current(book.version)
    assert mirror(study, peer_root(study), book.version, parent) is None
    live = PlaybookStore(settings.data_dir)
    assert live.current() == book.version and NativeCodeStore(settings.data_dir).source(code)
    assert mirror(study, peer_root(study), book.version, parent) is None  # already there: nothing to do


def test_mirror_refuses_a_same_named_different_version(settings, tmp_path):
    study = isolated_settings(settings).data_dir
    live, train = PlaybookStore(settings.data_dir), PlaybookStore(study)
    parent = live.current()
    version = live.next_version()
    live.write(live.load().model_copy(update={"version": version, "parent": parent, "notes": "live"}))
    train.write(train.load().model_copy(update={"version": version, "parent": parent, "notes": "training"}))
    assert "different" in mirror(settings.data_dir, study, version, parent)
    assert train.current() == parent
    assert "no playbook store" in mirror(settings.data_dir, tmp_path / "missing", version, parent)


def test_human_edit_turns_on_seed_code_and_fallback_for_both(settings, monkeypatch):
    study = isolated_settings(settings).data_dir
    parent = PlaybookStore(settings.data_dir).current()
    monkeypatch.setattr(human_edit, "load_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["human_edit", "--player-code", "seed", "--set", "fallback.allow_planting=true",
                                     "--both", "--note", "seed System0 and fallback for live and training"])
    human_edit.main()
    live, train = PlaybookStore(settings.data_dir), PlaybookStore(study)
    book = live.load()
    assert book.parent == parent and book.player_code == NativeCodeStore(settings.data_dir).add(SEED)
    assert book.fallback.allow_planting and train.load() == book
    assert NativeCodeStore(study).source(book.player_code)
    monkeypatch.setattr("sys.argv", ["human_edit", "--set", "fallback.allow_planting=false", "--both",
                                     "--no-promote", "--note", "x"])
    with pytest.raises(SystemExit):
        human_edit.main()


def test_a_peer_on_another_version_is_left_alone(settings):
    study = isolated_settings(settings).data_dir
    store = PlaybookStore(study)
    ahead = store.load().model_copy(update={"version": "v050", "notes": "training far ahead"})
    store.write(ahead)
    store.set_current("v050")
    _, job = promote(settings, "ahead")
    assert not job["mirrored"] and "not the parent" in job["mirror_skipped"] and store.current() == "v050"


def test_new_versions_never_reuse_a_name_from_the_other_store(settings):
    study = isolated_settings(settings).data_dir
    live, train = PlaybookStore(settings.data_dir), PlaybookStore(study)
    for _ in range(2):  # training writes Coach candidates the live store never sees
        train.write(train.load().model_copy(update={"version": train.next_version(), "notes": "candidate"}))
    newest = max(int(v[1:]) for v in train.versions())
    assert live.next_version() == f"v{newest + 1:03d}" and train.next_version() == live.next_version()
