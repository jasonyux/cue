"""Fakes shared by the live-session tests: a scripted model client, a scripted screen source, an observer stub."""

from PIL import Image

from app.live.config import LiveSettings, ModelEndpoint
from app.live.observer.sources import Snapshot, snapshot_hash
from app.live.session import LiveSessionManager
from app.live.utils.llm import ModelReply

TRUNCATED = "<truncated>"


class FakeClient:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls = []
        self.closed = False

    async def complete(self, messages, *, thinking, images=None, max_tokens=None, temperature=0.7, tools=None, tool_choice="auto"):
        self.calls.append({"messages": messages, "thinking": thinking, "images": len(images or []), "max_tokens": max_tokens,
                           "tools": [tool["function"]["name"] for tool in tools or []], "tool_choice": tool_choice})
        reply = self.replies.pop(0) if self.replies else '{"summary": "Nothing much happened.", "action": "none"}'
        # A scripted reply is text, or a list of (tool name, arguments) pairs for a stage that is given tools.
        tool_calls = ([{"id": f"call_{index}", "name": name, "arguments": arguments} for index, (name, arguments) in enumerate(reply)]
                      if isinstance(reply, list) else [])
        content = "" if isinstance(reply, list) else reply
        cut_off = content == TRUNCATED  # the model spent the whole budget reasoning and never wrote an answer
        if cut_off:
            content = ""
        return ModelReply(content=content, reasoning="thought about it" if thinking else "",
                          finish_reason="length" if cut_off else "stop", model="fake-model",
                          usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}, images=len(images or []),
                          request={"messages": messages, "thinking": thinking},
                          response={"choices": [{"message": {"content": content}, "finish_reason": "length" if cut_off else "stop"}]},
                          tool_calls=tool_calls)

    async def close(self):
        self.closed = True


class FakeSource:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.screenshots = 0
        self.again = []  # what each look was asked to look at again, None for the app in front

    def snapshot(self, again=None):
        self.again.append(again)
        return self.snapshots.pop(0) if self.snapshots else self.last

    @property
    def last(self):
        return Snapshot(ts="2026-09-17T10:00:59Z", app="Idle", hash="idle")

    def screenshot(self):
        self.screenshots += 1
        return Image.new("RGB", (8, 6))


class FakeObserver:
    def __init__(self):
        self.running = True

    def alive(self):
        return self.running

    async def stop(self):
        self.running = False
        return 0


def fake_settings(**changes):
    """Settings over fake endpoints; the desktop agent unless a test says otherwise (the real default is the claude agent)."""
    endpoint = ModelEndpoint(model="fake-model", base_url="http://fake/v1")
    return LiveSettings(summary=endpoint, belief=endpoint, proposer=endpoint, preference=endpoint, **{"actor_agents": ("desktop",), **changes})


def snap(ts, app, window, tree, **extra):
    return Snapshot(ts=ts, app=app, window=window, tree=tree, hash=snapshot_hash(app, window, tree), **extra)


def make_manager(tmp_path, replies=(), **settings):
    """A manager over fakes; ``settings`` are LiveSettings fields, taken as they are (no minimums, so a loop can tick fast)."""
    client = FakeClient(replies)
    observers = []

    async def spawn(paths, settings, stream):
        observer = FakeObserver()
        observers.append(observer)
        return observer

    manager = LiveSessionManager(data_root=tmp_path, settings=fake_settings(**settings),
                                 client_factory=lambda endpoint: client, observer_factory=spawn)
    return manager, client, observers
