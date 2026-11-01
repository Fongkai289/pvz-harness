"""Durable pvz_rl jobs. Windows runs games; Linux owns models and promotion.

Only strategy knowledge can change. State development/confirmation is followed
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
from rsi.native_gate import COUNTS, compare
from server.playbook import PlaybookStore

TERMINAL = {"promoted", "rejected", "failed", "stale", "stopped"}
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

    def enqueue(self, session, *, retry=False):
        with file_lock(self.root / "QUEUE.lock"):
            return self._enqueue(session, retry=retry)

    def _enqueue(self, session, *, retry=False):
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
        parent = self.store.load()
        rng = secrets.SystemRandom()
        used = set()
        suites = {}
        for stage, count in COUNTS.items():
            seeds = []
            while len(seeds) < count:
                seed = rng.randrange(1, 2**30)
                if seed not in used:
                    used.add(seed)
                    # Initially cover supported ordinary day levels, not all Adventure modes.
                    seeds.append({"seed": seed, "level": (2, 3, 4, 6)[len(seeds) % 4]})
            suites[stage] = seeds
        job = dict(id=job_id, source_session=session, status="waiting_runtime", step=0,
                   parent=parent.version, parent_digest=digest(parent.model_dump()), candidate=None,
                   candidate_digest=None, suites=suites, verdicts=[], upstream=self.upstream,backend=self.backend,
                   scope=self.backend+" ordinary day levels 1-2,1-3,1-4,1-6; state dev/confirmation + pixel audit",
                   promoted=False, created=time.time())
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
                    verdicts.append(compare(*reports, stage))
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
        if self.store.current() != job["parent"]:
            raise ValueError("CURRENT changed; stale candidate cannot replace it")
        for role in ("parent", "candidate"):
            if job.get(role) and digest(self.store.load(job[role]).model_dump()) != job[role + "_digest"]:
                raise ValueError("immutable playbook changed")
            if job.get(role):
                self.code.source(self.store.load(job[role]).player_code)
        if job.get("evolution") == "code" and job.get("candidate"):
            candidate = self.store.load(job["candidate"])
            engineering = job.get("engineering") or {}
            if (not candidate.player_code or engineering.get("tested_version") != candidate.player_code
                    or engineering.get("pending")):
                raise ValueError("code candidate must be the exact completed Engineer test")

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
        validity = compare(report, report, request["stage"])
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
            verdict = compare(base, report, stage)
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
                verdicts.append(compare(*reports, stage))
            if not all(v["passed"] for v in verdicts):
                raise ValueError("promotion evidence did not pass")
            self.store.promote_if_current(job["candidate"], job["parent"])
            job.update(status="promoted", promoted=True, verdicts=verdicts,
                       reason="state development/confirmation and independent pixel audit passed; next connection uses candidate")
            self.store.log(kind="native_rsi_promotion", job=job["id"], parent=job["parent"],
                           candidate=job["candidate"], verdicts=verdicts)
        self.save(job)
        return job
