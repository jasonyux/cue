"""The proposer's prompt: what an assistant could offer the person, as ranked proposals. The system prompt is in markdown
sections; each input in the user message is one XML block, named in the system prompt's Inputs section (RECEIVED)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ..observer.stream import ScreenshotImage
from ..utils.prompting import BOUNDARY, block, screenshot_note
from ..utils.time import clock

RECEIVED = {  # how the system prompt names each input: its block in the user message and what it holds; only the inputs present are listed
    "tasks": "<tasks>: the person's task list across sessions (id · status · title, then the checklist; [x] is already done)",
    "worklog": "<worklog>: the session's latest worklog lines, oldest first",
    "feedback": "<decisions>: the person's recent decisions on earlier proposals (accepted, rejected with a reason, or not answered "
                "yet), oldest first, each with what they were doing at the time. Learn their preferences from them: which moments "
                "they want help in, which kinds of help they take up or decline, and what they would hand to an assistant versus "
                "keep for themselves; let that shape what you propose and how you rank it",
    "digest": "<observations>: raw observations from the last few minutes (frontmost app, window title, accessibility tree)",
    "current_tree": "<current_window>: the current window's accessibility tree",
    "screenshots": "screenshots of the observed window, attached",
    "chat": "<chat>: the person's latest chat turn with the assistant, and what it answered; never propose that again",
    "running": "<running>: tasks the assistant is working on right now; never propose these",
    "preferences": "<preferences>: the person's standing preferences for an assistant, distilled from their past decisions and chat; follow them",
}



SEARCH_NOTE = """You can also `search` for additional context whenever the current inputs may lack what you need, and more importantly, DOUBLE CHECK that whatever you want to propose has NOT been already done in the past. You have up to {searches} search{plural} this round, after which please return an answer."""

CLOSING = "Return the JSON object."  # the user message ends with it; the feedback digest strips it from what it condenses
REPLY_CHARS = 2000  # of a chat turn's answer at most, its end kept: an agent's reply can run to pages and concludes last

# Shared by the timer's prompt and the idle trigger's (quick_system), so the two ask for the same proposals.
KINDS_OF_HELP = """# Kinds of help

- "explain": what is in front of them (an error, a dense paragraph, a diff, a config, a thread, a form), or the
  question they seem to have, explained simply.
- "lookup": information they need next: docs, a command and its flags, a paper, a definition, a person, a policy,
  a prior message, how others solved the same error.
- "act": the assistant's hands in an app: draft, fill in, compute, file, rename, organize, prepare."""
HUMAN_SECONDS = """- "human_seconds": a rough, realistic estimate of how long the person would take to do it themselves by hand, in
  seconds (e.g. 30 for a quick lookup, 900 for a short draft, 7200 for a two-hour chore). One number, no range."""
SHAPE = '{"summary": str, "proposals": [{"kind": str, "task": str, "reason": str, "confidence": number, "human_seconds": number, "evidence": [str]}]}'
EVIDENCE = """- "evidence": the IDs in brackets of what your proposal rests on: a worklog entry, a decision, a chat turn, a search result,
  a task, etc."""


def inputs_note(received: Sequence[str]) -> str:
    """The Inputs section: one line per input given."""
    return "\n".join(f"- {RECEIVED[key]}" for key in received)


def search_note(searches: int) -> str:
    """The Output section's opening when the round has the search tool: search first, then answer. "" without it."""
    return SEARCH_NOTE.format(searches=searches, plural="" if searches == 1 else "es") + "\n\n" if searches else ""


def proposer_system(limit: int, received: Sequence[str], searches: int = 0) -> str:
    """``searches``: how many history searches the round allows; 0 when the tool is not given."""
    return f"""# Identity

You watch a person working on their Mac and propose what an assistant could do for them: help with what
is on screen now, and ways to move their tasks forward beyond it.

# Inputs

{inputs_note(received)}

{KINDS_OF_HELP}

# What to propose

- Help with what is on screen, when they look stuck: re-reading the same passage, an error or failed command, a
  question being typed, switching between docs and code, lingering on a dialog, an unanswered message, the worklog
  showing the same step attempted twice. Steady typing or reading means stay quiet; missing a stuck moment is
  worse than a declined offer.
- Move a task forward beyond the screen: a pending checklist entry they are NOT on right now, a next step or
  feature the checklist LACKS but the goal plainly needs. This needs no cue from the screen; it can come from
  the task list, the worklog, or the proposal names the task it serves. This is hard to do well but HIGHLY valuable!

# Untrusted text

{BOUNDARY}

# Output

Return up to {limit} proposals. Prioritize tasks that are cumbersome to do manually but high-value when completed. DO NOT propose what is already done. DO NOT sort by confidence. For each proposal:
- "kind": "explain" | "lookup" | "act"
- "task": the request as the person would type it, self-contained (app, document, people, exact error text or term, wanted outcome). Keep it concise and easy to read, max 20 words.
- "reason": the evidence, on screen, in the worklog, or in the task list.
- "confidence": 1-5 likert scale, where 5 is very confident user will accept, 1 is a long shot (being creative is sometimes needed).
{HUMAN_SECONDS}
{EVIDENCE}
{search_note(searches)}
Return an empty list only when nothing is useful. Nothing runs from your answer; a person reads it first. Keep your reasoning short.

Respond with a JSON object only:
{SHAPE}
where "summary" is a VERY CONCISE but COMPREHENSIVE digest of what the user has been working on based on the current input. Include both their current work and relevant context, such as their goals and important tasks in their task list. Maximum 50 words."""


