from common.protocol import (
    ExecReport,
    FrameHeader,
    Hello,
    StateMsg,
    dumps,
    pack_frame,
    parse_client_message,
    unpack_frame,
)
from server import geometry


def test_frame_roundtrip():
    header = FrameHeader(frame_id=7, t_capture=123.5, width=800, height=600)
    got_header, got_jpeg = unpack_frame(pack_frame(header, b"\xff\xd8jpeg"))
    assert got_header == header
    assert got_jpeg == b"\xff\xd8jpeg"


def test_client_messages_dispatch_on_type():
    assert isinstance(parse_client_message(dumps(Hello(token="t", client_id="c"))), Hello)
    assert isinstance(parse_client_message(dumps(StateMsg(t=1.0, data={"sun": 50}))), StateMsg)
    msg = parse_client_message(dumps(ExecReport(cmd_id=1, frame_id=2, t_exec=3.0, ok=True)))
    assert isinstance(msg, ExecReport)


def test_geometry_bounds():
    assert geometry.cell_center(0, 0) == (80, 130)
    x, y = geometry.cell_center(4, 8)
    assert 0 < x < geometry.WIDTH and 0 < y < geometry.HEIGHT
