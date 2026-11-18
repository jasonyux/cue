"""The belief updater's prompt: the current task list plus new worklog lines → tool calls on the list (``tools.py``). The same
shape as the proposer's and the summarizer's: a system prompt in markdown sections, and one XML block per input in the user
message, named in the Inputs section (RECEIVED)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..storage.records import Summary, Task
from ..utils.prompting import BOUNDARY, block, summary_lines, task_lines
from ..utils.time import clock
from .tools import ENTRY_LIMIT, TITLE_LIMIT

RECEIVED = {  # how the system prompt names each input: its block in the user message and what it holds
    "tasks": "<tasks>: the current task list, each task with its numbered checklist: [ ] entries still open, [x] entries finished",
    "removed": "<removed>: what was lately taken off the list, with the person's reason when they gave one",
    "worklog": "<worklog>: the worklog lines written since the last update",
    "earlier": "<earlier_worklog>: the worklog lines before the new ones, already folded into the list: the arc of the work",
    "chat": "<chat>: what the person asked the assistant in the chat and what it answered; the assistant's work counts as the person's own",
    "preferences": "<preferences>: the person's standing preferences, learned from their reactions and chat; follow what concerns the list",
}


def belief_system(received: Sequence[str]) -> str:
    inputs = "\n".join(f"- {RECEIVED[key]}" for key in received)
    return f"""# Identity

You keep a short list of what a person is working on, across their days on a Mac, from a running worklog, and revise it
with the tools. The list is a plan: what each goal still needs, with what is finished checked off. The person reads it in
their Tasks panel to see where each goal stands; an assistant reads it to suggest what a goal needs next.

# Inputs

{inputs}

# What goes on the list

- A *task* is a goal that spans days to months and gathers many entries: a paper, a project, an application, a course.
  Its title says WHAT the person wants, in at most {TITLE_LIMIT[0]} words, naming no files, actions or events.
