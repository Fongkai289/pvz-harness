"""Private SSH broker: Linux owns tools/gates; Windows owns the Codex login.

Requests are leased once, bounded by deadline, and never silently fall back to Qwen.
An identical completed request can be delivered repeatedly without another model call.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from common.file_lock import file_lock
from rsi.code_store import write_json
from rsi.code_evaluate import Interrupted
from rsi.coach_usage import summarize_calls


class CoachQueue:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, identity):
        if not re.fullmatch('[0-9a-f]{64}', identity): raise ValueError('invalid Coach request id')
        return self.root / (identity + '.json')

    def read(self, identity):
        return json.loads(self.path(identity).read_text(encoding='utf-8'))

    def save(self, item):
        write_json(self.path(item['id']), item)

    def stopped(self, item):
        controls = Path(item['controls'])
        if any((controls / name).exists() for name in ('STOP', 'PAUSE')): return True
        if (controls/'job.json').exists():
            job=json.loads((controls/'job.json').read_text(encoding='utf-8'))
            return job.get('paused') or job.get('status') in ('stopped','stale','failed','rejected','promoted','tested')
        return False

    def refresh(self, item):
        if item['status'] in ('queued', 'running'):
            if self.stopped(item) or time.time() > item['deadline']:
                item.update(status='cancelled', error='Coach paused/stopped or deadline exceeded')
                self.save(item)
        return item

    def dispatch(self, request):
        with file_lock(self.root / 'QUEUE.lock'):
            op = request['op']
            if op == 'stats':
                rounds = {}
                for path in sorted(self.root.glob('*.json'),key=lambda p:p.stat().st_mtime):
                    item = self.refresh(json.loads(path.read_text(encoding='utf-8')))
                    row = rounds.setdefault(item['round_id'], {'round_id': item['round_id'], 'calls': []})
                    row['calls'].append({'request_id': item['id'], 'status': item['status'],
                                         **item.get('metadata', {})})
                return [{**row, 'tokens': summarize_calls(row['calls'])} for row in rounds.values()]
            if op == 'next':
                for path in sorted(self.root.glob('*.json'), key=lambda p: p.stat().st_mtime):
                    item = self.refresh(json.loads(path.read_text(encoding='utf-8')))
                    if item['status'] == 'queued':
                        item.update(status='running', owner=request['owner'], started=time.time())
                        self.save(item)
                        return {k: v for k, v in item.items() if k != 'controls'}
                return None
            item = self.refresh(self.read(request['id']))
            if item.get('owner') != request['owner']: raise ValueError('Coach lease owner mismatch')
            if op == 'progress':
                if request.get('metadata'): item['metadata'] = request['metadata']; self.save(item)
                return {'status': item['status']}
            if op == 'complete':
                # Keep token evidence even if a cancellation raced the final response.
                if item['status'] in ('completed', 'failed'): return {'status': item['status']}
                item['metadata'] = request.get('metadata', {})
                if item['status'] == 'running':
                    item.update(status='failed' if request.get('error') else 'completed',
                                result=request.get('result'), error=request.get('error'), finished=time.time())
                self.save(item)
                return {'status': item['status']}
            raise ValueError('unknown Coach queue operation')


async def request_coach(role, messages, schema, settings, *, controls, audit_dir):
    controls, audit_dir = Path(controls).resolve(), Path(audit_dir).resolve()
    if any((controls / p).exists() for p in ('STOP', 'PAUSE')): raise Interrupted('Coach paused/stopped')
    queue = CoachQueue(settings.coach_queue_dir)
    payload = dict(role=role, messages=messages, schema=schema.model_json_schema(), model=settings.coach_model)
    # Same logical action on reconnect reuses the result, including an explicit failure.
    identity = hashlib.sha256(json.dumps([str(audit_dir), payload], sort_keys=True).encode()).hexdigest()
    round_id = controls.relative_to(Path(__file__).resolve().parents[1]).as_posix() if controls.is_relative_to(
        Path(__file__).resolve().parents[1]) else str(controls)
    with file_lock(queue.root / 'QUEUE.lock'):
        if not queue.path(identity).exists():
            queue.save(dict(id=identity, round_id=round_id, controls=str(controls), status='queued',
                            created=time.time(), deadline=time.time()+settings.coach_timeout, **payload))
    audit_dir.mkdir(parents=True, exist_ok=True)
    write_json(audit_dir / (identity+'-request.json'), payload)
    try:
        while True:
            with file_lock(queue.root / 'QUEUE.lock'):
                item = queue.refresh(queue.read(identity))
            if item['status'] not in ('queued', 'running'): break
            await asyncio.sleep(.2)
        metadata = {'role': role, 'provider': 'codex', 'model': settings.coach_model,
                    'request_id': identity, **item.get('metadata', {})}
        write_json(audit_dir / (identity+'-response.json'), item)
        write_json(controls / 'coach_tokens.json', next(row for row in queue.dispatch({'op':'stats'})
                   if row['round_id'] == round_id))
        if item['status'] == 'cancelled': raise Interrupted(item['error'])
        if item['status'] != 'completed': raise RuntimeError(item.get('error') or 'Codex Coach failed')
        return schema.model_validate(item['result']), metadata
    except asyncio.CancelledError:
        with file_lock(queue.root / 'QUEUE.lock'):
            item = queue.read(identity)
            if item['status'] in ('queued', 'running'):
                item.update(status='cancelled', error='Calling task cancelled'); queue.save(item)
        raise


async def main():
    from server.config import load_settings
    from rsi.native_bridge import serve
    queue = CoachQueue(load_settings().coach_queue_dir)
    class Bridge:
        async def handle(self, request): return queue.dispatch(request)
    await serve(Bridge())


if __name__ == '__main__': asyncio.run(main())
