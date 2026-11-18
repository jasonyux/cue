#!/bin/sh
# The session's look, applied once the desktop is up through the session's own settings daemon (reached as xfdesktop
# reaches it), so it shows at once and is kept in the session user's settings: WhiteSur's GTK and window themes and
# icons, the Inter font, and the wallpaper on every monitor and workspace. Nothing else about the desktop changes.
for _ in $(seq 120); do pid=$(pgrep -o -x xfdesktop) && break; sleep 1; done
[ -n "$pid" ] || { echo "the desktop never came up"; exit 1; }
sleep 3  # until its settings are written
for name in DISPLAY DBUS_SESSION_BUS_ADDRESS HOME XDG_RUNTIME_DIR; do
  value=$(tr '\0' '\n' < "/proc/$pid/environ" | grep "^$name=") && export "$value"
done
put() { xfconf-query -c "$1" -p "$2" -n -t "$3" -s "$4"; }
put xsettings /Net/ThemeName string WhiteSur-Light
put xsettings /Net/IconThemeName string WhiteSur
put xsettings /Gtk/FontName string "Inter 10"
put xsettings /Xft/Antialias int 1
put xsettings /Xft/Hinting int 1
put xsettings /Xft/HintStyle string hintslight
put xfwm4 /general/theme string WhiteSur-Light
put xfwm4 /general/title_font string "Inter Bold 10"
if [ -f /usr/share/backgrounds/whitesur.png ]; then
  for path in $(xfconf-query -c xfce4-desktop -l | grep '/last-image$'); do
    put xfce4-desktop "$path" string /usr/share/backgrounds/whitesur.png
    put xfce4-desktop "${path%last-image}image-style" int 5  # zoomed
  done
fi
