"""Act: carry an accepted proposal out.

The live page's runs come here: ``app/scenario_runtime.py`` hands a run with live context to ``run.py``, the entry
point, which builds the run's ``ActorInput`` (``prompts.py``: what a run is given and the instructions it becomes,
for either agent) and hands it to the agent the run names. ``desktop/`` is the desktop agent, an OpenAI model driving
the actor's own desktop worker with exec_py (a copy of the agent in ``app/scenario_runtime.py``, which stays intact);
``cmux/`` is the cmux agent, Claude Code in a cmux workspace. ``session_runs.py`` is the session's side: what a run is
shown, where it is stored, what the session learns back. The run lifecycle and storage are the runner's
(``app/runner.py``). May import from the observer and the proposer.
"""
