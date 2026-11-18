"""The belief updater's tools: their schemas, and the check every call passes before it becomes an op.

A call is applied whole or rejected whole, and the model reads the result of each one. Rejected: an unknown tool,
malformed arguments, a task that is not on the list, or text over the length limit. A title passes when it fits in
TITLE_LIMIT words or TITLE_LIMIT tokens, an entry likewise within ENTRY_LIMIT: tokens catch the detail-stuffing that
few words can hide (hostnames, ids, paths), words leave room for a plain phrase of ordinary length. The rejection
says by how much the text is over, and the updater gives the model another reply to fix it (``update.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from typing import Any

import tiktoken

from ..storage.records import Task, TaskOp, normalize_op

TITLE_LIMIT = (7, 15)   # words, cl100k tokens: a title passes when it fits either
ENTRY_LIMIT = (10, 20)  # the same for a checklist entry
MAX_ROUNDS = 4          # one reply, then up to three more to fix rejected calls

TOOL_OPS = {"add_task": "add", "update_task_checklist": "update", "toggle_task_status": "status", "rename_task": "rename",
            "delete_task": "delete"}  # tools → ops; restoring from the trash is the operator's, not a tool
_EVIDENCE = {"type": "array", "items": {"type": "string"}, "description": "Worklog times relied on, e.g. \"21:14–21:15\"."}
_ENTRIES = {"type": "array", "description": f"Checklist entries, each a phrase of at most {ENTRY_LIMIT[0]} words; done is true when already finished.",
            "items": {"type": "object", "properties": {"text": {"type": "string"}, "done": {"type": "boolean"}}, "required": ["text"]}}
_REWORDS = {"type": "array", "description": f"Open entries to reword: the entry number and its new text, at most {ENTRY_LIMIT[0]} words.",
            "items": {"type": "object", "properties": {"step": {"type": "integer"}, "text": {"type": "string"}}, "required": ["step", "text"]}}
_TITLE = {"type": "string", "description": f"The task as the person would say it, at most {TITLE_LIMIT[0]} words."}
TASK_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "add_task", "description": "Add a task the person is pursuing that no existing task covers.",
        "parameters": {"type": "object", "properties": {
            "title": _TITLE,
            "steps": _ENTRIES,
            "evidence": _EVIDENCE}, "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "update_task_checklist", "description": "Add entries to a task's checklist, remove entries from it, or reword entries on it.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string", "description": "The id from the task list, e.g. t003."},
            "add": _ENTRIES,
            "remove": {"type": "array", "items": {"type": "integer"}, "description": "Entry numbers to remove."},
            "reword": _REWORDS,
            "evidence": _EVIDENCE}, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "toggle_task_status", "description": "Set an entry, or the whole task, done or not done.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string", "description": "The id from the task list."},
            "step": {"type": "integer", "description": "The entry number; leave out to set the whole task."},
            "done": {"type": "boolean"},
            "evidence": _EVIDENCE}, "required": ["task_id", "done"]}}},
    {"type": "function", "function": {
        "name": "rename_task", "description": "Give a task a sharper title.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string", "description": "The id from the task list."},
            "title": _TITLE,
            "evidence": _EVIDENCE}, "required": ["task_id", "title"]}}},
    {"type": "function", "function": {
        "name": "delete_task", "description": "Move a task that was tracked by mistake to the trash (recoverable for a few days).",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string", "description": "The id from the task list."},
            "evidence": _EVIDENCE}, "required": ["task_id"]}}},
]


@cache
def encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def too_long(kind: str, text: str, limit: tuple[int, int]) -> str | None:
    """Why ``text`` is over ``limit`` (words, tokens), or None when it fits either measure."""
    words, tokens = len(text.split()), len(encoding().encode(text))
    max_words, max_tokens = limit
    if words <= max_words or tokens <= max_tokens:
        return None
    return (f'{kind} "{text}" is {words} words / {tokens} tokens; the limit is {max_words} words or {max_tokens} tokens. '
            f"Shorten it by {words - max_words} words ({tokens - max_tokens} tokens).")


FILLER = "placeholder"  # what the model writes into a call it started and did not mean (a call cannot be retracted; seen twice from Sonnet 5.5)


def filler(kind: str, text: str) -> str | None:
    return f'{kind} is the word "{FILLER}": a call you do not mean is better left out.' if text.strip().lower() == FILLER else None


def taken(op: TaskOp, tasks: Sequence[Task], trash: Sequence[Task]) -> str | None:
    """Why the op would repeat what the list holds: everything on it is unique, open, done or in the trash, case aside.
    A title is checked against every other task (a rename may recase a task's own title), an entry against its task's
    entries."""
    held = [*tasks, *trash]
    wanted = (op.title or "").strip().lower()
    if wanted and op.op in ("add", "rename"):
        other = next((task for task in held if task.title.strip().lower() == wanted and task.id != op.task_id), None)
        if other:
            return f'There is already a task "{other.title}" ({other.id}{", in the trash" if other.deleted_at else ""}).'
    steps = [step for task in held if task.id == op.task_id for step in task.steps]
    for text, own in [(spec.text, 0) for spec in op.steps] + [(spec.text, spec.step) for spec in op.reword]:  # a reworded entry may keep its own text
        same = next((step for number, step in enumerate(steps, start=1) if number != own and step.text.strip().lower() == text.strip().lower()), None)
        if same and same.deleted_at:
            return f'"{text}" was removed from {op.task_id} (in its trash).'
        if same:
            return f'"{text}" is already on {op.task_id}{" (done)" if same.done_at else ""}.'
    return None


def asked(call: dict[str, Any]) -> TaskOp | None:
    """The op a call asks for, parsed and nothing more; None when the tool or the arguments are not understood."""
    op_name = TOOL_OPS.get(str(call.get("name")))
    return None if op_name is None else normalize_op({**call["arguments"], "op": op_name})


def check(call: dict[str, Any], tasks: list[Task], trash: Sequence[Task] = ()) -> tuple[TaskOp | None, str]:
    """One tool call → its op and the result the model reads, or no op and why the call was rejected. ``tasks`` is the
    list the model was shown: the one its task ids refer to; ``trash`` what it keeps out of sight (MemoryStore.trash)."""
    op_name = TOOL_OPS.get(str(call.get("name")))
    if op_name is None:
        return None, f"Rejected: there is no tool named {call.get('name')}."
    op = normalize_op({**call["arguments"], "op": op_name})
    if op is None:
        return None, "Rejected: a required argument is missing or malformed."
    if op.op != "add" and op.task_id not in {task.id for task in tasks}:
        return None, f"Rejected: there is no task {op.task_id} on the list."
    problems = [too_long("The title", op.title, TITLE_LIMIT), filler("The title", op.title)] if op.title else []
    texts = [spec.text for spec in op.steps] + [spec.text for spec in op.reword]
    problems += [problem for text in texts for problem in (too_long("The entry", text, ENTRY_LIMIT), filler("The entry", text))]
    steps = [step for task in tasks if task.id == op.task_id for step in task.steps]
    for spec in op.reword:
        if not 1 <= spec.step <= len(steps):
            problems.append(f"There is no entry {spec.step} on {op.task_id}.")
        elif steps[spec.step - 1].done_at:
            problems.append(f"Entry {spec.step} of {op.task_id} is done; only an open entry can be reworded.")
    problems.append(taken(op, tasks, trash))
    if any(problems):
        return None, "Rejected. " + " ".join(problem for problem in problems if problem)
    return op, "Accepted."
