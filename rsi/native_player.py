"""Evaluated System0 programs, used identically by pvz_rl and the live model pipeline.

Code derives observations/context and episode memory. It has no input authority:
only the unchanged Jev and trusted executor can issue a planting operation.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import queue
import threading

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rsi.code_runtime import CandidateProgram, _check_json, validate_source
from rsi.code_bundle import compile_bundle, unpack_source

SEED = '''def perceive(image):
    return image

def decide(observation, memory):
    return {"planner_context": {}, "jev_context": {}, "request_replan": False}
'''

CONTRACT = '''Native System0 contract, pvz_rl and live screenshot client:
perceive(image) receives a JSON object, NOT an Emulator RGB object:
{scene: current observed sun/cards/zombies/rows, plants: list of {row,col,plants},
 guidance: previous/current Planner message or null, committed_spend: integer,
 tick: game/observation ticks (100 per second), pixels: null or 16x12 RGB averages}.
Pixels[y][x] is [r,g,b]; no pixels are available in the state condition. Never assume
that observations are complete. Live images and Qwen observations may have different ages.
perceive returns derived JSON observations. decide(observation,memory) must return
exactly {planner_context: JSON dict, jev_context: JSON dict, request_replan: bool}.
memory is a mutable JSON dictionary private to one episode. You can implement algorithms
for observation fusion, image features, temporal state, resource accounting, tactical
distance and risk estimates, context/action-option construction, or planner refresh requests.
These are calculated model inputs, not direct planting commands. Planner receives the
planner_context; Jev receives jev_context. New code is actually executed before models.
Raw latest facts and hard legality checks remain separate and authoritative. Only Jev
chooses planting. You cannot issue mouse events, replace Jev, overwrite a chosen action,
change sun pickup, pause, score, reset, seeds, validation or promotion. No files/network,
imports/attributes, hidden state or judge data. No host helpers are available.
You may create, split, merge, rename and delete modules under player/; original filenames
and module count are not fixed. Use lowercase identifier directory/file names and .py.
Use edit_modules writes/deletes for atomic refactors. Modules are compiled in one restricted
namespace without imports. Define each entry function exactly once in any module, and use
only pure JSON arithmetic, loops and safe data methods. Max combined
source 24000 characters, memory 64 KiB, result 16 KiB, one call has a fixed time/memory cap.
State development and confirmation precede an independent pixels audit. Frozen-clock
tests are not real-time inference benchmarks. Keep code fast for the live client.
For native research only, jev_context may include runtime_schedule:
{seconds: 0.5..10, planner_seconds: 4..60, event_min_seconds: 0.25..5,
 events: subset of [new_threat,threat_advance,plant_lost,resource_ready],
selection: flat or hierarchical}. Omitted fields use defaults (2s/20s/.5s/all/flat).
Explicit playbook.timing.planner_seconds takes precedence over planner_seconds.
This tunes future model calls in GAME time, never creates an action. A periodic
heartbeat is mandatory; all edits are tested with unchanged quality/promotion gates.
The live screenshot client treats this as context only until separately validated.
'''


class NativeCodeStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "native_player_code"
        self.root.mkdir(parents=True, exist_ok=True)

    def add(self, source):
        source = compile_bundle(unpack_source(source, ()), ())
        version = hashlib.sha256(source.encode()).hexdigest()
        path = self.root / (version + ".py")
        if not path.exists():
            try:
                with path.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(source)
            except FileExistsError:
                pass
        if self.source(version) != source:
            raise ValueError("immutable code collision")
        return version

    def source(self, version=None):
        if version is None:
            return compile_bundle(unpack_source(SEED, ()), ())
        if not re.fullmatch(r"[0-9a-f]{64}", version):
            raise ValueError("invalid native code version")
        source = (self.root / (version + ".py")).read_text(encoding="utf-8")
        if hashlib.sha256(source.encode()).hexdigest() != version:
            raise ValueError("immutable native code changed")
        validate_source(source)
        return source


def player_input(scene, plants, guidance, spent, tick, jpeg=None):
    pixels = None
    if jpeg:
        import cv2
        import numpy as np
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.shape[:2] != (600, 800):
            raise ValueError("invalid player image")
        pixels = cv2.cvtColor(cv2.resize(frame, (16,12), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB).tolist()
    # JSON roundtrip removes int dictionary keys/tuples; no judge or native mask.
    return json.loads(json.dumps(dict(scene=scene.model_dump(), plants=plants,
        guidance=guidance.model_dump() if guidance else None, committed_spend=spent, tick=tick, pixels=pixels)))


def validate_result(result):
    if type(result) is not dict or set(result) != {"planner_context","jev_context","request_replan"}:
        raise ValueError("System0 may return contexts/replan only; no operation authority")
    if type(result["planner_context"]) is not dict or type(result["jev_context"]) is not dict or type(result["request_replan"]) is not bool:
        raise ValueError("invalid System0 result types")
    _check_json(result, "System0 result", 16384)
    return result


def _windows_job(process):
    """Assign before sending source; kill-on-close and process memory bound on Windows."""
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [("process_time",ctypes.c_int64),("job_time",ctypes.c_int64),("flags",wintypes.DWORD),
                    ("min_ws",ctypes.c_size_t),("max_ws",ctypes.c_size_t),("processes",wintypes.DWORD),
                    ("affinity",ctypes.c_size_t),("priority",wintypes.DWORD),("scheduling",wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name,ctypes.c_uint64) for name in ("read","write","other","read_bytes","write_bytes","other_bytes")]
    class Limits(ctypes.Structure):
        _fields_ = [("basic",Basic),("io",IO),("process_memory",ctypes.c_size_t),
                    ("job_memory",ctypes.c_size_t),("peak_process",ctypes.c_size_t),("peak_job",ctypes.c_size_t)]
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p,wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateJobObjectW(None,None)
    if not handle: raise ctypes.WinError(ctypes.get_last_error())
    limits = Limits(); limits.basic.flags = 0x2000 | 0x100
    limits.process_memory = 256 * 1024**2
    if not api.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not api.AssignProcessToJobObject(handle,int(process._handle)):
        error = ctypes.get_last_error(); api.CloseHandle(handle); raise ctypes.WinError(error)
    return lambda: api.CloseHandle(handle)


def execute(source, observation, memory, timeout=3):
    validate_source(source)
    _check_json(observation,"player input")
    _check_json(memory,"player memory")
    request = json.dumps(dict(source=source,observation=observation,memory=memory),ensure_ascii=False).encode()
    if len(request)>262144: raise ValueError("System0 request too large")
    # No credentials or model URLs in worker environment, arguments or working directory.
    env = {key:os.environ[key] for key in ("SYSTEMROOT","WINDIR") if key in os.environ}
    with tempfile.TemporaryDirectory(prefix="pvz-player-") as cwd:
        process = subprocess.Popen([sys.executable,"-I",str(Path(__file__).resolve()),"--worker"],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=cwd,env=env,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        close_job = None
        try:
            if sys.platform == "win32": close_job = _windows_job(process)
            output, error = process.communicate(request, timeout=timeout)
            if process.returncode or len(output)>150000:
                raise RuntimeError("isolated System0 worker failed")
            response = json.loads(output)
            if "error" in response: raise ValueError(response["error"])
            validate_result(response["result"])
            _check_json(response["memory"],"player memory")
            return response
        finally:
            if process.poll() is None: process.kill()
            process.wait()
            if close_job: close_job()


class PersistentWorker:
    """One bounded, credential-free subprocess; compile once, JSON per request."""
    def __init__(self, source):
        validate_source(source)
        self.source = source
        self.cwd = tempfile.TemporaryDirectory(prefix='pvz-player-')
        self.close_job = None
        self.process = None
        self.closed = False
        env = {key:os.environ[key] for key in ('SYSTEMROOT','WINDIR') if key in os.environ}
        try:
            self.process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), '--persistent'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=self.cwd.name, env=env, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if sys.platform == 'win32':
                self.close_job = _windows_job(self.process)
            self.responses = queue.Queue()
            stream, responses = self.process.stdout, self.responses
            def read():
                try:
                    while line := stream.readline(150001):
                        responses.put(line)
                        if len(line)>150000 or not line.endswith(b'\n'):
                            break
                finally:
                    responses.put(None)
            self.reader = threading.Thread(target=read, daemon=True)
            self.reader.start()
        except BaseException:
            self.close()
            raise

    def call(self, observation, memory, timeout=3):
        _check_json(observation, 'player input')
        _check_json(memory, 'player memory')
        request = json.dumps(dict(source=self.source, observation=observation, memory=memory), ensure_ascii=False).encode()+b'\n'
        if len(request)>262144:
            raise ValueError('System0 request too large')
        try:
            if self.closed:
                raise RuntimeError('System0 worker closed')
            self.process.stdin.write(request)
            self.process.stdin.flush()
            output = self.responses.get(timeout=timeout)
            if output is None or len(output)>150000:
                raise RuntimeError('isolated System0 worker failed')
            response = json.loads(output)
            if 'error' in response:
                raise ValueError(response['error'])
            validate_result(response['result'])
            _check_json(response['memory'], 'player memory')
            return response
        except queue.Empty as exc:
            self.close()
            raise TimeoutError('System0 call exceeded wall-time limit') from exc
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
        if self.close_job:
            self.close_job()
        self.cwd.cleanup()


class PlayerRuntime:
    def __init__(self, source):
        self.source, self.memory = source, {}
        self.lock = asyncio.Lock()
        self.generation = 0
        self.worker = None
        self.closed = False

    def reset(self):
        self.generation += 1
        self.memory = {}
        if self.worker:
            self.worker.close()
            self.worker = None

    def close(self):
        self.closed = True
        self.reset()

    def __del__(self):
        if getattr(self, 'worker', None):
            self.worker.close()

    async def step(self, observation):
        async with self.lock:
            generation = self.generation
            if self.closed:
                raise RuntimeError('player runtime closed')
            if self.worker is None:
                self.worker = PersistentWorker(self.source)
            response = await asyncio.to_thread(self.worker.call, observation, self.memory)
            if generation != self.generation:
                raise RuntimeError("episode changed during System0 call")
            self.memory = response["memory"]
            return response["result"]


def worker():
    if sys.platform == "linux":
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(256*1024**2,256*1024**2))
        resource.setrlimit(resource.RLIMIT_CPU,(2,2))
    try:
        request = json.loads(sys.stdin.buffer.read(262145))
        program = CandidateProgram(request["source"])
        observation = program.perceive(request["observation"])
        result = validate_result(program.decide(observation,request["memory"]))
        response = dict(result=result,memory=request["memory"])
    except Exception as exc:
        response = {"error":f"{type(exc).__name__}: {exc}"[:500]}
    sys.stdout.buffer.write(json.dumps(response,ensure_ascii=False,allow_nan=False).encode("utf-8"))


def persistent_worker():
    if sys.platform == 'linux':
        import math
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (256*1024**2,256*1024**2))
    program, source = None, None
    while line := sys.stdin.buffer.readline(262145):
        try:
            if len(line)>262144 or not line.endswith(b'\n'):
                raise ValueError('oversized worker input')
            if sys.platform == 'linux':
                used = resource.getrusage(resource.RUSAGE_SELF)
                resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(used.ru_utime+used.ru_stime)+2, resource.RLIM_INFINITY))
            request = json.loads(line)
            if program is None:
                source = request['source']
                program = CandidateProgram(source)
            if request['source'] != source:
                raise ValueError('code changed within worker')
            _check_json(request['observation'], 'player input')
            _check_json(request['memory'], 'player memory')
            result = validate_result(program.decide(program.perceive(request['observation']),request['memory']))
            _check_json(request['memory'], 'player memory')
            response = dict(result=result, memory=request['memory'])
        except Exception as exc:
            response = {'error':f'{type(exc).__name__}: {exc}'[:500]}
        sys.stdout.buffer.write(json.dumps(response,ensure_ascii=False,allow_nan=False).encode()+b'\n')
        sys.stdout.buffer.flush()
        if 'error' in response:
            return


if __name__ == "__main__":
    persistent_worker() if '--persistent' in sys.argv else worker()