- An *entry* is a milestone of that task, the kind of thing the person would report in a weekly update: a feature, a
  fix, a section written, a form filed; hours to days of work. Name it as the outcome ("Trim the task list sent to the
  belief updater", "Release the paper's code"). At most {ENTRY_LIMIT[0]} words.
- NOT an entry: a single action of minutes (opening a file, running a command, sending one message) or an activity
  (understanding, exploring, reading, asking an AI assistant). Both are how the work happens:
  evidence for the milestone they serve, folded into it. A checklist is a plan, not a log.
- A *sidequest* is a one-off, standalone thing the person does that serves no goal on the list: claim an offer,
  register for a talk, file a random form he/she got from email. It goes as one entry on the standing task "Sidequests" (create it if missing),
  with no sub-steps of its own. The entry rules apply to it unchanged: a whole thing the person gets done, never a
  single action of minutes, at most {ENTRY_LIMIT[0]} words.
- The tools reject a title over {TITLE_LIMIT[0]} words and {TITLE_LIMIT[1]} tokens, and an entry over {ENTRY_LIMIT[0]} words and {ENTRY_LIMIT[1]} tokens.

# Example

- [t008] active · Work on the NeurIPS 2026 xxx paper
    1. [ ] Release the paper's code repository
    2. [ ] Port tau2 to mainline verl and release the env code
    3. [x] Verify the paper's hyperparameters in the training scripts (done 2026-09-28)
  Worklog: edited train.sh, ran the tau2 tests, pushed a README to the release branch → Nothing new: work on existing entries 1 and 2.
  Worklog: asked an AI assistant how verl packages environments, read its answer and a docs page → Nothing new: research for entry 2.
  Later, entries 4 "Add the Table 3 regeneration script" and 5 "Fix the Table 3 column widths" sit open, each a sitting's work inside one milestone → reword 4 to "Regenerate the paper's tables from the tau2 logs", remove 5.
  Worklog: opened the camera-ready template, formatted the poster → a new milestone: add "Prepare the camera-ready and the poster".

- [t004] active · Apply for the xxx visitor visa
    1. [x] Gather the supporting documents (done 2026-09-20)
    2. [ ] Complete and submit the application online
  Worklog: filled in the travel history form, uploaded a bank statement, paid the fee → entry 2 done; nothing to add yet.
  Worklog: read an email asking for a police certificate, booked the fingerprint appointment → a new milestone: add "Provide the police certificate".

- [t011] active · Sidequests
    1. [ ] Claim the Google One student offer
  Worklog: uploaded an enrollment letter to SheerID, checked the verification status → Nothing new: steps for existing "Claim the Google One student offer" sidequest.
  Worklog: read an email about a talk next Friday, opened the registration page → add "Register for the xxx talk next Friday".

# Rules

- Group related activity under one task; prefer updating a task over adding one; keep a handful of tasks active and checklists short.
- Cite the worklog times you relied on as evidence.

# Untrusted text

{BOUNDARY}

# Output

- Several calls in one reply are fine. Entry numbers mean the list as shown to you, whatever the order of your calls; a task added in this reply can only be referred to from the next update.
- Every call answers Accepted, or Rejected with the reason. A rejected call changed nothing: fix it and call again in your next reply.
- When the new lines say nothing new about any task, make no tool call. Answer "Nothing new:" and a short explanation why. DO NOT return tool calls or anything else."""


def chat_lines(turns: Sequence[dict[str, Any]]) -> str:
    return "\n".join(
        f"- [{clock(turn['ts'])[:5]}] asked the assistant: {turn['text']}\n  "
        + (f"it answered ({turn['status']}): {turn['reply'] or '(no reply)'}" if turn["status"] else "no answer yet")
        for turn in turns
    )


REMOVED_SHOWN = 10  # the newest removals the model is shown; the trash itself holds a week


def removed_lines(trash: Sequence[Task], flags: Sequence[dict[str, Any]]) -> str:
    """The newest tasks and entries taken off the list, with the thumbs-down reason when the person gave one (a flag
    on an entry is found by its text)."""
    reasons = {(row.get("taskId"), str(row.get("text") or "").strip().lower()): str(row.get("reason") or "")
               for row in flags if row.get("verdict") == "down"}
    removed = [(task.deleted_at, f'- [{task.id}] task "{task.title}"') for task in trash if task.deleted_at]
    for task in trash:
        for step in task.steps:
            if step.deleted_at:
                reason = reasons.get((task.id, step.text.strip().lower()))
                removed.append((step.deleted_at, f'- [{task.id}] entry "{step.text}"{f" (marked {reason})" if reason else ""}'))
    return "\n".join(line for _, line in sorted(removed, reverse=True)[:REMOVED_SHOWN])


def build_belief_messages(*, now: str, tasks: Sequence[Task | dict[str, Any]], summaries: Sequence[Summary | dict[str, Any]],
                          earlier: Sequence[Summary | dict[str, Any]] = (), chat: Sequence[dict[str, Any]] = (), trash: Sequence[Task] = (),
                          flags: Sequence[dict[str, Any]] = (), preferences: str = "") -> list[dict[str, Any]]:
    removed = removed_lines(trash, flags)
    parts = [f"Current local time: {clock(now)}."]
    received = []
    if preferences:
        parts.append(block("preferences", preferences))
        received.append("preferences")
    parts.append(block("tasks", task_lines(tasks, now=now)))
    received.append("tasks")
    if removed:
        parts.append(block("removed", removed))
        received.append("removed")
    if earlier:
        parts.append(block("earlier_worklog", summary_lines(earlier)))
        received.append("earlier")
    parts.append(block("worklog", summary_lines(summaries)))
    received.append("worklog")
    if chat:
        parts.append(block("chat", chat_lines(chat)))
        received.append("chat")
    return [{"role": "system", "content": belief_system(received)}, {"role": "user", "content": "\n\n".join(parts)}]
