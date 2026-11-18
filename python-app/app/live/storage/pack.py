"""A stored session's two large parts, compressed in place and still readable.

The observer's screenshots are PNG files of screens that change little from one to the next. A PNG is compressed on
its own, so the files do not shrink in an archive; their pixels do. ``compress`` packs the pixels, 32 screenshots to a
block, each block one xz stream in ``screenshots.pack``, with an index beside it (``screenshots.pack.json``: per file
its block, where its pixels are there, its size, mode and SHA-256). A PNG written back from its pixels is the file the
observer wrote, byte for byte: that is checked for every file, from the pack as it will be read, before the file is
removed, and a file that does not come back identical stays a file. ``screenshot`` reads one picture out of its block,
which is how the console keeps showing a compressed session.

The model-call log repeats those screenshots as base64 data URLs, each exactly what ``encode_screenshot`` derives
from a stored file. The log is kept as ``live_model_calls.jsonl.xz`` with every such URL replaced by a reference to
its file (``screenshot:<max side>:<SHA-256 of the file>``), and it too is checked to come back as it was before it is
removed; ``model_calls`` reads the rows back, references and all, since nothing reads the image bytes. ``restore`` writes the files back, puts the URLs back from them, and checks every
SHA-256: with another Pillow the bytes may differ (the pixels never do), and then it stops and keeps the pack.

``Packing`` compresses several sessions, or restores them, a few at a time, and says how far along they are. It works in threads of
the backend while a session may be running, so no step here holds the interpreter for long: xz, the PNG codec and
SHA-256 let other threads run, and the log is gone through a line at a time.
"""

from __future__ import annotations

import hashlib
import io
import json
import lzma
import os
import re
import threading
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import PIL
from PIL import Image

from ...errors import RunnerCoreError
from ..utils.images import encode_screenshot
from ..utils.jsonl import read_json, read_jsonl
from .paths import SessionPaths

PACK = "screenshots.pack"  # the blocks, one after another
INDEX = "screenshots.pack.json"  # where each file is in them; written last, so its presence says the session is compressed
CALLS = "live_model_calls.jsonl.xz"
BLOCK = 32  # screenshots to a block: one picture is read by unpacking its block alone
WORKERS = 3  # sessions compressed at once: a core and under a gigabyte of memory each
FILTERS = [{"id": lzma.FILTER_LZMA2, "preset": 6, "dict_size": 128 << 20}]  # a window most of a block wide, so a screen seen again costs little
DATA_URL = re.compile(rb"data:image/png;base64,[A-Za-z0-9+/=]+")
REFERENCE = re.compile(rb"screenshot:(\d+):([0-9a-f]{64})")
_UNPACKING = threading.Lock()


def compressed(paths: SessionPaths) -> bool:
    return (paths.root / INDEX).is_file()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _png(entry: dict[str, Any], pixels: bytes) -> bytes:
    """A packed screenshot's PNG file, written from its pixels the way the observer wrote it."""
    out = io.BytesIO()
    Image.frombytes(entry["mode"], tuple(entry["size"]), pixels[entry["offset"]:entry["offset"] + entry["length"]]).save(out, format="PNG")
    return out.getvalue()


def _write(path: Path, data: bytes) -> None:
    """A file written whole and onto the disk: what it stands in for is removed next."""
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


@lru_cache(maxsize=1)
def _block(pack: Path, offset: int, length: int) -> bytes:
    """A block's pixels; the last one read is kept, since a page of pictures asks for neighbours."""
    with pack.open("rb") as file:
        file.seek(offset)
        return lzma.decompress(file.read(length))


def screenshot(paths: SessionPaths, name: str) -> bytes | None:
    """A compressed session's screenshot as the PNG file it was; None when the pack has none by that name."""
    index = read_json(paths.root / INDEX) or {}
    entry = (index.get("files") or {}).get(name)
    if entry is None:
        return None
    block = index["blocks"][entry["block"]]
    with _UNPACKING:  # one at a time: a page of pictures asks for the same block many times at once
        pixels = _block(paths.root / PACK, block["offset"], block["length"])
    return _png(entry, pixels)


def screenshot_source(paths: SessionPaths, name: str) -> Any:
    """What ``Image.open`` reads a session's screenshot from: its file, or the picture out of the pack."""
    path = paths.screenshots / name
    packed = None if path.is_file() else screenshot(paths, name)
    return io.BytesIO(packed) if packed else path


