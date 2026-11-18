"""The stand-in frame a desktop run records when no app window has been observed yet.

Everything about it lives here: its canvas, its message, how it is drawn, and how a stored screenshot record is
recognized as one (records written before the ``placeholder`` flag existed only have the fixed canvas to go by).
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageDraw

PLACEHOLDER_SIZE = (640, 120)
PLACEHOLDER_MESSAGE = "No app window observed yet. The model has not called ax.get_state()."


def placeholder_png(message: str = PLACEHOLDER_MESSAGE) -> bytes:
    image = Image.new("RGB", PLACEHOLDER_SIZE, (245, 245, 245))
    ImageDraw.Draw(image).text((16, 50), message, fill=(80, 80, 80))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def is_placeholder(artifact: dict[str, Any]) -> bool:
    """True for a flagged record, or for a timeline capture with the placeholder's canvas (pre-flag records)."""
    if artifact.get("placeholder"):
        return True
    timeline = artifact.get("source") != "code_tool"
    return timeline and (artifact.get("imageWidth"), artifact.get("imageHeight")) == PLACEHOLDER_SIZE
