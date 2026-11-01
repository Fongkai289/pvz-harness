import asyncio
import json

from client.link import ServerLink
from common.protocol import Command, ModelTiming, dumps


def test_small_sample_p95_is_not_lower_than_median():
    link = ServerLink("ws://unused", "test", "test", lambda: None, None)
    link.latencies = [0.3852, 0.4492]
    assert link.latency_summary()["latency_p95_ms"] == 449.2
    assert link.latency_summary()["latency_median_ms"] == 417.2


def test_telemetry_is_not_executed_and_failed_clicks_do_not_pollute_latency():
    async def check():
        messages = [dumps(ModelTiming(stage="plan", latency_ms=1250)),
                    dumps(Command(cmd_id=1, action="click", t_capture=0)),
                    dumps(Command(cmd_id=2, action="resume", t_capture=0)),
                    dumps(Command(cmd_id=3, action="click", t_capture=0))]
        executed, reports = [], []

        class WS:
            async def send(self, data):
                reports.append(data)

            def __aiter__(self):
                async def stream():
                    for message in messages:
                        yield message
                return stream()

        async def execute(cmd):
            executed.append(cmd.cmd_id)
            if cmd.cmd_id == 3:
                raise RuntimeError("operator paused")

        link = ServerLink("ws://unused", "test", "test", lambda: None, execute)
        await link._recv_commands(WS())
        assert executed == [1, 2, 3]
        assert link.model_timings["plan"]["latency_ms"] == 1250
        assert len(link.latencies) == 1
        assert link.bytes_received == sum(len(m.encode()) for m in messages)
        assert link.bytes_sent == sum(len(m.encode()) for m in reports)
        assert json.loads(reports[-1])["ok"] is False
    asyncio.run(check())


def test_network_rtt_uses_websocket_ping_round_trip(monkeypatch):
    async def check():
        class WS:
            async def ping(self):
                result = asyncio.get_running_loop().create_future()
                result.set_result(0.125)
                return result

        async def stop_after_sample(_):
            raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "sleep", stop_after_sample)
        link = ServerLink("ws://unused", "test", "test", lambda: None, None)
        try:
            await link._measure_rtt(WS())
        except asyncio.CancelledError:
            pass
        assert link.rtt_ms == 125
    asyncio.run(check())
