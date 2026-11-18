"""The history index: what of the stream it indexes, how it searches, and how it keeps up."""

import json
import time

import pytest

from app.live.actor.standalone import history_server
from app.live.storage.records import Summary
from app.live.storage.search import GROUPS, TOOL, VERSION, HistoryIndex, bound, render, run_tool
from app.live.storage.stream import Stream


@pytest.fixture
def zone(monkeypatch):
    """Sets the local time zone for a test; the machine's own is back afterwards."""
    def set_zone(name):
        monkeypatch.setenv("TZ", name)
        time.tzset()
    yield set_zone
    monkeypatch.undo()
    time.tzset()


def worklog(stream, ts, text, session="old"):
    stream.emit("summary_written", Summary(ts=ts, window_start=ts, window_end=ts, summary=text).wire(), ts=ts, session_id=session)


def test_the_index_searches_worklog_chat_proposals_and_decisions_by_stem_and_time(tmp_path, zone):
    zone("UTC")
    stream = Stream(tmp_path / "stream")
    worklog(stream, "2026-09-10T09:00:00Z", "Booked the Sydney hotel for NeurIPS, Dec 2 to 6, on booking.com.")
    worklog(stream, "2026-09-10T09:01:00Z", "Read the Paris workshop's call for papers.")
    worklog(stream, "2026-10-19T15:00:00Z", "Looking at flights to Sydney on Google Flights.", session="new")
    stream.emit("chat_message", {"ts": "2026-10-19T15:02:00Z", "runId": "c1", "text": "Compare the Qantas fares"}, ts="2026-10-19T15:02:00Z", session_id="new")
    stream.emit("proposals_made", {"ts": "2026-10-19T15:05:00Z", "summary": "Travel.", "proposals": [{"kind": "act", "task": "Book the Sydney flight"}]},
                ts="2026-10-19T15:05:00Z", session_id="new")
    stream.emit("proposals_made", {"ts": "2026-10-19T15:10:00Z", "summary": "Travel.", "proposals": [{"kind": "act", "task": "Book the Sydney flight"}]},
                ts="2026-10-19T15:10:00Z", session_id="new")  # the same proposal again: one result
    stream.emit("proposal_rejected", {"ts": "2026-10-19T15:11:00Z", "proposalsTs": "2026-10-19T15:10:00Z", "decision": "reject", "task": "Book the Sydney flight"},
                ts="2026-10-19T15:11:00Z", session_id="new")
    stream.emit("reject_reason_given", {"decisionTs": "2026-10-19T15:11:00Z", "reason": "not now", "typed": False}, ts="2026-10-19T15:11:30Z", session_id="new")
    stream.emit("reject_reason_given", {"decisionTs": "2026-10-19T15:11:00Z", "reason": "I fly from Melbourne, not Sydney", "typed": True},
                ts="2026-10-19T15:12:00Z", session_id="new")
    stream.emit("screen_changed", {"ts": "2026-10-19T15:13:00Z", "app": "Safari", "window": "Sydney hotels", "tree": "x"}, ts="2026-10-19T15:13:00Z", session_id="new")
    index = HistoryIndex(tmp_path / "memory" / "search.sqlite", stream)
    assert index.catch_up() == 8 and index.span() == (8, "2026-09-10")  # the screen row and the preset reason are not indexed
    hits = index.search("neurips hotels")  # by stem: "hotels" finds "hotel"
    assert [hit.text[:16] for hit in hits] == ["Booked the Sydne"] and hits[0].kind == "summary_written" and hits[0].session_id == "old"
    assert render(hits).startswith(f"[{hits[0].id}] [2026-09-10 ") and len(hits[0].id) == 12 and "worklog: Booked the Sydney hotel" in render(hits)
    assert sorted(hit.text[:9] for hit in index.search("sydney")) == ["Booked th", "Looking a"]  # the worklog and the chat unless kinds says otherwise
    sydney = index.search("sydney", kinds=list(GROUPS))
    assert len(sydney) == 5  # the hotel, the flights, the proposal once, its decision, the typed reason; the order is bm25's
    assert sum(hit.kind == "proposals_made" for hit in sydney) == 1 and any("Melbourne" in hit.text for hit in sydney)
    assert [hit.ts[:10] for hit in index.search("sydney", since="2026-10-01")] and all(hit.ts >= "2026-10-01" for hit in index.search("sydney", since="2026-10-01"))
    assert [hit.text[:10] for hit in index.search("sydney", until="2026-09-10")] == ["Booked the"]  # a date alone spans its day
    assert [hit.text[:9] for hit in index.search("", since="2026-09-10", until="2026-09-10")] == ["Read the ", "Booked th"]  # no words: the slice, newest first
    assert [hit.text[:9] for hit in index.search("paris qantas")] == ["Compare t", "Read the "]  # nothing has both words: any of them
    assert index.search("zanzibar") == [] and render([]) == "(nothing found)"
    assert len(index.search("sydney", limit=2)) == 2
    assert [hit.text[:9] for hit in index.search("neur*")] == ["Booked th"] and index.search("hotl*") == []  # a word ending in *: a prefix
    assert sorted(hit.text[:9] for hit in index.search("sydney", kinds=["worklog"])) == ["Booked th", "Looking a"]  # only some kinds
    assert {hit.kind for hit in index.search("sydney", kinds=["decision"])} == {"proposal_rejected", "reject_reason_given"}
    flights = index.search("", regex="fl(ight|y)", kinds=list(GROUPS))  # a regex, case-insensitive, over the slice: newest first, a text once per kind
    assert [hit.text[:9] for hit in flights] == ["I fly fro", "Book the ", "Book the ", "Looking a"] and len({hit.kind for hit in flights}) == 4
    assert [hit.text[:9] for hit in index.search("sydney", regex="HOTEL")] == ["Booked th"]  # with keywords: ranked among their matches
    zone("Australia/Sydney")  # bounds are local time, as the results show it: UTC+11 in mid-October
    assert bound("2026-10-14", end=False) == "2026-10-13T13:00:00.000Z" and bound("2026-10-14", end=True) == "2026-10-14T12:59:59.999Z"
    assert bound(" 2026-10-14T09:00 ", end=True) == "2026-10-13T22:00:00.000Z" and bound("2026-10-14T09:00Z", end=True) == "2026-10-14T09:00:00.000Z"
    assert bound("", end=True) is None and run_tool(index, {"query": "sydney", "since": "yesterday"})[0] == "Rejected: 'yesterday' is not an ISO date or timestamp."
    assert render(index.search("neurips"))[15:34] == "[2026-09-10 19:00] " and index.span()[1] == "2026-09-10"  # the hotel line, 09:00Z, shown in local time (UTC+10 before October)
    zone("UTC")
    # The tool over it, as a stage or an agent calls it: the model's arguments in, the text and a note of the search out.
    text, note = run_tool(index, {"query": "neurips hotels", "limit": "3"})
    assert text.startswith(f"[{hits[0].id}] [2026-09-10 ")
    assert note == {"query": "neurips hotels", "regex": None, "kinds": ["worklog", "chat"], "since": None, "until": None, "hits": 1, "lines": [text], "found": [hits[0].wire()]}
    assert note["found"][0]["id"] == hits[0].id
    assert run_tool(index, None)[0] == render(index.search("")) and run_tool(index, None)[1]["hits"] == 4 and len(run_tool(index, None)[1]["found"]) == 4
    assert run_tool(index, {"query": "sydney", "kinds": "worklog"})[1]["hits"] == 2  # one kind, as a string
    assert {hit.kind for hit in index.search("sydney", kinds=["worklog", "decision"])} == {"summary_written", "proposal_rejected", "reject_reason_given"}
    assert run_tool(index, {"query": "x", "kinds": ["worklogs"]})[0] == "Rejected: no such kind worklogs; the kinds are worklog, chat, proposal, decision."
    text, note = run_tool(index, {"regex": "("})
    assert text.startswith("Rejected: the regex does not compile") and note["hits"] == 0 and note["found"] == [] and note["error"].startswith("the regex")
    assert TOOL["function"]["name"] == "search_history" and set(TOOL["function"]["parameters"]["properties"]) == {"query", "regex", "kinds", "since", "until", "limit"}
    index.close()


