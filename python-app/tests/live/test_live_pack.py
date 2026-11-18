"""A stored session compressed in place: its screenshots packed by their pixels, its model-call log with references
to them, both still readable, and everything back byte for byte on a restore."""

import asyncio
import hashlib
from unittest.mock import AsyncMock

import httpx
import pytest
from live_helpers import make_manager
from PIL import Image

from app.api import create_app
from app.errors import RunnerCoreError
from app.live.proposer.feedback_digest import input_images, proposer_request
from app.live.storage import pack
from app.live.storage.disk import disk_report
from app.live.storage.pack import (
    CALLS,
    INDEX,
    PACK,
    Packing,
    compress,
    compressed,
    model_calls,
    packable,
    restorable,
    restore,
    screenshot,
)
from app.live.storage.paths import session_paths
from app.live.utils.images import encode_screenshot
from app.live.utils.jsonl import append_jsonl, read_json, write_json
from app.runner import RunnerManager

SESSION = "20261001-100000"
STAY = ["broken.png", "odd.png"]  # what the pack does not take: these stay files


def a_session(data, session=SESSION):
    """Three screenshots as the observer writes them, two files that are not, and a log whose calls carry them."""
    paths = session_paths(data, session).create()
    write_json(paths.session_json, {"id": session, "settings": {"screenshotMaxSide": 48}})
    for number, color in enumerate(("navy", "navy", "teal")):
        image = Image.new("RGB", (96, 60), color)
        image.putpixel((number, number), (255, 255, 255))
        image.save(paths.screenshots / f"shot-{number}.png")
        append_jsonl(paths.snapshots, {"kind": "screenshot_taken", "ts": f"2026-10-01T10:0{number}:00Z", "app": "Numbers", "window": "Budget", "path": f"shot-{number}.png"})
    Image.new("RGB", (96, 60), "maroon").save(paths.screenshots / "odd.png", compress_level=1)  # not what a default save writes
    (paths.screenshots / "broken.png").write_bytes(b"cut short")  # not a picture at all
    for number in range(3):
        url = encode_screenshot(paths.screenshots / f"shot-{number}.png", 48)
        request = {"messages": [{"role": "user", "content": [{"type": "text", "text": f"window {number}"}, {"type": "image_url", "image_url": {"url": url}}]}]}
        append_jsonl(paths.model_calls, {"id": f"c{number}", "kind": "proposer", "windowStart": "2026-10-01T10:00:00Z", "windowEnd": f"2026-10-01T10:0{number}:30Z", "request": request, "response": {}})
    return paths


def files_of(paths):
    return {path.name: path.read_bytes() for path in [*paths.screenshots.iterdir(), paths.model_calls]}


async def finished(packing):
    while packing.wire()["running"]:
        await asyncio.sleep(0.01)
    return packing.wire()


def test_a_session_is_compressed_in_place_stays_readable_and_comes_back_byte_for_byte(tmp_path):
    paths = a_session(tmp_path / "data")
    before = files_of(paths)
    images = input_images(proposer_request(paths, "2026-10-01T10:02:30Z"), paths, 48)
    read = []
    sizes = compress(paths, dry_run=True, progress=read.append)
    assert files_of(paths) == before and not compressed(paths) and sizes["after"] < sizes["before"]  # a dry run only measures
    assert sum(read) == packable(paths) == sum(map(len, before.values()))  # told every byte it read
    assert compress(paths) == sizes
    assert compressed(paths) and sorted(path.name for path in paths.screenshots.iterdir()) == STAY and not paths.model_calls.exists()
    assert {(paths.root / name).is_file() for name in (PACK, INDEX, CALLS)} == {True}
    assert [screenshot(paths, f"shot-{number}.png") for number in range(3)] == [before[f"shot-{number}.png"] for number in range(3)]  # one picture, as the file it was
    assert screenshot(paths, "odd.png") is None and screenshot(paths, "nope.png") is None
    rows = model_calls(paths)
    assert [row["id"] for row in rows] == ["c0", "c1", "c2"]
    assert all(row["request"]["messages"][0]["content"][1]["image_url"]["url"].startswith("screenshot:48:") for row in rows)  # a reference to the file, not its bytes
    call = proposer_request(paths, "2026-10-01T10:02:30Z")
    assert call["id"] == "c2" and input_images(call, paths, 48) == images  # a later decision reads the same picture out of the pack
    with pytest.raises(RunnerCoreError):
        compress(paths)  # already
    append_jsonl(paths.model_calls, {"id": "c3", "kind": "proposer_feedback"})  # logged while compressed
    assert [row["id"] for row in model_calls(paths)] == ["c0", "c1", "c2", "c3"]
    later = paths.model_calls.read_bytes()
    restore(paths)
    assert files_of(paths) == {**before, paths.model_calls.name: before[paths.model_calls.name] + later}
    assert not compressed(paths) and not any((paths.root / name).exists() for name in (PACK, INDEX, CALLS))
    with pytest.raises(RunnerCoreError):
        restore(paths)  # nothing to restore


