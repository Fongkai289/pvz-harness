"""Loopback transport for the existing panel; actions run on its UI thread."""
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class PanelBridge:
    def __init__(self, port=8767):
        self.snapshot = {}
        self.commands = queue.Queue(maxsize=8)
        self.port = port
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def allowed(self):
                return (self.headers.get('Host') == f'127.0.0.1:{bridge.port}'
                        and not self.headers.get('Origin')
                        and self.headers.get('X-PvZ-Panel') == 'local')

            def reply(self, code, value):
                body = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self.allowed():
                    self.reply(403, {'error': 'local transport only'}); return
                if self.path != '/status':
                    self.reply(404, {}); return
                self.reply(200, bridge.snapshot)

            def do_POST(self):
                if not self.allowed():
                    self.reply(403, {'error': 'local transport only'}); return
                action = self.path.removeprefix('/action/')
                if action not in ('开始', '暂停', '继续', '结束', '选植物', '测试连接'):
                    # Transport uses ASCII action names.
                    action = {'start': '开始', 'pause': '暂停', 'resume': '继续', 'end': '结束',
                              'choose_seeds': '选植物', 'test_links': '测试连接'}.get(action)
                state = bridge.snapshot
                if not action or time.time() - state.get('updated', 0) > 3 or not state.get('buttons', {}).get(action):
                    self.reply(409, {'error': '客户端状态已变化，请刷新后重试'}); return
                try:
                    bridge.commands.put_nowait((action, state['generation'], time.monotonic()))
                except queue.Full:
                    self.reply(409, {'error': '操作队列繁忙'}); return
                self.reply(202, {'queued': True})

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def drain(self, panel):
        callbacks = {'开始': panel._on_start, '暂停': panel._on_pause,
                     '继续': panel._on_resume, '结束': panel._on_stop, '选植物': panel._on_choose_seeds,
                     '测试连接': panel._on_test_links}
        while True:
            try:
                action, generation, created = self.commands.get_nowait()
            except queue.Empty:
                break
            if generation == panel.generation and time.monotonic() - created < 3:
                callbacks[action]()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
