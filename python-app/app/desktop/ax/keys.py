"""Key chord parsing for process-targeted keyboard events (macOS virtual key codes, US layout)."""

from __future__ import annotations

# Core Graphics event flag masks (CGEventFlags).
FLAG_SHIFT = 1 << 17
FLAG_CONTROL = 1 << 18
FLAG_ALTERNATE = 1 << 19
FLAG_COMMAND = 1 << 20

MODIFIERS = {
    "cmd": FLAG_COMMAND, "command": FLAG_COMMAND, "super": FLAG_COMMAND, "meta": FLAG_COMMAND, "win": FLAG_COMMAND,
    "ctrl": FLAG_CONTROL, "control": FLAG_CONTROL,
    "alt": FLAG_ALTERNATE, "option": FLAG_ALTERNATE, "opt": FLAG_ALTERNATE,
    "shift": FLAG_SHIFT,
}

# Virtual key codes from Carbon's Events.h for an ANSI US keyboard.
KEY_CODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "return": 36, "tab": 48, "space": 49, "backspace": 51, "escape": 53, "delete": 117,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80,
    "kp_0": 82, "kp_1": 83, "kp_2": 84, "kp_3": 85, "kp_4": 86, "kp_5": 87, "kp_6": 88, "kp_7": 89, "kp_8": 91,
    "kp_9": 92, "kp_enter": 76, "kp_decimal": 65, "kp_multiply": 67, "kp_add": 69, "kp_divide": 75,
    "kp_subtract": 78, "kp_equal": 81, "clear": 71,
}

KEY_ALIASES = {
    "enter": "return", "ret": "return", "esc": "escape", "bksp": "backspace", "back": "backspace",
    "del": "delete", "forwarddelete": "delete", "forward_delete": "delete", "pgup": "pageup", "prior": "pageup",
    "pgdn": "pagedown", "next": "pagedown", "arrowleft": "left", "arrowright": "right", "arrowup": "up",
    "arrowdown": "down", "spacebar": "space", " ": "space", "plus": "=", "minus": "-", "equal": "=",
    "period": ".", "comma": ",", "slash": "/", "backslash": "\\", "semicolon": ";", "quote": "'",
    "apostrophe": "'", "grave": "`", "backquote": "`", "bracketleft": "[", "bracketright": "]",
    "numpad_0": "kp_0", "numpad_1": "kp_1", "numpad_2": "kp_2", "numpad_3": "kp_3", "numpad_4": "kp_4",
    "numpad_5": "kp_5", "numpad_6": "kp_6", "numpad_7": "kp_7", "numpad_8": "kp_8", "numpad_9": "kp_9",
    "numpad_enter": "kp_enter",
}

# Characters that are typed as shift plus another key on a US layout.
SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/",
    "~": "`",
}


def parse_chord(chord: str) -> tuple[int, int]:
    """Return (virtual key code, modifier flags) for a chord such as ``cmd+shift+a`` or ``Return``."""
    if not isinstance(chord, str) or not chord.strip():
        raise ValueError("A key chord must be a nonempty string such as 'Return' or 'cmd+a'.")
    text = chord.strip()
    if text == "+":
        parts = ["+"]
    elif text.endswith("++"):
        parts = [part.strip() for part in text[:-2].split("+")] + ["+"]  # e.g. "cmd++"
    else:
        parts = [part.strip() for part in text.split("+")]
    if any(part == "" for part in parts):
        raise ValueError(f"Malformed key chord: {chord!r}.")
    flags = 0
    for modifier in parts[:-1]:
        mask = MODIFIERS.get(modifier.lower())
        if mask is None:
            raise ValueError(f"Unknown modifier {modifier!r} in chord {chord!r}. Use cmd, ctrl, alt, or shift.")
        flags |= mask
    key = parts[-1]
    name = key.lower() if len(key) > 1 else key
    name = KEY_ALIASES.get(name, name)
    if name in MODIFIERS:
        raise ValueError(f"Chord {chord!r} names only modifiers; add a key such as 'a' or 'Return'.")
    if len(name) == 1:
        if name in SHIFTED:
            flags |= FLAG_SHIFT
            name = SHIFTED[name]
        elif name.isalpha() and name.isupper():
            flags |= FLAG_SHIFT
            name = name.lower()
    code = KEY_CODES.get(name)
    if code is None:
        raise ValueError(f"Unknown key {key!r} in chord {chord!r}.")
    return code, flags


def key_code_for_character(character: str) -> tuple[int, int] | None:
    """Best-effort (code, flags) for a single character; None when the character needs Unicode input."""
    if len(character) != 1:
        return None
    if character == "\n":
        return KEY_CODES["return"], 0
    if character == "\t":
        return KEY_CODES["tab"], 0
    if character == " ":
        return KEY_CODES["space"], 0
    try:
        return parse_chord(character)
    except ValueError:
        return None
