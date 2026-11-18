"""The live actor's real worker code with a bare namespace (no pyautogui, no accessibility), for runtime tests."""

import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # python-app

from app.live.actor.desktop import worker

worker.open_channel()
signal.signal(signal.SIGINT, signal.default_int_handler)
worker.emit({"ready": True, "platform": sys.platform, "mode": "fake"})
worker.serve_requests({"__builtins__": __builtins__, "time": time}, sys.stdin)
