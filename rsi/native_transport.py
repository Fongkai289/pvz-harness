"""Multiplex bounded JSON-lines requests over one owned process/SSH connection."""
import json
import queue
import threading
import time
import uuid


class JsonRPC:
    def __init__(self, process, check=lambda: None):
        self.process, self.check = process, check
        self.pending = {}
        self.lock, self.writer = threading.Lock(), threading.Lock()
        self.failure = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def fail(self, error):
        with self.lock:
            self.failure = error
            for response in self.pending.values():
                response.put(error)

    def _read(self):
        try:
            while line := self.process.stdout.readline(4_000_001):
                if len(line)>4_000_000 or not line.endswith('\n'):
                    raise ValueError('oversized or incomplete RPC response')
                reply = json.loads(line)
                with self.lock:
                    target = self.pending.get(reply.get('rpc_id'))
                if target is None:
                    raise ValueError('unknown RPC response identity')
                target.put(reply)
            raise RuntimeError('research process disconnected')
        except Exception as exc:
            self.fail(exc)

    def call(self, op, **payload):
        self.check()
        rpc_id, response = uuid.uuid4().hex, queue.Queue()
        with self.lock:
            if self.failure:
                raise RuntimeError('RPC connection failed') from self.failure
            self.pending[rpc_id] = response
        try:
            line = json.dumps(dict(op=op, rpc_id=rpc_id, **payload),ensure_ascii=False)+'\n'
            if len(line)>4_000_000:
                raise ValueError('oversized RPC request')
            with self.writer:
                self.process.stdin.write(line)
                self.process.stdin.flush()
            deadline = time.monotonic()+300
            while True:
                self.check()
                if time.monotonic()>deadline:
                    raise TimeoutError('research RPC deadline')
                try:
                    reply = response.get(timeout=.2)
                    break
                except queue.Empty:
                    pass
            if isinstance(reply, Exception):
                raise RuntimeError('RPC connection failed') from reply
            if not reply.get('ok'):
                raise RuntimeError(reply.get('error', 'invalid RPC response'))
            self.check()
            return reply['result']
        finally:
            with self.lock:
                self.pending.pop(rpc_id, None)


class ArenaProcess:
    """A separate Python/DLL instance per game lane; no model credentials sent."""
    def __init__(self, config, mode):
        import subprocess
        import sys
        from pathlib import Path
        from rsi.native_loop import control
        self.root = Path(config['work_dir'])
        self.log = (self.root/f"arena-{config.get('instance_id',0)}.log").open('a',encoding='utf-8')
        self.process = subprocess.Popen([sys.executable,'-X','utf8','-u','-m','rsi.native_arena_worker'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf-8',
            cwd=Path(__file__).resolve().parents[1],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.rpc = JsonRPC(self.process, lambda: control(self.root))
        public_config = {k:config[k] for k in ('repository','game_executable','dll','work_dir','instance_id',
                                             'backend','resources','runtime_bin') if k in config}
        try:
            self.rpc.call('init', config=public_config, mode=mode)
        except BaseException:
            self.close()
            raise

    def reset(self, level, seed):
        return self.rpc.call('reset', level=level, seed=seed)

    def step(self, placement):
        return self.rpc.call('step', placement=placement)

    def shovel(self, cell):
        return self.rpc.call('shovel', cell=cell)

    def prepare(self, level, seed):
        return tuple(self.rpc.call('prepare', level=level, seed=seed))

    def choose(self, plants):
        return self.rpc.call('choose', plants=plants)

    def pixels(self):
        import base64
        return base64.b64decode(self.rpc.call('pixels'),validate=True)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()  # Owned native process closes its kill-on-close game Job.
        self.process.wait(timeout=10)
        self.rpc.fail(RuntimeError('arena closed'))
        self.log.close()