def test_the_index_keeps_up_with_the_stream_and_rebuilds_when_its_version_changes(tmp_path, monkeypatch):
    stream = Stream(tmp_path / "stream")
    worklog(stream, "2026-10-19T15:00:00Z", "Wrote the abstract.")
    path = tmp_path / "memory" / "search.sqlite"
    index = HistoryIndex(path, stream)
    assert index.search("abstract") and index.cursor().part == "2026-10-19"
    worklog(stream, "2026-10-19T15:01:00Z", "Fixed the figure's caption.")
    part = stream.parts()[-1]
    with part.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": "0123456789ab", "ts": "2026-10-19T15:02:00Z", "kind": "summary_written", "sessionId": "s",
                                 "callId": "", "data": {"summary": "Half written"}})[:-20])  # a line still being written
    assert index.catch_up() == 1 and [hit.text for hit in index.search("caption")] == ["Fixed the figure's caption."]
    assert index.search("half") == [] and index.cursor().offset < part.stat().st_size  # not consumed: read whole when it lands
    index.close()
    again = HistoryIndex(path, stream)  # the same file: the cursor survives, nothing is indexed twice
    assert again.catch_up() == 0 and again.span()[0] == 2
    again.close()
    monkeypatch.setattr("app.live.storage.search.VERSION", VERSION + 1)
    rebuilt = HistoryIndex(path, stream)
    assert rebuilt.cursor().part == "" and rebuilt.span()[0] == 2 and rebuilt.search("abstract")
    rebuilt.close()


