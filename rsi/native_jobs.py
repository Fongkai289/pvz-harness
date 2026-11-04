"""Durable pvz_rl jobs. Windows runs games; Linux owns models and promotion.

The coach changes player code and the editable playbook sections (strategy notes, timing,
Jev mode, reflex settings); evaluation never changes. State development/confirmation is followed
by a pixel audit before touching the live CURRENT. No Emulator fallback.
"""
import hashlib
import json
from pathlib import Path
import re
import secrets
import time

from rsi.code_store import write_json
from common.file_lock import file_lock
from rsi.native_gate import COUNTS, TARGETED_COUNTS, compare
from server.level_books import LevelBooks
from server.playbook import PlaybookStore

TERMINAL = {"promoted", "rejected", "failed", "stale", "stopped", "tested"}  # tested: a variant awaiting the keep choice
STEPS = [("development", "state", "base"), ("development", "pixels", "base"),
         ("development", "state", "candidate"), ("confirmation", "state", "base"),
         ("confirmation", "state", "candidate"), ("audit", "pixels", "base"),
         ("audit", "pixels", "candidate")]
UPSTREAM = "d6ec92cba925218b655de90c56e850089ae349dc"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def source_digest():
    root = Path(__file__).resolve().parents[1]
    paths = sorted(p for folder in ("rsi", "rules", "server", "common") for p in (root / folder).glob("*.py"))
    return digest({p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in paths})


CONVEYOR_LEVELS = (5, 10)  # cards arrive on a belt at no cost; no sun economy
LEVEL_SEED = 71000  # level L's training game always uses seed LEVEL_SEED + L


# Suggested starting points for parallel attempts on one level; each Coach may still change anything.
ANGLES = {
    "a": "Start from the player code's tactics (decide/options/act/fallback) around the moments that went wrong.",
    "b": "Start from the playbook text: Planner knowledge, conditional lessons and the Planner/Jev settings.",
    "c": "Start from the seconds around the first lawn mower or lost plant (find_moments, query_episode), then fix"
         " whatever caused it.",
}


def clean_win(episode):
    return episode.get("outcome") == "win" and not episode.get("mower_times")


def level_goal(level, seed):
    goal = (f"Per-level training: this playbook is used only for level 1-{level} (live play loads it for that level"
            " too). Make 1-" + str(level) + " winnable without any lawn mower. Your test replays the same game"
            f" (seed {seed}); a tested change that wins mower-free, or scores higher than the current"
            " playbook, replaces it. Earlier attempts on this level are in evidence/history.json.")
    if level in CONVEYOR_LEVELS:
        goal += (" 1-" + str(level) + " is a conveyor-belt level: cards arrive on a belt at zero cost and there is"
                 " no sun economy, so its tactics differ from ordinary day levels.")
    return goal


def check_target(target):
    """{level, reason, mowers, regression_levels} from the training loop; what a targeted round fixes."""
    if not isinstance(target, dict):
        raise ValueError("target must be an object")
    level, regression = target.get("level"), target.get("regression_levels") or []
    if type(level) is not int or not 1 <= level <= 10:
        raise ValueError("target level must be 1..10")
    if not isinstance(regression, list) or any(type(l) is not int or not 1 <= l <= 10 or l == level for l in regression):
        raise ValueError("regression levels must be other levels 1..10")
    reason = target.get("reason")
    if reason not in ("lose", "mowers"):
        raise ValueError("target reason must be lose or mowers")
    mowers = target.get("mowers")
    if mowers is not None and (type(mowers) is not int or not 0 <= mowers <= 6):
        raise ValueError("invalid mower count")
    goal = (f"Training target: level 1-{level} was " + ("lost" if reason == "lose" else f"won but used {mowers} lawn mower(s)")
            + ". Development and confirmation games are all on this level (new seeds each stage); the pixel audit "
            "replays levels already won without mowers, which must not get worse. Make 1-" + str(level)
            + " winnable without any lawn mower.")
    if level in CONVEYOR_LEVELS:
        goal += (" 1-" + str(level) + " is a conveyor-belt level: cards arrive on a belt at zero cost and there is"
                 " no sun economy, so its tactics differ from ordinary day levels.")
    return {"level": level, "reason": reason, "mowers": mowers, "regression_levels": sorted(set(regression)),
            "goal": goal}


