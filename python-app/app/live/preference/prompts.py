"""The preference updater's prompt: the file as it stands plus the person's new reactions → one write of the file, or
none. The same shape as the other stages': a system prompt in markdown sections, one XML block per input in the user
message, named in the Inputs section (RECEIVED)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..proposer.prompts import chat_lines
from ..storage.records import Decision, Summary, Task
from ..utils.prompting import BOUNDARY, block, decision_lines, summary_lines, task_lines
from ..utils.time import clock

RECEIVED = {  # how the system prompt names each input: its block in the user message and what it holds
    "preferences": "<preferences>: the file as it stands",
    "feedback": "<reactions>: the person's new reactions since the last update, oldest first: a proposal accepted or rejected (with "
                "their reason when they gave one; \"not now\" put it off), each with what they were doing at the time; a thumbs up or "
                "down on a task or an entry of their task list, with the reason when given",
    "earlier": "<earlier_decisions>: their decisions before these, already folded in",
    "tasks": "<tasks>: their task list",
    "worklog": "<worklog>: the latest worklog lines, oldest first",
    "chat": "<chat>: what they lately asked the assistant in the chat, and what it answered",
}
SEARCH_NOTE = (
    "You may call search_history up to {searches} time{plural} first: every worklog line, chat turn, proposal and decision "
    "from every session, weeks back. The chat is where the person says most about what they want."
)
CLOSING = "Call write_preferences with the whole file, or reply briefly with no call."


def preference_system(received: Sequence[str], *, cap: int, searches: int) -> str:
    inputs = "\n".join(f"- {RECEIVED[key]}" for key in received)
    search = "\n" + SEARCH_NOTE.format(searches=searches, plural="" if searches == 1 else "s") + "\n" if searches else ""
    return f"""# Identity

You keep a short Markdown file of a person's standing preferences for an assistant that watches them work on their Mac
and proposes help. The assistant reads the file before every proposal, and the agent that carries out an accepted
proposal reads it too.

# Inputs

{inputs}

# Rules

- A preference is a rule the assistant can act on, in the person's terms, with where it comes from in a few words:
  "No offers to draft emails; they write their own (declined 3 times, Oct 12–20)." Not a log of decisions, and not
  what is already on the task list.
- One reaction is a hint, not a rule, unless the person said so in a reason or in the chat. Several pointing the same
  way make a rule; later reactions that contradict a rule revise or remove it.
- To change the file, call write_preferences with the whole file, at most {cap} tokens; keep it general and drop the
  weakest rule before cramming. When nothing warrants a change, make no call and say so in a few words.
{search}
# Untrusted text

{BOUNDARY}"""


def build_preference_messages(
    *,
    now: str,
    preferences: str,
    feedback: str,
    earlier: Sequence[Decision],
    tasks: Sequence[Task],
    worklog: Sequence[Summary],
    chat: list[dict[str, Any]],
    cap: int,
    searches: int = 0,
    history: str | None = None,
) -> list[dict[str, Any]]:
    """A block appears only for an input that has something in it; the file and the reactions always."""
    parts = [f"Current local time: {clock(now)}."]
    if history:
        parts.append(history)
    received = ["preferences", "feedback"]
    parts += [block("preferences", preferences.strip() or "(empty)"), block("reactions", feedback)]
    for key, tag, text in (
        ("earlier", "earlier_decisions", decision_lines(earlier) if earlier else ""),
        ("tasks", "tasks", task_lines(tasks, now=now) if tasks else ""),
        ("worklog", "worklog", summary_lines(worklog) if worklog else ""),
        ("chat", "chat", chat_lines(chat) if chat else ""),
    ):
        if text:
            parts.append(block(tag, text))
            received.append(key)
    parts.append(CLOSING)
    return [{"role": "system", "content": preference_system(received, cap=cap, searches=searches)}, {"role": "user", "content": "\n\n".join(parts)}]
