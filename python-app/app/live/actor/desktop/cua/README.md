# Remote desktop for the live actor

An isolated Ubuntu desktop the actor can use instead of the user's Mac: its own browser, its own downloads, none
of the user's files or accounts. Our worker runs inside it behind a WebSocket bridge (`server.py`), and the actor
on the Mac connects with `client.py`. With `remote` in `actor.desktop_source` (settings.yaml) next to `local`, `exec_py` gains a `target` argument, `local` or `remote`,
and the model picks per call; with `remote` alone the agent works only there; with `local` alone nothing changes.

## One-time setup on the host (a Linux box with Docker)

The image is published, so a host only pulls it:

```sh
docker run -d --name cua-actor --stop-timeout 120 \
  -e CUA_DESKTOP_REMOTE_TOKEN='<a long random secret>' \
  -p 45164:8100 -p 37737:6901 jasonyux/cua-actor:latest
curl -s http://localhost:45164/status        # → ready
```

Port 8100 is the bridge; 6901 is the image's own noVNC, so `http://<host>:37737` shows the desktop live. Later:
`docker stop cua-actor` and `docker start cua-actor`. One container serves one agent at a time (plus viewers): a
container per person, each with its own name, ports and token.

### Publishing the image (after a change to `server.py`, `worker.py` or the Dockerfile)

```sh
# from a checkout, in python-app/app/live/actor/desktop
docker build -f cua/Dockerfile -t jasonyux/cua-actor:latest .
docker login -u jasonyux        # Docker Hub, with an access token as the password
docker push jasonyux/cua-actor:latest
```

A public Docker Hub repository pulls without a login; a private one needs `docker login` on every host that pulls it. The base image is x86-64; a host of another architecture runs it
under emulation, too slowly for a desktop.

### Reaching it from the Mac

Publish the bridge port however the host allows. Behind a Cloudflare named tunnel, add an ingress entry:

```yaml
  - hostname: cua-actor.example.org
    service: http://localhost:45164
```

The tunnel carries WebSockets and keeps them open, so the Mac uses `wss://cua-actor.example.org`. The bridge
executes Python in the container for anyone who presents the token, so keep the token long and private, or put
Cloudflare Access in front of the hostname.

## On the Mac

On the Settings page, the **Remote desktop** rows take the desktops, the provider, the URL and the token (Save writes the
first three to `settings.yaml`, the token to `.env`). By hand, in `settings.yaml`:

```yaml
actor:
  desktop_source: [local, remote]   # or [remote]: the agent works only there
remote_desktop:
  provider: cua
  url: wss://cua-actor.example.org  # or ws://host:45164 on a private network
```

and in `.env`, with the API keys: `export CUA_DESKTOP_REMOTE_TOKEN="<the same secret>"`. **Test configuration** on the
live page opens the bridge and waits for the worker's ready line. Every run from the live page then connects once
and gets a fresh REPL on the remote desktop; closing the run kills it.

## Inside the container

Everything runs as root, the desktop session and the agent's shell included, so `apt-get install -y tmux` needs no
sudo. That is deliberate: a shared Docker host may re-own its data directory in the background to the account that
runs Docker, which is root inside a rootless container, and then anything another user created a minute ago is
root's: Firefox loses its profile ("Secure Connection Failed") and the terminal cannot delete a file it just made.
A fresh container starts from the image again, so anything wanted permanently goes into the Dockerfile. Firefox runs
with its content sandbox off (rootless Docker denies the user namespaces it needs), which is fine for a disposable
desktop. You can watch and use the same desktop through noVNC while the agent works; the two share the screen.

The image has Firefox (cua's) and LibreOffice (Writer, Calc, Impress). The desktop wears WhiteSur (a macOS-like GTK and window theme, with its icons) and the Inter font, set by `theme.sh` when
the session starts; nothing about what runs there changes. Another look is a matter of that script and the Dockerfile's
theme block.

## What the model gets there

`pyautogui` and screenshots on the container's X display, the same `log` and `display` helpers, yielding cells,
`wait_cell` and `stop_cell`. No `ax`: the remote has no accessibility tree yet. Cell ids from the remote start
with `r`, local ones with `c`.
