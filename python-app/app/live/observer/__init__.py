"""Perceive: the screen → snapshots.jsonl and screenshots/ → summaries.jsonl (the worklog).

Main outputs and where they are produced:
- ``snapshots.jsonl`` and ``screenshots/``: ``recorder.py``, the subprocess (one ``Snapshot`` per tick, read from
  the screen by ``sources.py``; ``recorder_process.py`` starts and stops it from the backend).
- ``summaries.jsonl``, the worklog: ``Summarizer.run_window`` in ``summarize.py`` (prompt in ``prompts.py``, the
  digest of a snapshot batch in ``digest.py``, reading the recorded stream in ``stream.py``).
Nothing here imports the proposer or the actor.
"""
