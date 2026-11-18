"""Delivering a proposal set to the person: a macOS notification for the top proposal, and the record's ``effect``.

Nothing is executed here or anywhere else from a proposal set; the person reads the proposals in the console.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from ..config import LiveSettings
from ..storage.records import ProposalSet


def send_notification(title: str, message: str) -> bool:
    """macOS notification via osascript; best effort and non-blocking."""
    if sys.platform != "darwin":
        return False
    script = f"display notification {json.dumps(message[:200])} with title {json.dumps(title[:60])}"
    try:
        subprocess.Popen(["osascript", "-e", script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


def notify_proposals(proposal_set: ProposalSet, settings: LiveSettings, *, notifier: Any = send_notification) -> ProposalSet:
    """The proposal set with its ``effect`` set: "recorded" when there is nothing to offer, otherwise "notified" or
    "proposed". Only the top proposal is surfaced; the alternatives stay in the record for the console."""
    if proposal_set.action == "none" or not proposal_set.task:
        return proposal_set.model_copy(update={"effect": "recorded"})
    others = len(proposal_set.proposals) - 1
    message = f"[{proposal_set.kind}] {proposal_set.task}" + (f" (+{others} more)" if others > 0 else "")
    delivered = bool(settings.notify and notifier("CUA observer suggests", message))
    return proposal_set.model_copy(update={"effect": "notified" if delivered else "proposed"})
