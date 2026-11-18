"""The disk panel's numbers: the data folder's bytes, each session by what fills it and the rest by what it is."""

from unittest.mock import AsyncMock

import httpx
from live_helpers import make_manager

from app.api import create_app
from app.live.storage.disk import disk_report
from app.live.storage.paths import session_paths
from app.live.utils.jsonl import write_json
from app.runner import RunnerManager


def put(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


async def test_the_disk_report_splits_each_session_and_the_rest_of_the_data_folder_by_kind(tmp_path):
    data = tmp_path / "data"
    old = session_paths(data, "20261001-100000").create()
    new = session_paths(data, "20261101-100000").create()
    write_json(old.session_json, {"id": "20261001-100000", "name": "Deck week"})
    write_json(new.session_json, {"id": "20261101-100000"})
    put(data / "sessions" / ".claude" / "notes.md", 7)  # a folder there that is no session
    put(old.screenshots / "a.png", 3000)
    put(old.screenshots / "b.png", 2000)
    put(old.model_calls, 700)
    put(old.runs_dir / "run-1" / "frames" / "1.png", 400)  # a run's files, however deep
    put(old.snapshots, 300)
    put(old.summaries, 50)
    put(new.screenshots / "c.png", 1000)
    put(data / "memory" / "stream" / "2026-10-01.jsonl", 900)
    put(data / "agent-browser" / "Default" / "Cookies", 600)
    put(data / "runs" / "r1" / "run.json", 80)
    put(data / "workspaces" / "w1" / "notes.md", 20)
    report = disk_report(data)
    assert report["root"] == str(data) and report["free"] > 0
    assert report["sessions"] == [
        {"id": "20261001-100000", "name": "Deck week", "compressed": False, "screenshots": 5000, "modelCalls": 700, "runs": 400, "screenReads": 300, "other": 50 + old.session_json.stat().st_size},
        {"id": "20261101-100000", "name": None, "compressed": False, "screenshots": 1000, "modelCalls": 0, "runs": 0, "screenReads": 0, "other": new.session_json.stat().st_size},
    ]
    assert report["rest"] == {"memory": 900, "agentBrowser": 600, "other": 107}  # runs and workspaces from outside a session, and the stray folder among them
    live, _client, _ = make_manager(data)
    app = create_app(manager=RunnerManager(data, executor=AsyncMock(), scenarios=[]), live=live, acquire_lease=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        served = (await http.get("/api/live/disk")).json()
        assert served["sessions"] == report["sessions"] and served["rest"]["agentBrowser"] == 600  # the same report over the route
