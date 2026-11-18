"""Continuous sessions: observe the screen every few seconds, summarize and proposer every few minutes.

Data flows one way: ``observer/`` (screen → snapshots → worklog) → ``belief/`` (every session's worklog → the tasks the
person is working on, in data/memory) → ``proposer/`` (tasks + worklog → ranked proposals) →
``actor/`` (the agent that carries a run out: its loop, tools, and instructions, plus the session's side of a run). ``storage/`` owns the session folder and its record shapes, ``utils/``
holds helpers with no knowledge of the pipeline, ``model_loop.py`` the base class the summarizer and proposer share, ``config.py`` every
session setting, ``session.py`` the orchestrator, and ``api.py`` the HTTP routes. A later stage may import an
earlier one, never the reverse.
"""