def model_calls(paths: SessionPaths) -> list[dict[str, Any]]:
    """The session's logged model calls: those of the compressed log (their screenshots as references), then the
    file's (a decision recorded since logs its digest there)."""
    packed = paths.root / CALLS
    lines = lzma.decompress(packed.read_bytes()).decode().split("\n")[:-1] if packed.is_file() else []  # whole lines only, as read_jsonl
    return [json.loads(line) for line in lines if line.strip()] + read_jsonl(paths.model_calls)[0]


def packable(paths: SessionPaths) -> int:
    """The bytes ``compress`` reads of a session: its screenshots and its model-call log."""
    return sum(path.stat().st_size for path in paths.screenshots.glob("*.png")) + (paths.model_calls.stat().st_size if paths.model_calls.is_file() else 0)


def compress(paths: SessionPaths, *, dry_run: bool = False, progress: Callable[[int], None] = lambda read: None) -> dict[str, int]:
    """Pack the session's screenshots and its model-call log: ``{"before", "after"}`` in bytes, of what was packed.
    Nothing is removed before the pack gave every file back; a dry run writes nothing and removes nothing.
    ``progress`` is told the bytes read, a block at a time, ``packable`` in all."""
    if compressed(paths):
        raise RunnerCoreError("This session is compressed already.", code="invalid_request", status_code=409)
    shots = sorted(paths.screenshots.glob("*.png"))
    log = paths.model_calls.read_bytes() if paths.model_calls.is_file() else b""
    max_side = ((read_json(paths.session_json) or {}).get("settings") or {}).get("screenshotMaxSide", 1024)
    logged = b"data:image/png;base64," in log
    references: dict[bytes, bytes] = {}  # a screenshot's data URL as the log carries it → its reference
    index: dict[str, Any] = {"pillow": PIL.__version__, "files": {}, "blocks": [], "calls": None}
    pack = io.BytesIO()
    before = len(log)
    for start in range(0, len(shots), BLOCK):
        pixels = bytearray()
        read = 0
        for path in shots[start:start + BLOCK]:
            data = path.read_bytes()
            read += len(data)
            try:
                with Image.open(io.BytesIO(data)) as image:
                    entry = {"block": len(index["blocks"]), "offset": len(pixels), "size": list(image.size), "mode": image.mode, "sha256": _sha(data)}
                    raw = image.tobytes()
            except OSError:  # not a picture Pillow reads (cut short by a crash, say): it stays a file
                continue
            url = encode_screenshot(io.BytesIO(data), max_side) if logged else None
            if url:
                references[url.encode()] = f"screenshot:{max_side}:{entry['sha256']}".encode()
            entry["length"] = len(raw)
            if _png({**entry, "offset": 0}, raw) == data:  # comes back identical: packed; otherwise it stays a file
                index["files"][path.name] = entry
                pixels += raw
                before += len(data)
        stream = lzma.compress(pixels, format=lzma.FORMAT_XZ, filters=FILTERS)
        index["blocks"].append({"offset": pack.tell(), "length": len(stream)})
        pack.write(stream)
        progress(read)
    packed = pack.getvalue()
    for number, block in enumerate(index["blocks"]):  # the pack as it will be read gives every file back
        unpacked = lzma.decompress(packed[block["offset"]:block["offset"] + block["length"]])
        for name, entry in index["files"].items():
            if entry["block"] == number and _sha(_png(entry, unpacked)) != entry["sha256"]:
                raise RunnerCoreError(f"{name} did not come back from the pack as it was; nothing was changed.", code="pack_failed", status_code=500)
    lines = list(io.BytesIO(log))
    plain = any(REFERENCE.search(line) for line in lines)  # a log that already reads like a reference keeps its URLs
    xz = lzma.LZMACompressor(preset=6)
    calls = b"".join(xz.compress(line if plain else DATA_URL.sub(lambda found: references.get(found.group(), found.group()), line)) for line in lines) + xz.flush() if log else b""
    urls = {reference: url for url, reference in references.items()}
    back = hashlib.sha256()  # the log as a restore will write it, from what was just packed
    for line in io.BytesIO(lzma.decompress(calls)) if log else ():
        back.update(line if plain else REFERENCE.sub(lambda found: urls.get(found.group(), found.group()), line))
    if log:
        index["calls"] = {"sha256": _sha(log), "plain": plain}
        if back.hexdigest() != index["calls"]["sha256"]:
            raise RunnerCoreError("The model-call log did not come back as it was; nothing was changed.", code="pack_failed", status_code=500)
    listed = json.dumps(index).encode()
    after = len(packed) + len(listed) + len(calls)
    progress(len(log))
    if dry_run:
        return {"before": before, "after": after}
    _write(paths.root / PACK, packed)
    _write(paths.root / INDEX, listed)
    for name in index["files"]:
        (paths.screenshots / name).unlink()
    if log:
        _write(paths.root / CALLS, calls)
        with paths.model_calls.open("rb") as file:
            file.seek(len(log))
            later = file.read()  # rows logged while it was being packed (a decision on an old suggestion) stay in the file
        if later:
            _write(paths.model_calls, later)
        else:
            paths.model_calls.unlink()
    return {"before": before, "after": after}


