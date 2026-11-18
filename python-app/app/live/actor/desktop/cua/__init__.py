"""The live actor's remote desktop provider: a cua Ubuntu container with our worker behind a WebSocket.

``server.py`` runs inside the container and bridges one socket to one worker process; ``client.py`` runs on the Mac
and gives the actor a second runtime with the same cell interface as the local one. The Dockerfile, the supervisor
entry and the README next to them are the one-time setup on the host.
"""
