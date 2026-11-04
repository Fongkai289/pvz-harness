"""OS-released file locks; no stale PID deletion after process crashes."""
from contextlib import contextmanager
import os
import time


def _try_lock(stream, blocking):
    if os.name == "nt":
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


@contextmanager
def file_lock(path, *, blocking=True, timeout=None):
    """timeout: keep retrying a held lock for up to this many seconds, then raise like blocking=False."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt" and stream.tell() == 0:
            stream.write(b"0"); stream.flush()
        if timeout is None:
            _try_lock(stream, blocking)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    _try_lock(stream, False)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(1)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