def test_a_row_logged_while_a_session_is_being_packed_stays_in_its_log(tmp_path):
    paths = a_session(tmp_path / "data")
    before = files_of(paths)

    def decided(read):  # a decision on an old suggestion logs its digest, once the log has been read
        if not paths.model_calls.read_bytes().endswith(b'"proposer_feedback"}\n'):
            append_jsonl(paths.model_calls, {"id": "c3", "kind": "proposer_feedback"})

    compress(paths, progress=decided)
    later = paths.model_calls.read_bytes()
    assert [row["id"] for row in model_calls(paths)] == ["c0", "c1", "c2", "c3"] and later.count(b"\n") == 1
    restore(paths)
    assert files_of(paths) == {**before, paths.model_calls.name: before[paths.model_calls.name] + later}


def test_a_log_that_already_reads_like_a_reference_keeps_its_urls_and_comes_back(tmp_path):
    paths = a_session(tmp_path / "data")
    sha = hashlib.sha256((paths.screenshots / "shot-0.png").read_bytes()).hexdigest()
    append_jsonl(paths.model_calls, {"id": "c3", "kind": "summary", "request": {"text": f"screenshot:48:{sha}"}})  # as a reference to a stored file reads
    before = files_of(paths)
    compress(paths)
    assert read_json(paths.root / INDEX)["calls"]["plain"] is True
    assert model_calls(paths)[0]["request"]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    restore(paths)
    assert files_of(paths) == before
    compress(paths)
    index = read_json(paths.root / INDEX)
    del index["calls"]["plain"]  # as the first packs were written: their URLs are references
    write_json(paths.root / INDEX, index)
    with pytest.raises(RunnerCoreError):
        restore(paths)  # the reference this log held itself is taken for one: caught by the SHA-256, the pack kept
    assert compressed(paths) and (paths.root / CALLS).is_file()


def test_a_log_left_whole_beside_its_packed_copy_by_a_backend_that_stopped_is_not_written_twice(tmp_path):
    paths = a_session(tmp_path / "data")
    before = files_of(paths)
    compress(paths)
    paths.model_calls.write_bytes(before[paths.model_calls.name])  # as between the packed copy's writing and the file's removal
    restore(paths)
    assert files_of(paths) == before


def test_a_pack_whose_index_does_not_say_plain_is_restored_from_its_references(tmp_path):
    paths = a_session(tmp_path / "data")
    before = files_of(paths)
    compress(paths)
    index = read_json(paths.root / INDEX)
    del index["calls"]["plain"]  # as the first packs were written
    write_json(paths.root / INDEX, index)
    restore(paths)
    assert files_of(paths) == before


def test_a_restore_that_would_not_give_a_file_back_as_it_was_stops_and_keeps_the_pack(tmp_path):
    paths = a_session(tmp_path / "data")
    compress(paths)
    index = read_json(paths.root / INDEX)
    index["files"]["shot-2.png"]["sha256"] = "0" * 64  # as if another Pillow wrote other bytes
    write_json(paths.root / INDEX, index)
    with pytest.raises(RunnerCoreError) as stopped:
        restore(paths)
    assert stopped.value.code == "restore_mismatch" and compressed(paths) and (paths.root / PACK).is_file() and (paths.root / CALLS).is_file()
    assert not (paths.screenshots / "shot-2.png").exists()