def restorable(paths: SessionPaths) -> int:
    """The bytes ``restore`` reads of a compressed session: its pack and its packed model-call log."""
    return sum((paths.root / name).stat().st_size for name in (PACK, CALLS) if (paths.root / name).is_file())


def restore(paths: SessionPaths, *, progress: Callable[[int], None] = lambda read: None) -> None:
    """Write the session's screenshots and its model-call log back as they were, and remove the pack. Every file is
    checked against its SHA-256 first: on a difference (another Pillow than the one that packed it) it stops there,
    the pack kept. ``progress`` is told the bytes read, a block at a time, ``restorable`` in all."""
    index = read_json(paths.root / INDEX)
    if not index:
        raise RunnerCoreError("This session is not compressed.", code="invalid_request", status_code=409)

    def differs(what: str) -> RunnerCoreError:
        message = f"{what} does not come back as it was: packed with Pillow {index['pillow']}, this is Pillow {PIL.__version__}. The pack is kept."
        return RunnerCoreError(message, code="restore_mismatch", status_code=409)

    pack = (paths.root / PACK).read_bytes()
    for number, block in enumerate(index["blocks"]):
        pixels = lzma.decompress(pack[block["offset"]:block["offset"] + block["length"]])
        for name, entry in index["files"].items():
            if entry["block"] != number:
                continue
            png = _png(entry, pixels)
            if _sha(png) != entry["sha256"]:
                raise differs(name)
            _write(paths.screenshots / name, png)
        progress(block["length"])
    calls = paths.root / CALLS
    if calls.is_file():
        files = {_sha(path.read_bytes()): path for path in paths.screenshots.glob("*.png")}
        urls: dict[bytes, bytes] = {}

        def url(found: re.Match[bytes]) -> bytes:
            if found.group() not in urls:
                path = files.get(found.group(2).decode())
                urls[found.group()] = ((encode_screenshot(path, int(found.group(1))) if path else None) or "").encode() or found.group()
            return urls[found.group()]

        log = lzma.decompress(calls.read_bytes())
        if not index["calls"].get("plain"):
            log = REFERENCE.sub(url, log)
        if _sha(log) != index["calls"]["sha256"]:
            raise differs("The model-call log")
        later = paths.model_calls.read_bytes() if paths.model_calls.is_file() else b""  # the rows logged while it was compressed
        if not later.startswith(log):  # or the whole log already, when a backend stopped between writing it and removing its packed copy
            _write(paths.model_calls, log + later)
        progress(calls.stat().st_size)
        calls.unlink()
    (paths.root / PACK).unlink()
    (paths.root / INDEX).unlink()


class Packing:
    """Sessions being compressed, or restored, ``WORKERS`` at a time, and how far along they are in bytes read. The
    workers are daemon threads: a backend that stops leaves a session either as it was or compressed (the index is
    written before anything is removed and removed last, and what is a file is read as one)."""

    def __init__(self, sessions: list[SessionPaths], *, restoring: bool = False) -> None:
        self.restoring = restoring
        self.sessions = len(sessions)
        self.total = sum((restorable if restoring else packable)(paths) for paths in sessions)
        self.done = 0
        self.finished = 0
        self.failed: dict[str, str] = {}  # session id → why it was left as it was
        self._waiting = list(sessions)
        self._lock = threading.Lock()
        for _ in range(min(WORKERS, self.sessions)):
            threading.Thread(target=self._work, daemon=True).start()

    def _read(self, count: int) -> None:
        with self._lock:
            self.done += count

    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._waiting:
                    return
                paths = self._waiting.pop(0)
            try:
                (restore if self.restoring else compress)(paths, progress=self._read)
            except Exception as error:  # noqa: BLE001 - whatever stopped it is what the console shows for this session
                self.failed[paths.root.name] = str(error)
            with self._lock:
                self.finished += 1

    def wire(self) -> dict[str, Any]:
        return {"total": self.total, "done": self.done, "sessions": self.sessions, "finished": self.finished, "failed": self.failed, "running": self.finished < self.sessions}
