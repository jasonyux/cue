FROM trycua/cua-xfce:latest

# cua's own API server binds to loopback only, so Docker's published port for it reaches nothing.
RUN sed -i 's/-m computer_server/-m computer_server --host 0.0.0.0/' /usr/local/bin/start-computer-server.sh

# Everything runs as root, the desktop session included. The image runs it as user cua, but a shared Docker host may
# re-own its data directory in the background to the account that runs Docker, which is root inside a rootless
# container: a file cua created a minute ago is then root's, Firefox loses its profile ("Secure Connection Failed")
# and the terminal cannot delete what it just made. Root is the owner such a host enforces, so nothing changes under it.
RUN sed -i 's/^user=cua$/user=root/' /etc/supervisor/conf.d/supervisord.conf

RUN uv pip install --python /opt/venv/bin/python3 pyautogui pillow websockets  # the image ships uv, not pip, for its venv

ARG WHITESUR_GTK_REPO=https://github.com/vinceliuice/WhiteSur-gtk-theme.git
ARG WHITESUR_ICON_REPO=https://github.com/vinceliuice/WhiteSur-icon-theme.git
ARG WALLPAPER_URL=https://raw.githubusercontent.com/vinceliuice/WhiteSur-wallpapers/main/1080p/WhiteSur-light.png
RUN apt-get update && apt-get install -y --no-install-recommends git curl sassc libglib2.0-dev-bin libxml2-utils optipng gtk-update-icon-cache librsvg2-common fonts-inter \
 && tmp="$(mktemp -d)" && export USER=root \
 && git clone --depth=1 "$WHITESUR_GTK_REPO" "$tmp/gtk" && (cd "$tmp/gtk" && ./install.sh -d /usr/share/themes -c light -t default --silent-mode) \
 && git clone --depth=1 "$WHITESUR_ICON_REPO" "$tmp/icons" && (cd "$tmp/icons" && ./install.sh -d /usr/share/icons) \
 && mkdir -p /usr/share/backgrounds && (curl -fsSL -o /usr/share/backgrounds/whitesur.png "$WALLPAPER_URL" || echo "no wallpaper") \
 && rm -rf "$tmp" && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY cua/theme.sh /opt/actor/cua/theme.sh

# Office apps for the agent's tasks (LibreOffice, with its GTK look so it wears the theme too).
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-gtk3 \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY worker.py /opt/actor/worker.py
COPY cua/server.py /opt/actor/cua/server.py
COPY cua/supervisor.conf /etc/supervisor/conf.d/worker-bridge.conf

# Firefox's content sandbox needs user namespaces, which this Docker setup denies (rootless); without these it cannot open a page.
ENV MOZ_DISABLE_CONTENT_SANDBOX=1 MOZ_DISABLE_GPU_SANDBOX=1