async def test_several_sessions_are_compressed_together_with_their_progress_and_one_that_fails_is_named(tmp_path, monkeypatch):
    sessions = [a_session(tmp_path / "data", f"2026100{day}-100000") for day in range(1, 6)]
    total = sum(packable(paths) for paths in sessions)
    whole = compress

    def one_fails(paths, **options):
        if paths.root.name == "20261003-100000":
            raise RunnerCoreError("no room", code="pack_failed", status_code=500)
        return whole(paths, **options)

    monkeypatch.setattr(pack, "compress", one_fails)
    packing = Packing(sessions)
    assert packing.wire()["sessions"] == 5 and packing.wire()["total"] == total
    done = await finished(packing)
    assert done == {"total": total, "done": total - packable(sessions[2]), "sessions": 5, "finished": 5, "failed": {"20261003-100000": "no room"}, "running": False}
    assert [compressed(paths) for paths in sessions] == [True, True, False, True, True]
    back = [paths for paths in sessions if compressed(paths)]
    total = sum(restorable(paths) for paths in back)
    assert await finished(Packing(back, restoring=True)) == {"total": total, "done": total, "sessions": 4, "finished": 4, "failed": {}, "running": False}
    assert not any(compressed(paths) for paths in sessions) and files_of(sessions[0]) == files_of(sessions[2])  # as the one never compressed


async def test_the_console_compresses_and_restores_stored_sessions_and_still_shows_their_pictures(tmp_path):
    data = tmp_path / "data"
    paths, other = a_session(data), a_session(data, "20261002-100000")
    before = files_of(paths)
    total = packable(paths) + packable(other)
    live, _client, _ = make_manager(data)
    app = create_app(manager=RunnerManager(data, executor=AsyncMock(), scenarios=[]), live=live, acquire_lease=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        assert (await http.get("/api/live/disk")).json()["packing"] is None
        assert (await http.post("/api/live/disk/compress", json={"ids": []})).status_code == 400
        assert (await http.post("/api/live/disk/compress", json={"ids": ["20990101-000000"]})).status_code == 404
        started = await http.post("/api/live/disk/compress", json={"ids": [SESSION, "20261002-100000"]})
        assert started.status_code == 202 and started.json()["packing"]["sessions"] == 2
        await finished(live.packing)
        packed = (await http.get("/api/live/disk")).json()
        assert packed["packing"] == {"total": total, "done": total, "sessions": 2, "finished": 2, "failed": {}, "running": False}
        row = next(item for item in packed["sessions"] if item["id"] == SESSION)
        assert row["compressed"] is True and row == next(item for item in disk_report(data)["sessions"] if item["id"] == SESSION)
        assert row["screenshots"] == sum((paths.root / name).stat().st_size for name in (PACK, INDEX)) + sum(len(before[name]) for name in STAY)  # the pack counts as screenshots
        assert row["modelCalls"] == (paths.root / CALLS).stat().st_size
        shown = await http.get(f"/api/live/sessions/{SESSION}/screenshots/shot-1.png")
        assert shown.status_code == 200 and shown.content == before["shot-1.png"]  # out of the pack
        assert (await http.get(f"/api/live/sessions/{SESSION}/screenshots/odd.png")).content == before["odd.png"]  # still a file
        assert (await http.get(f"/api/live/sessions/{SESSION}/screenshots/nope.png")).status_code == 404
        assert (await http.post("/api/live/disk/compress", json={"ids": [SESSION]})).status_code == 409  # already
        assert (await http.post("/api/live/disk/restore", json={"ids": [SESSION]})).status_code == 202
        await finished(live.packing)
        restored = (await http.get("/api/live/disk")).json()
        assert restored["packing"]["failed"] == {} and next(item for item in restored["sessions"] if item["id"] == SESSION)["compressed"] is False
        assert files_of(paths) == before
        assert (await http.post("/api/live/disk/restore", json={"ids": [SESSION]})).status_code == 409  # not compressed
        assert (await http.post("/api/live/disk/delete", json={"ids": [SESSION]})).status_code == 400  # compress or restore
