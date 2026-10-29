"""Wire protocol shared by the Windows client and the server.

Binary WebSocket message: 4-byte big-endian header length + JSON FrameHeader + JPEG bytes.
Text WebSocket message: one JSON object whose "type" field picks the model below.

All client timestamps (t_capture, t_exec) are on the client's clock, so
end-to-end latency (capture -> click) is measured on one clock without sync.
"""

from __future__ import annotations

import json
import struct
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

PROTOCOL_VERSION = 1


class FrameHeader(BaseModel):
    type: Literal["frame"] = "frame"
    frame_id: int
    t_capture: float
    width: int
    height: int


def pack_frame(header: FrameHeader, jpeg: bytes) -> bytes:
    head = header.model_dump_json().encode()
    return struct.pack(">I", len(head)) + head + jpeg


def unpack_frame(data: bytes) -> tuple[FrameHeader, bytes]:
    (n,) = struct.unpack(">I", data[:4])
    header = FrameHeader.model_validate_json(data[4 : 4 + n])
    return header, data[4 + n :]


# ---- client -> server (text) ----


class Hello(BaseModel):
    type: Literal["hello"] = "hello"
    protocol: int = PROTOCOL_VERSION
    token: str
    client_id: str
    game_version: str | None = None
    playbook: str | None = None  # evaluation runs pin a version; None = the promoted one


class StateMsg(BaseModel):
    """Ground-truth game state read from memory. Judge and coach only, never the player."""

    type: Literal["state"] = "state"
    t: float
    frame_id: int | None = None
    data: dict[str, Any]


class ExecReport(BaseModel):
    type: Literal["exec"] = "exec"
    cmd_id: int
    frame_id: int | None
    t_exec: float
    ok: bool
    error: str | None = None
    # Ground-truth outcome when the client knows it (the fake game does). Judge and coach only.
    result: dict[str, Any] | None = None


class EpisodeEvent(BaseModel):
    type: Literal["episode"] = "episode"
    event: Literal["start", "end"]
    level: str | None = None
    info: dict[str, Any] = {}


ClientMessage = Annotated[
    Union[Hello, StateMsg, ExecReport, EpisodeEvent], Field(discriminator="type")
]
client_message_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


def parse_client_message(text: str) -> ClientMessage:
    return client_message_adapter.validate_json(text)


# ---- server -> client (text) ----


class Command(BaseModel):
    type: Literal["cmd"] = "cmd"
    cmd_id: int
    # The frame this decision was based on; lets the client compute capture -> exec latency.
    frame_id: int | None = None
    t_capture: float | None = None
    action: Literal["click", "pause", "resume", "set_speed", "noop"]
    x: int | None = None
    y: int | None = None
    speed: float | None = None
    reason: str = ""


class Welcome(BaseModel):
    type: Literal["welcome"] = "welcome"
    protocol: int = PROTOCOL_VERSION
    session_id: str
    playbook: str


def dumps(msg: BaseModel) -> str:
    return msg.model_dump_json()


def loads(text: str) -> dict[str, Any]:
    return json.loads(text)
