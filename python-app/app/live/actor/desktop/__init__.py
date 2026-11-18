"""The desktop agent: an OpenAI model driving the actor's desktop worker with exec_py.

``run.py`` is its entry point; ``agent_loop.py`` the model loop; ``tools.py`` the exec_py, wait_cell and stop_cell
tools; ``worker.py`` and ``runtime.py`` the worker (copies of app/desktop/{worker,runtime}.py that the actor may
change); ``remote.py`` and ``cua/`` the remote desktop."""