def test_the_history_mcp_server_lists_and_runs_the_search_tool_over_the_backends_index(tmp_path, monkeypatch):
    monkeypatch.setenv("CUA_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("CUA_HISTORY_BEFORE", "2026-10-19T15:01:30Z")  # the run's start
    stream = Stream(tmp_path / "memory" / "stream")
    worklog(stream, "2026-10-19T15:00:00Z", "Booked the Sydney hotel.")
    history = history_server.index()
    assert history.path == tmp_path / "memory" / "search.sqlite"
    listed = history_server.answer(history, {"method": "tools/list"})["result"]["tools"]
    assert [tool["name"] for tool in listed] == ["search_history"] and listed[0]["inputSchema"]["required"] == [] and "weeks back" in listed[0]["description"]
    found = history_server.answer(history, {"method": "tools/call", "params": {"name": "search_history", "arguments": {"query": "sydney"}}})["result"]
    assert found["content"][0]["text"].endswith("worklog: Booked the Sydney hotel.") and "isError" not in found
    assert history_server.answer(history, {"method": "tools/call", "params": {"name": "exec_py", "arguments": {}}})["result"]["isError"] is True
    assert history_server.answer(history, {"method": "initialize", "params": {"protocolVersion": "2025-06-18"}})["result"]["serverInfo"]["name"] == "computerpilot-history"
    assert history_server.answer(history, {"method": "ping"}) == {"result": {}} and history_server.answer(history, {"method": "resources/list"})["error"]["code"] == -32601
    other = HistoryIndex(history.path, stream)  # the backend's own instance over the same file: both catch up, nothing indexed twice
    worklog(stream, "2026-10-19T15:01:00Z", "Paid the deposit.")
    assert other.catch_up() == 1 and history.catch_up() == 0 and len(history.search("")) == 2
    stream.emit("chat_message", {"ts": "2026-10-19T15:01:30Z", "runId": "r1", "text": "Pay the hotel"}, ts="2026-10-19T15:01:30Z", session_id="s")  # the run's own turn
    assert len(history.search("")) == 3 and len(history.search("", before="2026-10-19T15:01:30Z")) == 2
    assert "Pay the hotel" not in history_server.answer(history, {"method": "tools/call", "params": {"name": "search_history", "arguments": {}}})["result"]["content"][0]["text"]
    other.close()
    history.close()
