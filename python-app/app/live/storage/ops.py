"""Task ops on a list, one function for everyone who applies them: the store, on the list it keeps (memory.py); a reader
rebuilding the list at a moment from the last one carried and the ops after it (stream.py's Window.tasks, sessions.py's
tasks_left); and the belief updater, replaying its own ops to learn whether its row must carry the list (belief/update.py).
So a replay gives the store's list op for op, and a row carries the list only when its ops would not."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import uuid4

from .records import Step, Task, TaskOp


def new_task_id() -> str:
    """A new task's id: random, so it is never one a task had before (a task purged from the trash keeps its id in
    flags and events), and short, since the task model reads every id and copies it into its calls. Tasks from before
    keep their numbered ids (t001, …)."""
    return f"t{uuid4().hex[:8]}"


def visible(tasks: list[Task]) -> list[Task]:
    """Tasks not in the trash, each without its trashed entries."""
    return [task.model_copy(update={"steps": [s for s in task.steps if not s.deleted_at]}) for task in tasks if not task.deleted_at]


def play(tasks: list[Task], ops: list[TaskOp], *, now: str, session_id: str, call_id: str) -> list[TaskOp]:
    """Apply ``ops`` to ``tasks`` in place and return the ones that applied. Ops other than restore act on the visible
    list, and entry numbers mean the list as shown when the calls were made. An add's op is given the new task's id, so
    the log and a replay name the same task. Afterwards each task's entries stand pending first in the order added,
    finished after, newest first, trashed last: the order the prompt and the console show, and the numbers refer to."""
    stored = {task.id: task for task in tasks}
    by_id = {task.id: task for task in tasks if not task.deleted_at}   # ops other than restore act on the visible list
    shown = {task.id: [s for s in task.steps if not s.deleted_at] for task in tasks}  # entry numbers mean the list as shown
    binned = {task.id: [s for s in task.steps if s.deleted_at] for task in tasks}     # restore numbers mean the trash as shown
    applied: list[TaskOp] = []
    for op in ops:
        if op.op == "add":
            task = Task(id=op.task_id or new_task_id(), title=op.title or "", last_seen=now, session_id=session_id, call_id=call_id,
                        evidence=op.evidence, steps=[Step(text=s.text, done_at=now if s.done else None, call_id=call_id, added_at=now) for s in op.steps])
            op.task_id = task.id  # the log and a replay name the task the add made
            tasks.append(task)
            stored[task.id] = by_id[task.id] = task
        elif op.op == "restore":
            target = stored.get(op.task_id or "")
            if target is None:
                continue
            if op.step is None:
                if not target.deleted_at:
                    continue
                target.deleted_at = None
            elif 1 <= op.step <= len(binned.get(target.id, [])):
                binned[target.id][op.step - 1].deleted_at = None
            else:
                continue
        else:
            existing = by_id.get(op.task_id or "")
            if existing is None:
                continue
            listed = shown.get(existing.id, [])
            if op.op == "delete":
                existing.deleted_at = now
            elif op.op == "update":
                for number in op.remove:
                    if 1 <= number <= len(listed):
                        listed[number - 1].deleted_at = now
                for change in op.reword:
                    if 1 <= change.step <= len(listed) and not listed[change.step - 1].done_at:  # an open entry only
                        listed[change.step - 1].text = change.text
                for spec in op.steps:
                    if not any(step.text.lower() == spec.text.lower() for step in existing.steps):  # trashed ones included: restore instead
                        existing.steps.append(Step(text=spec.text, done_at=now if spec.done else None, call_id=call_id, added_at=now))
            elif op.op == "rename":
                existing.title = op.title or existing.title
            elif op.step is None:
                existing.status = "done" if op.done else "active"
            elif 1 <= op.step <= len(listed):
                listed[op.step - 1].done_at = now if op.done else None  # a no-op if that entry was trashed in this batch
            if existing.status == "paused":
                existing.status = "active"  # touched again
            existing.last_seen, existing.session_id = now, session_id
            existing.evidence = (existing.evidence + op.evidence)[-5:]
        applied.append(op)
    for task in tasks:
        live = [s for s in task.steps if not s.deleted_at]
        task.steps = ([s for s in live if not s.done_at] + sorted((s for s in live if s.done_at), key=lambda s: s.done_at or "", reverse=True)
                      + [s for s in task.steps if s.deleted_at])
    return applied


def replay(tasks: list[Task], rows: Iterable[tuple[str, str, str, list[TaskOp]]]) -> list[Task]:
    """The visible list after each row's ops are played on ``tasks`` in turn; a row is (ts, session id, call id, ops). It
    is how a reader rebuilds the list from the last one carried, and what a belief row's ops must reproduce for the row
    to go without the list: a rule firing in between, an operator edit, an add doubling a trashed entry, are why the
    next row carries it again."""
    for now, session_id, call_id, ops in rows:
        play(tasks, ops, now=now, session_id=session_id, call_id=call_id)
        tasks = visible(tasks)
    return visible(tasks)