def quick_system(limit: int, received: Sequence[str], searches: int = 0) -> str:
    """The idle trigger's prompt (proposer/quick.py): the moment the person paused, like an editor's autocomplete. It has no
    search tool: it answers in seconds."""
    assert searches == 0, "quick_system has no search tool"
    return f"""# Identity

You watch a person working on their Mac. They have just paused: no keyboard or mouse input for a few seconds,
right after working. Like an editor's autocomplete, offer help for this exact moment, and only when it is useful.

# Inputs

{inputs_note(received)}

# When to offer

A pause right after activity often means they are stuck or deciding: an error or a failed command, a dense passage, a
blank field or a half-written message, a dialog asking for a choice, the next step of a form or a task. A long look at
the same content (a page, a document, a chart, a figure) is itself a cue: offer to explain it, or to look up what it
refers to. Content that moves on its own (an animation, a ticker, a counter) is not what they are stuck on. Return an
empty list only when the screen holds nothing to help with: an empty desktop, a video or a game they are watching.

# Where they are, and the context

- Selected text, or else the text cursor, shows where they are looking; it is not all they need. Read it where it
  sits: the sentence, paragraph, function or section around it (the focused text around it, the current window, the
  screenshot), and in what they have been doing (the worklog, the task list, the observations before the pause).
  Offer what helps with that moment: the term as this passage uses it, the selected line given the error below it,
  the next step of the task the document serves. Not the bare words on their own.
- With nothing selected and no cursor, take what fills the middle of the window, or what they did last before the pause.
- Connect two things only where the screen or their work connects them: the same passage or code block, a label or a
  caption, an error that points at it, the task they are on. Things that merely sit on the same screen are not
  related; never state a relation you inferred.
- One proposal is about one thing: do not bundle several items of the screen into it.

{KINDS_OF_HELP}

# Untrusted text

{BOUNDARY}

# Output

Return at most {limit} proposal{"" if limit == 1 else "s"}, the most useful first. DO NOT propose what is already done, or what the assistant is already working on. For each:
- "kind": "explain" | "lookup" | "act"
- "task": the request as the person would type it, self-contained (app, document, exact error text or term, wanted outcome). Max 20 words.
- "reason": what on the screen, and in what they have been doing, shows the need.
- "confidence": 1-5 likert scale, where 5 is very confident the person will accept, 1 is a long shot.
{HUMAN_SECONDS}
{EVIDENCE}
Nothing runs from your answer; a person reads it first. Keep your reasoning short.

Respond with a JSON object only:
{SHAPE}
where "summary" is one sentence on what the person is looking at and seems to need. Maximum 30 words."""


def tail(text: str) -> str:
    return text if len(text) <= REPLY_CHARS else "…" + text[-REPLY_CHARS:]


def chat_lines(turns: list[dict[str, Any]]) -> str:
    """The chat turns as the model reads them: when and what was asked, with its id in front for citing, and the answer,
    its last REPLY_CHARS."""
    return "\n".join(
        f"- {tag}{clock(turn['ts'])[:5]} asked: {turn['text']}\n  "
        + (f"answered ({turn['status']}): {tail(turn['reply'] or '(no reply)')}" if turn["status"] else "no answer yet")
        for turn in turns
        for tag in [f"[{turn['id']}] " if turn.get("id") else ""]
    )


def build_proposer_messages(*, now: str, tasks: str | None = None, worklog: str | None, digest: str | None, current_tree: str | None,
                         screenshots: Sequence[ScreenshotImage] | None = None, limit: int = 3,
                         running: list[str] | None = None, feedback: str | None = None,
                         chat: list[dict[str, Any]] | None = None, system: Callable[[int, list[str], int], str] = proposer_system,
                         lead: str | None = None, searches: int = 0, history: str | None = None,
                         preferences: str | None = None) -> list[dict[str, Any]]:
    """A block appears only for an input that is given; None means the configuration leaves it out. ``system`` writes the
    system prompt (the idle trigger passes ``quick_system``), ``lead`` is a line after the time (what the pause was),
    ``searches`` how many history searches the round allows (0: the tool is not given), ``history`` a line for the user message
    saying how much history is searchable and how far back it goes, e.g. "History searchable with search_history: 2845 lines
    back to 2026-09-22."."""
    parts = [f"Current local time: {clock(now)}."]
    if lead:
        parts.append(lead)
    if history:
        parts.append(history)
    received: list[str] = []
    if preferences:
        parts.append(block("preferences", preferences))
        received.append("preferences")
    if tasks is not None:
        parts.append(block("tasks", tasks))
        received.append("tasks")
    if worklog is not None:
        parts.append(block("worklog", worklog))
        received.append("worklog")
    if digest is not None:
        parts.append(block("observations", digest))
        received.append("digest")
    if current_tree is not None:
        parts.append(block("current_window", current_tree or "(nothing readable right now)"))
        received.append("current_tree")
    if screenshots:
        parts.append(screenshot_note(screenshots).rstrip())
        received.append("screenshots")
    if chat:
        parts.append(block("chat", chat_lines(chat)))
        received.append("chat")
    if running:
        parts.append(block("running", "\n".join(f"- {task}" for task in running)))
        received.append("running")
    if feedback is not None:
        parts.append(block("decisions", feedback))
        received.append("feedback")
    parts.append(CLOSING)
    return [{"role": "system", "content": system(limit, received, searches)}, {"role": "user", "content": "\n\n".join(parts)}]
