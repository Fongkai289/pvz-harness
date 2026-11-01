"""Private game/DLL process. Only the parent executor talks to this pipe."""
import base64
from dataclasses import asdict, is_dataclass
import json
import sys

from rsi.native_runtime import NativeArena


def encode(value):
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f'unsupported native result {type(value).__name__}')


def main():
    arena = None
    try:
        while line := sys.stdin.readline(4_000_001):
            if len(line)>4_000_000:
                raise ValueError('oversized arena request')
            request = json.loads(line)
            try:
                op = request['op']
                if op == 'init' and arena is None:
                    arena = NativeArena(request['config'],request['mode'])
                    result = True
                elif op == 'reset' and arena:
                    result = arena.reset(request['level'],request['seed'])
                elif op == 'step' and arena:
                    result = arena.step(request['placement'])
                elif op == 'pixels' and arena:
                    result = base64.b64encode(arena.pixels()).decode()
                else:
                    raise ValueError('invalid arena operation')
                response = dict(ok=True,result=result)
            except Exception as exc:
                response = dict(ok=False,error=f'{type(exc).__name__}: {exc}')
            print(json.dumps(dict(rpc_id=request['rpc_id'],**response),default=encode),flush=True)
    finally:
        if arena:
            arena.close()


if __name__ == '__main__':
    main()
