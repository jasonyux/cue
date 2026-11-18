"""Decide: worklog + recent snapshots → proposals.jsonl, a ranked list of proposals.

Main output and where it is produced: ``proposals.jsonl``, by ``Proposer.run_window`` in ``propose.py`` (prompt in
``prompts.py``); ``notify.py`` then delivers the top proposal as a notification and stamps the record's effect.
May import from the observer (its stream and digest), never from the actor.
"""
