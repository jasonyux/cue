"""Image encoding for model calls."""

from __future__ import annotations

import base64
import io
from typing import Any

from PIL import Image


def encode_screenshot(path: Any, max_side: int = 1024) -> str | None:
    """A PNG data URL of the file, downscaled so its longer side is at most ``max_side`` pixels."""
    try:
        with Image.open(path) as image:
            picture = image.convert("RGB")
            longest = max(picture.size)
            if longest > max_side:
                scale = max_side / longest
                picture = picture.resize((max(1, round(picture.width * scale)), max(1, round(picture.height * scale))),
                                         Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            picture.save(buffer, format="PNG", optimize=True)
    except (OSError, ValueError):
        return None
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
