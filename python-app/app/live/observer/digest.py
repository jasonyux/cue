"""Pure text builders: a batch of snapshots → the digest a model reads."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..permissions import Reason
from ..storage.records import Snapshot, as_snapshots
from ..utils.time import clock, seconds_between

EDITABLE_ROLES = ("AXTextArea", "AXTextField", "AXSearchField", "AXComboBox")
NEAR_LINES = 15  # excerpt_tree with ``near``: the lines kept on each side of a line holding it


def compact_tree(tree: str, max_chars: int = 1500) -> str:
    """Keep the header and as many element lines as fit; note how many were dropped."""
    lines = tree.splitlines()
    if not lines:
        return ""
    kept: list[str] = [lines[0]]
    used = len(lines[0])
    for index, line in enumerate(lines[1:], start=1):
        if used + len(line) + 1 > max_chars:
            kept.append(f"… (+{len(lines) - index} more lines)")
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


def line_priority(line: str, previous: set[str] | None) -> int | None:
    """0: what the user is typing or has focused; 1: changed since the previous snapshot; 2: everything else."""
    if any(role in line for role in EDITABLE_ROLES) and "value=" in line:
        return 0
    if " focused" in line:
        return 0
    if previous is None:
        return 2
    return 1 if line not in previous else None


def near_lines(body: Sequence[str], near: str) -> set[int]:
    """The indices of the lines within NEAR_LINES of a line holding ``near`` (its first words, case and spacing aside)."""
    words = " ".join(near.split()).lower()
    for key in (words[:40], " ".join(words.split()[:3])):  # a selection longer than an element's text: its first words
        hits = [index for index, line in enumerate(body) if key and key in " ".join(line.split()).lower()]
        if hits:
            return {index for hit in hits for index in range(hit - NEAR_LINES, hit + NEAR_LINES + 1)}
    return set()


def excerpt_tree(tree: str, previous: str | None = None, max_chars: int = 1500, near: str | None = None) -> str:
    """The lines of a tree worth the model's attention, in tree order, within a character budget.

    Typed text and focused elements always come first, then lines that changed since ``previous`` (the last
    snapshot of the same window), then the top of the tree. Head-only truncation hid exactly the lines that
    matter most, which tend to sit near the bottom (message composers, terminals, reply boxes). With ``near`` (the
    selected text), the lines around it come before all of them: what the person marked, and what surrounds it.
    """
    lines = tree.splitlines()
    if not lines:
        return ""
    header, body = lines[0], lines[1:]
    previous_lines = set(previous.splitlines()[1:]) if previous else None
    around = near_lines(body, near) if near else set()
    candidates = []
    for index, line in enumerate(body):
        priority = -1 if index in around else line_priority(line, previous_lines)
        if priority is not None:
            candidates.append((priority, index, line))
    candidates.sort()
    budget = max_chars - len(header) - 1
    chosen: list[tuple[int, str]] = []
    dropped = 0
    for _, index, line in candidates:
        if len(line) + 1 <= budget:
            chosen.append((index, line))
            budget -= len(line) + 1
        else:
            dropped += 1
    chosen.sort()
    out = [header, *(line for _, line in chosen)]
    unchanged = len(body) - len(candidates)
    notes = []
    if dropped:
        notes.append(f"+{dropped} more lines")
    if previous_lines is not None and unchanged:
        notes.append(f"{unchanged} lines unchanged since the previous snapshot")
    if notes:
        out.append("… (" + "; ".join(notes) + ")")
    return "\n".join(out)


def build_window_digest(records: Sequence[Snapshot | dict[str, Any]], *, max_chars: int = 12_000,
                        tree_chars: int = 1500) -> str:
    """Chronological account of the observed window: app and window switches, dwell times, and tree excerpts."""
    entries: list[tuple[Snapshot, str]] = []  # (record, last ts it was still current)
    for record in as_snapshots(records):
        if record.kind == "screen_unchanged" and entries:
            entries[-1] = (entries[-1][0], record.ts)
            continue
        entries.append((record, record.ts))
    if not entries:
        return "(no observations in this window)"
    parts: list[str] = []
    last_tree: dict[tuple[str, str], str] = {}
    for entry, until in entries:
        stamp = clock(entry.ts)
        dwell = seconds_between(entry.ts, until)
        dwell_text = f" · stayed {dwell:.0f}s" if dwell >= 1 else ""
        app = entry.app or "?"
        if entry.kind == "observer_paused":
            parts.append(f"[{stamp}] (session paused{dwell_text})")
        elif entry.excluded:
            why = ("its page is blocked" if entry.reason == Reason.SITE else "not listed" if entry.reason == Reason.DEFAULT
                   else "not answered yet" if entry.reason == Reason.ASK else "blocked by its rule")
            parts.append(f"[{stamp}] {app} (content not read: {why}{dwell_text})")
        elif entry.error:
            parts.append(f"[{stamp}] {app}: no readable window ({entry.error}){dwell_text}")
        elif entry.kind == "screenshot_taken":
            parts.append(f"[{stamp}] screenshot saved: {entry.path or ''}")
        else:
            title = entry.window
            pointer = ""
            if entry.pointer_app:
                pointer = f' · pointer over {entry.pointer_app} — "{entry.pointer_window or ""}"'
            others = entry.visible or []
            visible = f"\nalso on screen: {'; '.join(others[:8])}" if others else ""
            if entry.selection:
                visible += f'\nselected: "{entry.selection}"'  # what the person marked in it
            key = (app, title)
            excerpt = excerpt_tree(entry.tree, last_tree.get(key), tree_chars)
            last_tree[key] = entry.tree
            parts.append(f'[{stamp}] {app} — "{title}"{dwell_text}{pointer}{visible}\n{excerpt}')
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        head = text[: max_chars // 3]
        tail = text[-(max_chars - max_chars // 3):]
        text = head + "\n\n… (middle of the window omitted) …\n\n" + tail
    return text


def latest_snapshot(observations: Sequence[Snapshot | dict[str, Any]]) -> Snapshot | None:
    """The most recent readable window in a batch of snapshot records."""
    return next((item for item in reversed(as_snapshots(observations)) if item.kind == "screen_changed" and item.tree), None)

