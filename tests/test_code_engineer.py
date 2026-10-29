"""Role separation, actual tool edits/repair, and the immutable judge boundary."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsi import code_bundle, code_coach, code_engineer, code_loop, code_model
from rsi.code_bundle import FILES, compile_bundle, unpack_source
from rsi.code_engineer import EngineerAction, EngineerWorkspace
from rsi.code_evaluate import Interrupted
from rsi.code_runtime import CandidateProgram
from rsi.code_store import CodeStore, digest
from tests.test_code_loop import report

SOURCE = "def perceive(image):\n    return {}\n\ndef decide(observation, memory):\n    return None\n"


def action(name, **kwargs):
    return EngineerAction(action=name, explanation="test evidence", **kwargs)


@pytest.fixture
def workspace(tmp_path):
    return EngineerWorkspace(SOURCE, tmp_path / "engineer", {"docs/test.txt": "read-only"}, [17, 23], tmp_path)


def test_bundle_retains_executable_tools_and_module_boundaries():
    files = unpack_source(SOURCE)
    files["player/tools.py"] = "def detect(image):\n    return {'count': len(image)}\n"
    files["player/perception.py"] = "def perceive(image):\n    return detect(image)\n"
    bundled = compile_bundle(files)
    assert compile_bundle(unpack_source(bundled)) == bundled
    assert CandidateProgram(bundled).perceive([1, 2]) == {"count": 2}
    assert unpack_source(bundled)["player/tools.py"].startswith("def detect")


def test_seed_conversion_preserves_perception_and_decisions():
    from rsi.pixel_tools import HELPERS
    from sim.render import PLANTS, render
    seed = (code_loop.ROOT / "playbook_seed/sim_player.py").read_text(encoding="utf-8")
    old, new = CandidateProgram(seed, HELPERS), CandidateProgram(compile_bundle(unpack_source(seed)), HELPERS)
    frame = render(sun=50, plants=[], zombies=[], cards=[{"plant": p, "ready": True} for p in PLANTS],
                   tick=0, limit=18000, outcome="running")
    assert old.perceive(frame) == new.perceive(frame)
    assert old.decide(old.perceive(frame), {}) == new.decide(new.perceive(frame), {})


@pytest.mark.parametrize("path", ["../judge.py", "C:/Windows/test.py", "rules/code_gate.py",
                                  "evidence/confirmation.json", ".env", "docs/test.txt"])
def test_write_capability_rejects_every_nonplayer_path(workspace, path):
    with pytest.raises(ValueError, match="writable"):
        workspace.dispatch(action("write_file", path=path, content="bad"))
    assert workspace.files == workspace.original


def test_read_capability_cannot_access_secrets_or_holdouts(workspace):
    for path in (".env", "../status.json", "evidence/confirmation.json", "rules/code_gate.py"):
        with pytest.raises(ValueError, match="capability"):
            workspace.dispatch(action("read_file", path=path))
    assert workspace.dispatch(action("read_file", path="docs/test.txt"))["content"] == "read-only"


@pytest.mark.parametrize("code", ["import os", "def leak():\n    return open('secret')",
                                  "# file: player/policy.py\ndef cheat():\n    return 1"])
def test_tool_modules_cannot_escape_restricted_language(workspace, code):
    workspace.dispatch(action("write_file", path="player/tools.py", content=code))
    with pytest.raises(ValueError):
        workspace.dispatch(action("check"))


def test_finish_requires_successful_test_of_exact_final_source(workspace, monkeypatch):
    workspace.dispatch(action("write_file", path="player/tools.py", content="def helper():\n    return 1\n"))
    finish = action("finish", diagnosis="observed failure", rationale="repair tested")
    with pytest.raises(ValueError, match="exact current"):
        workspace.dispatch(finish)
    monkeypatch.setattr(code_engineer, "evaluate", lambda path, seeds, output, **kw:
                        report(seeds, 0.2, path.read_text()))
    result = workspace.dispatch(action("test_candidate"))
    assert "seed" not in json.dumps(result)
    assert workspace.dispatch(finish)["finished"]
    workspace.dispatch(action("write_file", path="player/tools.py", content="def helper():\n    return 2\n"))
    with pytest.raises(ValueError, match="exact current"):
        workspace.dispatch(finish)


def test_test_budget_and_failed_worker_cannot_be_bypassed(workspace, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("worker failed")
    monkeypatch.setattr(code_engineer, "evaluate", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="worker failed"):
            workspace.dispatch(action("test_candidate"))
    with pytest.raises(ValueError, match="budget"):
        workspace.dispatch(action("test_candidate"))
    assert workspace.tested_version is None


def test_stop_blocks_file_mutation(workspace):
    (workspace.controls / "STOP").touch()
    with pytest.raises(Interrupted):
        workspace.dispatch(action("write_file", path=FILES[0], content="bad"))
    assert workspace.files == workspace.original


def test_coach_schema_cannot_submit_code():
    with pytest.raises(ValueError):
        code_coach.CoachTask(diagnosis="failure", evidence=["record"], objective="improve",
                             source=SOURCE)


def test_engineer_repairs_invalid_tool_code_using_its_own_feedback(tmp_path, monkeypatch):
    actions = iter([
        action("read_file", path="docs/contract.txt"),
        action("write_file", path="player/tools.py", content="import os"),
        action("check"),
        action("write_file", path="player/tools.py", content="def helper():\n    return 1\n"),
        action("test_candidate"),
        action("finish", diagnosis="engineer disagrees with coach", rationale="evidence from tool test"),
    ])
    requests = []
    async def request(role, messages, schema, settings, **kwargs):
        requests.append(json.loads(json.dumps(messages)))
        return next(actions), {"role": role, "latency_seconds": 0.01}
    monkeypatch.setattr(code_engineer, "request_role", request)
    monkeypatch.setattr(code_engineer, "evaluate", lambda path, seeds, output, **kw:
                        report(seeds, 0.2, path.read_text()))
    result, calls = asyncio.run(code_engineer.engineer_code(SOURCE, {"diagnosis": "coach hypothesis"},
        report([938713], 0.1), [], SimpleNamespace(), controls=tmp_path, root=tmp_path / "engineer",
        renderer_contract="pixels", pixel_helpers="pixel", language_contract="no imports"))
    assert len(calls) == 6
    assert "only functions" in json.dumps(requests[3])
    assert result["diagnosis"] == "engineer disagrees with coach"
    assert "def helper" in result["source"]
    assert "938713" not in json.dumps(requests)
    assert (tmp_path / "engineer/candidate.diff").exists()
    assert (tmp_path / "engineer/manifest.json").exists()


def test_real_role_orchestration_reaches_judge_and_preserves_modules(tmp_path, monkeypatch):
    import server.config
    args = SimpleNamespace(data_dir=tmp_path / "data", rounds=2, baseline_only=False, settings_env=None)
    monkeypatch.setattr(code_loop, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(server.config, "load_settings", lambda *_: SimpleNamespace(qwen_model="test-qwen"))
    coach_requests, engineer_requests = [], []
    counter = {"round": 0}
    actions = iter(())

    async def request(role, messages, schema, settings, **kwargs):
        nonlocal actions
        if role == "coach":
            counter["round"] += 1
            coach_requests.append(json.loads(json.dumps(messages)))
            actions = iter([
                action("read_file", path="evidence/development.json"),
                action("write_file", path="player/tools.py",
                       content=f"def helper():\n    return {counter['round']}\n"),
                action("test_candidate"),
                action("finish", diagnosis="engineer evidence", rationale="tested candidate"),
            ])
            value = code_coach.CoachTask(diagnosis="coach hypothesis", evidence=["development"], objective="improve")
        else:
            engineer_requests.append(json.loads(json.dumps(messages)))
            value = next(actions)
        return value, {"role": role, "latency_seconds": 0.01}

    def evaluate(path, seeds, output, **kwargs):
        score = 0.2 if output.stem.endswith("-base") else 0.5
        marker = "HIDDEN_CANARY" if any(s in output.stem for s in ("audit", "confirmation")) else "development"
        return report(seeds, score, path.read_text(encoding="utf-8"), marker)

    monkeypatch.setattr(code_coach, "request_role", request)
    monkeypatch.setattr(code_engineer, "request_role", request)
    monkeypatch.setattr(code_engineer, "evaluate", evaluate)
    monkeypatch.setattr(code_loop, "evaluate", evaluate)
    asyncio.run(code_loop.run(args))
    store = CodeStore(args.data_dir)
    champion = store.source(store.current())
    assert "return 2" in unpack_source(champion)["player/tools.py"]
    assert len(coach_requests) == 2 and len(engineer_requests) == 8
    assert "return 1" in json.dumps(coach_requests[1])
    assert "HIDDEN_CANARY" not in json.dumps(coach_requests + engineer_requests)
    assert "replace one" not in json.dumps(coach_requests).lower()
    run = Path(json.loads((store.root / "LATEST_RUN.json").read_text())["path"])
    status = json.loads((run / "status.json").read_text())
    assert status["promotions"] == 2
    proposal = json.loads((run / "round1-proposal.json").read_text())
    assert proposal["model"]["model_calls"] == 5
    assert [c["role"] for c in proposal["model"]["calls"]] == ["coach"] + ["engineer"] * 4
    assert json.loads((store.candidates / f"{store.current()}.json").read_text())["origin"] == "qwen-engineer"


def test_model_stop_prevents_any_network_request(tmp_path, monkeypatch):
    monkeypatch.setattr(code_model, "sys", SimpleNamespace(platform="linux"))
    (tmp_path / "STOP").touch()
    monkeypatch.setattr(code_model.httpx, "AsyncClient", lambda **kw: pytest.fail("must not make a request"))
    with pytest.raises(Interrupted):
        asyncio.run(code_model.request_role("coach", [], code_coach.CoachTask, SimpleNamespace(),
                    controls=tmp_path, audit_dir=tmp_path / "calls"))


def test_engineer_tool_module_runs_in_native_worker(workspace):
    pytest.importorskip("pvzemu")
    workspace.seeds = [123]
    workspace.dispatch(action("write_file", path="player/tools.py",
                              content="def inspect_frame(image):\n    return {'dimensions': image_size(image)}\n"))
    workspace.dispatch(action("write_file", path="player/perception.py",
                              content="def perceive(image):\n    return inspect_frame(image)\n"))
    result = workspace.dispatch(action("test_candidate"))
    trace = result["development"]["worst_traces"][0]
    assert trace[0]["observation"] == {"dimensions": [180, 140]}
    assert workspace.dispatch(action("finish", diagnosis="tested pixel tool",
                                     rationale="native worker executed module"))["finished"]


@pytest.mark.parametrize("status", [200, 400])
def test_role_transport_audits_success_and_http_failure(tmp_path, monkeypatch, status):
    import httpx
    monkeypatch.setattr(code_model, "sys", SimpleNamespace(platform="linux"))
    payload = {"id": "response-test", "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"diagnosis": "failure", "evidence": ["trace"], "objective": "improve"})}}],
        "usage": {"total_tokens": 17}}
    sent = []
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, json):
            sent.append(json)
            return httpx.Response(status, json=payload if status == 200 else {"error": "test API error"},
                                  request=httpx.Request("POST", url))
    monkeypatch.setattr(code_model.httpx, "AsyncClient", lambda **kw: Client())
    settings = SimpleNamespace(qwen_url="http://model.invalid/v1/chat/completions", qwen_model="test-qwen",
                               token="SECRET_MUST_NOT_APPEAR")
    def run():
        return asyncio.run(code_model.request_role("coach", [{"role": "user", "content": "evidence"}],
            code_coach.CoachTask, settings, controls=tmp_path, audit_dir=tmp_path / "calls"))
    if status == 400:
        with pytest.raises(httpx.HTTPStatusError):
            run()
        assert list((tmp_path / "calls").glob("*-error.json"))
    else:
        result, metadata = run()
        assert result.objective == "improve"
        assert metadata["role"] == "coach" and metadata["response_id"] == "response-test"
        assert metadata["usage"] == {"total_tokens": 17}
    assert "SECRET_MUST_NOT_APPEAR" not in json.dumps(sent)
    saved = json.loads(next((tmp_path / "calls").glob("*-response.json")).read_text())
    assert saved["status_code"] == status
    if status == 400:
        assert "test API error" in saved["body"]