class NativeJobs:
    backend = 'pvz_rl'
    upstream = UPSTREAM
    identity_fields = ('game_sha256','dll_sha256','worker_source')
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.data_dir / "native_rsi"
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = PlaybookStore(settings.data_dir)
        from rsi.native_player import NativeCodeStore
        self.code = NativeCodeStore(settings.data_dir)

    def engine_digest(self):
        fields = ("planner", "qwen_url", "qwen_model", "onejev_url", "onejev_model", "decider",
                  "plan_interval", "planner_timeout", "decider_timeout", "native_evolution")
        return digest({"source": source_digest(), "settings": {k:getattr(self.settings,k) for k in fields}})

    def folder(self, job_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
            raise ValueError("invalid job id")
        return self.root / job_id

    def load(self, job_id):
        return json.loads((self.folder(job_id) / "job.json").read_text(encoding="utf-8"))

    def save(self, job):
        folder = self.folder(job["id"])
        folder.mkdir(parents=True, exist_ok=True)
        job["updated"] = time.time()
        write_json(folder / "job.json", job)
        write_json(self.root / "LATEST.json", {"id": job["id"]})

    def current_for(self, job):
        """The version the job's playbook slot plays now: its level's playbook, or CURRENT."""
        if job.get("book_level") is not None:
            return LevelBooks(self.settings.data_dir).version(self.store, job["book_level"])
        return self.store.current()

    def enqueue(self, session, *, retry=False, target=None, book_level=None):
        with file_lock(self.root / "QUEUE.lock"):
            return self._enqueue(session, retry=retry, target=target, book_level=book_level)

    def level_attempt(self, session, level, evidence, group=None, angle=None, evidence_job=None):
        """One per-level attempt: the Coach edits level `level`'s playbook from `evidence` (a report of
        that playbook on the level's game); its tested change is kept if it wins mower-free or scores higher.
        With group, it is one of several parallel variants: keep_best() then keeps at most one of them."""
        with file_lock(self.root / "QUEUE.lock"):
            if (self.folder(session) / "job.json").exists():
                return self.load(session)
            if type(level) is not int or not 1 <= level <= 10:
                raise ValueError("level must be 1..10")
            parent = self.store.load(LevelBooks(self.settings.data_dir).version(self.store, level))
            seed = evidence["episodes"][0]["seed"]  # the test replays exactly the game the evidence played
            if type(seed) is not int or not 0 < seed < 2**30:
                raise ValueError("evidence has no valid seed")
            if angle is not None and angle not in ANGLES:
                raise ValueError(f"angle must be one of {sorted(ANGLES)}")
            job = dict(id=session, source_session=session, kind="level", book_level=level, status="coaching", step=2,
                       parent=parent.version, parent_digest=digest(parent.model_dump()), candidate=None,
                       candidate_digest=None, gate="level-v1", verdicts=[], upstream=self.upstream,
                       backend=self.backend, promoted=False, created=time.time(),
                       suites={"development": [{"seed": seed, "level": level}], "confirmation": [],
                               "audit": []},
                       scope=f"{self.backend} level 1-{level} playbook; one replay of its game per Coach test",
                       target={"level": level, "goal": level_goal(level, seed)
                               + ((" Other Coaches work on the same game in parallel from other starting points; the"
                                   " best tested result is kept. Suggested starting point (not a limit): " + ANGLES[angle])
                                  if angle else "")},
                       **({"group": group, "angle": angle} if group else {}),
                       **({"evidence_job": evidence_job} if evidence_job else {}),
                       baseline={k: evidence["episodes"][0].get(k)
                                 for k in ("level", "seed", "outcome", "score", "progress", "mower_times")})
            job["engine_digest"] = self.engine_digest()
            job["evolution"] = "code"
            self.folder(session).mkdir(parents=True, exist_ok=True)
            write_json(self.folder(session) / "evidence.json", evidence)
            self.save(job)
            return job

    def settle_level(self, job, report):
        """After the Coach submits: keep the tested candidate for this level if it is better.
        A parallel variant only records its result; keep_best() decides for the whole group."""
        episode = report["episodes"][0]
        base = job["baseline"]
        better = clean_win(episode) or episode["score"] > base["score"] + 1e-9
        result = {k: episode.get(k) for k in ("outcome", "score")} | {"mowers": len(episode.get("mower_times") or [])}
        if job.get("group"):
            job.update(status="tested", better=better, cleared=clean_win(episode), result=result,
                       reason="tested; waiting for the other variants")
            self.save(job)
            return job
        if better:
            LevelBooks(self.settings.data_dir).set(self.store, job["book_level"], job["candidate"], expected=job["parent"])
            self.store.log(kind="level_book", job=job["id"], level=job["book_level"], parent=job["parent"],
                           version=job["candidate"], result=result)
            from rsi.book_sync import mirror_level, peer_root
            try:
                skipped = mirror_level(self.settings.data_dir, peer_root(self.settings.data_dir), job["book_level"],
                                       job["candidate"], job["parent"])
            except Exception as exc:
                skipped = f"{type(exc).__name__}: {exc}"[:300]
            job.update(mirrored=skipped is None, **({"mirror_skipped": skipped} if skipped else {}))
        job.update(status="promoted" if better else "rejected", promoted=better, cleared=clean_win(episode),
                   result=result, reason=("wins 1-{0} without a lawn mower" if clean_win(episode) else
                                          "scores higher on 1-{0}; kept for the next attempt" if better else
                                          "not better than the current 1-{0} playbook").format(job["book_level"]))
        self.save(job)
        return job

    def resumable_round(self, level):
        """The latest unfinished parallel round on `level` that still works on the level's current playbook.
        Older or outdated open rounds on that level are closed; the training loop finishes this one."""
        current = LevelBooks(self.settings.data_dir).version(self.store, level)
        groups = {}
        for path in sorted(self.root.glob("*/job.json")):
            job = self.load(path.parent.name)
            if job.get("kind") == "level" and job.get("book_level") == level and job.get("group"):
                groups.setdefault(job["group"], []).append(job)
        def open_round(jobs):
            return any(j["status"] not in TERMINAL for j in jobs) or (
                all(j["status"] in TERMINAL for j in jobs) and any(j["status"] == "tested" for j in jobs))
        candidates = sorted((g for g, jobs in groups.items() if open_round(jobs)),
                            key=lambda g: max(j.get("created", 0) for j in groups[g]))
        engine = self.engine_digest()  # jobs refuse to continue after a code/model change (verify_versions)
        keep = next((g for g in reversed(candidates)
                     if all(j["parent"] == current and j.get("evidence_job") and j.get("engine_digest") == engine
                            for j in groups[g])), None)
        for group in candidates:
            if group == keep:
                continue
            for job in groups[group]:
                if job["status"] not in TERMINAL or job["status"] == "tested":
                    job.update(status="stopped", reason="unfinished round replaced: the level's playbook changed or a newer round exists")
                    self.save(job)
        if keep is None:
            return None
        jobs = sorted(groups[keep], key=lambda j: j.get("angle") or "")
        baseline = jobs[0]["baseline"]
        return {"group": keep, "variants": {j["angle"]: j["id"] for j in jobs}, "evidence_job": jobs[0]["evidence_job"],
                "reason": "lose" if baseline.get("outcome") != "win" else "mowers",
                "mowers": len(baseline.get("mower_times") or [])}

    def keep_best(self, job_ids):
        """Of parallel variants on one level, keep the best tested change: a mower-free win first (highest
        score), else the highest score above the old playbook. Every other variant is closed as not kept."""
        jobs = [self.load(j) for j in job_ids]
        if len({(j.get("group"), j.get("book_level"), j.get("parent")) for j in jobs}) != 1 or not jobs[0].get("group"):
            raise ValueError("variants must share one group, level and parent")
        tested = [j for j in jobs if j["status"] == "tested" and j.get("better")]
        best = max(tested, key=lambda j: (j.get("cleared", False), j["result"]["score"]), default=None)
        if best is not None:
            LevelBooks(self.settings.data_dir).set(self.store, best["book_level"], best["candidate"], expected=best["parent"])
            self.store.log(kind="level_book", job=best["id"], level=best["book_level"], parent=best["parent"],
                           version=best["candidate"], result=best["result"], variants=job_ids)
            from rsi.book_sync import mirror_level, peer_root
            try:
                skipped = mirror_level(self.settings.data_dir, peer_root(self.settings.data_dir), best["book_level"],
                                       best["candidate"], best["parent"])
            except Exception as exc:
                skipped = f"{type(exc).__name__}: {exc}"[:300]
            best.update(status="promoted", promoted=True, mirrored=skipped is None,
                        reason=("wins 1-{0} without a lawn mower" if best["cleared"] else
                                "best variant, scores higher on 1-{0}; kept for the next attempt").format(best["book_level"]),
                        **({"mirror_skipped": skipped} if skipped else {}))
            self.save(best)
        for job in jobs:
            if best is not None and job["id"] == best["id"]:
                continue
            if job["status"] == "tested":
                job.update(status="rejected", promoted=False,
                           reason="another variant did better" if job.get("better") else
                                  "not better than the current 1-{0} playbook".format(job["book_level"]))
                self.save(job)
        return [best["id"] if best else None, [self.load(j) for j in job_ids]]

    def _enqueue(self, session, *, retry=False, target=None, book_level=None):
        folder = self.folder(session)
        job_id = session
        if (folder / "job.json").exists():
            previous = self.load(session)
            if not retry:
                return previous
            if previous.get("engine_digest") != self.engine_digest():
                previous.update(status="stale",reason="explicit retry after code/configuration change")
                self.save(previous)
            if previous["status"] not in TERMINAL:
                raise ValueError("stop the existing active job before requesting a fresh retry")
            job_id = session + "-retry-" + secrets.token_hex(3)
            for path in self.root.glob(session+"-retry-*/job.json"):
                active = self.load(path.parent.name)
                if active["status"] not in TERMINAL:
                    return active  # interrupted enqueue/repeated button must not duplicate a retry
        parent = self.store.load(LevelBooks(self.settings.data_dir).version(self.store, book_level))
        rng = secrets.SystemRandom()
        used = set()
        def fresh(level):
            while True:
                seed = rng.randrange(1, 2**30)
                if seed not in used:
                    used.add(seed)
                    return {"seed": seed, "level": level}
        if target is None:
            # Initially cover supported ordinary day levels, not all Adventure modes.
            suites = {stage: [fresh((2, 3, 4, 6)[i % 4]) for i in range(count)] for stage, count in COUNTS.items()}
            gate, scope = "paired-v1", " ordinary day levels 1-2,1-3,1-4,1-6; state dev/confirmation + pixel audit"
        else:
            target = check_target(target)
            regression = target["regression_levels"] or [target["level"]]
            suites = {"development": [fresh(target["level"]) for _ in range(TARGETED_COUNTS["development"])],
                      "confirmation": [fresh(target["level"]) for _ in range(TARGETED_COUNTS["confirmation"])],
                      "audit": [fresh(regression[i % len(regression)]) for i in range(TARGETED_COUNTS["audit"])]}
            gate = "targeted-v1"
            scope = (f" targeted at 1-{target['level']}: state dev/confirmation on it, pixel audit on levels "
                     + ",".join(f"1-{l}" for l in regression))
        job = dict(id=job_id, source_session=session, status="waiting_runtime", step=0,
                   parent=parent.version, parent_digest=digest(parent.model_dump()), candidate=None,
                   candidate_digest=None, suites=suites, verdicts=[], upstream=self.upstream,backend=self.backend,
                   scope=self.backend+scope, gate=gate, promoted=False, created=time.time())
        if target is not None:
            job["target"] = target
        if book_level is not None:
            job["book_level"] = book_level
        job["engine_digest"] = self.engine_digest()
        if self.settings.native_evolution not in {"code", "strategy"}:
            raise ValueError("PVZ_NATIVE_EVOLUTION must be code or strategy")
        job["evolution"] = self.settings.native_evolution
        self.save(job)
        return job

    def latest(self):
        path = self.root / "LATEST.json"
        if not path.exists():
            return {"status": "not_triggered", "backend": "pvz_rl", "emulator": False}
        job = self.load(json.loads(path.read_text())["id"])
        # Do not expose held-out seed lists or huge reports in the operator view.
        return {k: v for k, v in job.items() if k not in {"suites", "parent_digest", "candidate_digest", "engineering"}}

    def next_job(self):
        for path in sorted(self.root.glob("*/job.json")):
            job = self.load(path.parent.name)
            if job["status"] not in TERMINAL and job.get("candidate") == self.store.current():
                # Recover a crash after atomic CURRENT replacement but before job checkpoint.
                verdicts = []
                for stage in COUNTS:
                    indices = [i for i,s in enumerate(STEPS) if s[0] == stage and s[1] == ("pixels" if stage == "audit" else "state")]
                    if not all(self.report_path(job,i).exists() for i in indices):
                        break
                    reports = [json.loads(self.report_path(job,i).read_text()) for i in indices]
                    verdicts.append(compare(*reports, stage, job.get("gate", "paired-v1")))
                if (len(verdicts) == 3 and all(v["passed"] for v in verdicts)
                        and digest(self.store.load(job["candidate"]).model_dump()) == job["candidate_digest"]):
                    job.update(status="promoted", promoted=True, verdicts=verdicts, reason="recovered completed promotion")
                    self.save(job)
            if job["status"] not in TERMINAL:
                try:
                    self.verify_versions(job)
                except ValueError as exc:
                    job.update(status="stale", reason=str(exc))
                    self.save(job)
                    continue
                return job
        return None

    def verify_versions(self, job):
        if job.get("engine_digest") != self.engine_digest():
            raise ValueError("evaluation code/model configuration changed; start fresh evidence")
        if self.current_for(job) != job["parent"]:
            raise ValueError("CURRENT changed; stale candidate cannot replace it")
        for role in ("parent", "candidate"):
            if job.get(role) and digest(self.store.load(job[role]).model_dump()) != job[role + "_digest"]:
                raise ValueError("immutable playbook changed")
            if job.get(role):
                self.code.source(self.store.load(job[role]).player_code)
        if job.get("evolution") == "code" and job.get("candidate"):
            from rsi.native_engineer import book_identity
            candidate = self.store.load(job["candidate"])
            engineering = job.get("engineering") or {}
            if (not candidate.player_code or engineering.get("pending")
                    or engineering.get("tested_version") not in {book_identity(candidate), candidate.player_code}):
                raise ValueError("code candidate must be the exact completed coach test")

    def report_path(self, job, step):
        return self.folder(job["id"]) / ("-".join(STEPS[step]) + ".json")

    def request(self, job):
        self.verify_versions(job)
        stage, mode, side = STEPS[job["step"]]
        pending = (job.get("engineering") or {}).get("pending")
        version = job["parent"] if side == "base" or pending else job["candidate"]
        if version is None:
            raise ValueError("candidate has not been generated")
        book = self.store.load(version).model_dump()
        if pending:
            book.update(pending.get("sections", {}))
            book["player_code"] = pending["code"]
            self.code.source(pending["code"])
        request = dict(job_id=job["id"], step=job["step"], stage=stage, mode=mode, side=side,
                    version=version, book=book,
                    episodes=job["suites"][stage], upstream=job["upstream"], engine_digest=job["engine_digest"])
        if pending:
            request.update(engineering_test=pending["test"], side="engineer")
        return request

    def validate_report(self, job, report, request):
        if report.get("request_digest") != digest(request):
            raise ValueError("report belongs to a different request")
        if report.get("version") != request["version"]:
            raise ValueError("report version mismatch")
        expected = [(e["level"], e["seed"]) for e in request["episodes"]]
        actual = [(e.get("level"), e.get("seed")) for e in report.get("episodes", [])]
        if expected != actual:
            raise ValueError("missing or mismatched episodes")
        env = report.get("environment", {})
        if env.get("upstream") != self.upstream or env.get("mode") != request["mode"] or env.get("backend") != self.backend:
            raise ValueError("wrong evaluation backend")
        if env.get("fixture") or env.get("engine_digest") != job["engine_digest"]:
            raise ValueError("fixture or mismatched engine cannot qualify")
        for field in self.identity_fields:
            if not re.fullmatch(r"[0-9a-f]{64}", str(env.get(field, ""))):
                raise ValueError("missing runtime identity")
        identity = {k:v for k,v in env.items() if k != "mode"}
        if job.get("runtime_identity") and job["runtime_identity"] != identity:
            raise ValueError("runtime changed between evaluation stages")
        # Reject incomplete infrastructure runs before any Coach or gate decision.
        validity = compare(report, report, request["stage"], job.get("gate", "paired-v1"))
        if validity.get("reason") not in {"gain below fixed minimum", "passed"}:
            raise ValueError("invalid evaluation: " + validity["reason"])
        job["runtime_identity"] = identity

    def accept(self, job, report):
        self.verify_versions(job)
        request = self.request(job)
        if request.get("engineering_test"):
            from rsi.native_engineer import NativeEngineer
            return NativeEngineer(self).feedback(job, report=report)
        self.validate_report(job, report, request)
        path = self.report_path(job, job["step"])
        if path.exists() and json.loads(path.read_text()) != report:
            raise ValueError("cannot overwrite an evaluation")
        write_json(path, report)
        stage, mode, side = STEPS[job["step"]]
        if side == "candidate":
            base_index = next(i for i,s in enumerate(STEPS) if s == (stage, mode, "base"))
            base = json.loads(self.report_path(job, base_index).read_text())
            verdict = compare(base, report, stage, job.get("gate", "paired-v1"))
            job["verdicts"].append(verdict)
            if not verdict["passed"]:
                job.update(status="rejected", reason=verdict["reason"])
                self.save(job)
                return job
        job["step"] += 1
        job["status"] = "coaching" if job["step"] == 2 else "evaluating"
        if job["step"] == len(STEPS):
            # Re-read all evidence instead of trusting the checkpoint's verdict list.
            verdicts = []
            for stage in COUNTS:
                indices = [i for i,s in enumerate(STEPS) if s[0] == stage and s[1] == ("pixels" if stage == "audit" else "state")]
                reports = [json.loads(self.report_path(job, i).read_text()) for i in indices]
                verdicts.append(compare(*reports, stage, job.get("gate", "paired-v1")))
            if not all(v["passed"] for v in verdicts):
                raise ValueError("promotion evidence did not pass")
            self.store.promote_if_current(job["candidate"], job["parent"])
            job.update(status="promoted", promoted=True, verdicts=verdicts,
                       reason="state development/confirmation and independent pixel audit passed; next connection uses candidate")
            self.store.log(kind="native_rsi_promotion", job=job["id"], parent=job["parent"],
                           candidate=job["candidate"], verdicts=verdicts)
            # Live play and Portable training run the same strategy: copy the promotion across.
            from rsi.book_sync import mirror, peer_root
            try:
                skipped = mirror(self.settings.data_dir, peer_root(self.settings.data_dir), job["candidate"], job["parent"])
            except Exception as exc:
                skipped = f"{type(exc).__name__}: {exc}"[:300]
            job["mirrored"] = skipped is None
            if skipped:
                job["mirror_skipped"] = skipped
                self.store.log(kind="mirror_skipped", job=job["id"], candidate=job["candidate"], reason=skipped)
        self.save(job)
        return job
