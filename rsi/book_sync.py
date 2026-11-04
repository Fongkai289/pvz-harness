"""Live play and Portable training share one strategy.

A version promoted through the gate in one store (live data_dir or data_dir/portable_study) is
copied, with its player code, into the other and made CURRENT there, but only while that store is
still on the same parent version. Otherwise nothing is overwritten and the reason is returned.
Per-level playbooks (server/level_books.py) are mirrored the same way with mirror_level.
"""
from pathlib import Path

from rsi.native_player import NativeCodeStore
from server.level_books import LevelBooks
from server.playbook import PlaybookStore

STUDY = "portable_study"


def peer_root(data_dir) -> Path:
    data_dir = Path(data_dir)
    return data_dir.parent if data_dir.name == STUDY else data_dir / STUDY


def _copy(src_root, dst_root, src, dst, version):
    """Put `version` and its code into dst; None, or why that was impossible."""
    book = src.load(version)
    if (dst.dir / f"{version}.json").exists():
        if dst.load(version) != book:
            return f"{dst_root.name} already has a different {version}"
    else:
        dst.write(book)
    if book.player_code:
        source = NativeCodeStore(src_root).source(book.player_code)
        if NativeCodeStore(dst_root).add(source) != book.player_code:
            raise ValueError("player code changed while copying")
    return None


def mirror(src_root, dst_root, version, parent):
    """None when dst now runs `version`, else why it was left alone."""
    src_root, dst_root = Path(src_root), Path(dst_root)
    if not (dst_root / "CURRENT").exists():
        return f"{dst_root.name} has no playbook store yet"
    src, dst = PlaybookStore(src_root), PlaybookStore(dst_root)
    book = src.load(version)
    if (dst.dir / f"{version}.json").exists() and dst.load(version) != book:
        return f"{dst_root.name} already has a different {version}"
    if dst.current() == version:
        return None
    if dst.current() != parent:
        return f"{dst_root.name} CURRENT is {dst.current()}, not the parent {parent}"
    if reason := _copy(src_root, dst_root, src, dst, version):
        return reason
    dst.promote_if_current(version, parent)
    dst.log(kind="mirrored_promotion", source=src_root.name, version=version, parent=parent)
    return None


def mirror_level(src_root, dst_root, level, version, parent):
    """Same for one level's playbook: dst's level moves to `version` only if it still plays `parent`."""
    src_root, dst_root = Path(src_root), Path(dst_root)
    if not (dst_root / "CURRENT").exists():
        return f"{dst_root.name} has no playbook store yet"
    src, dst = PlaybookStore(src_root), PlaybookStore(dst_root)
    books = LevelBooks(dst_root)
    if books.get(level) == version and dst.load(version) == src.load(version):
        return None
    # Playing the same version only because it is CURRENT is not enough: CURRENT may move later.
    if books.version(dst, level) != parent:
        return f"{dst_root.name} level {level} plays {books.version(dst, level)}, not the parent {parent}"
    if reason := _copy(src_root, dst_root, src, dst, version):
        return reason
    books.set(dst, level, version, expected=parent)
    dst.log(kind="mirrored_level_book", source=src_root.name, level=level, version=version, parent=parent)
    return None
