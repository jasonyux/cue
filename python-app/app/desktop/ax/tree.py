"""Flatten an accessibility tree into indexed text lines the model can act on.

The walk is platform-independent: a ``Reader`` supplies attribute lookups so the
serialization can be unit-tested without the macOS Accessibility framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

Frame = tuple[float, float, float, float]

INTERACTIVE_ROLES = frozenset({
    "AXButton", "AXPopUpButton", "AXMenuButton", "AXCheckBox", "AXRadioButton", "AXTextField", "AXTextArea",
    "AXSearchField", "AXComboBox", "AXSlider", "AXIncrementor", "AXLink", "AXMenuItem", "AXMenuBarItem",
    "AXRow", "AXCell", "AXDisclosureTriangle", "AXSwitch", "AXToggle", "AXColorWell", "AXDateField",
    "AXTimeField", "AXStepper", "AXTab", "AXOutlineRow", "AXHandle", "AXValueIndicator", "AXPageRole",
})
TEXT_ROLES = frozenset({"AXStaticText", "AXHeading", "AXImage", "AXWindow", "AXSheet", "AXDialog", "AXGroup", "AXList",
                        "AXTable", "AXOutline", "AXTabGroup", "AXToolbar", "AXProgressIndicator", "AXBusyIndicator",
                        "AXWebArea", "AXScrollArea", "AXSplitGroup", "AXDrawer", "AXPopover", "AXGenericElement",
                        "AXLevelIndicator", "AXRelevanceIndicator", "AXHelpTag", "AXMenu", "AXMenuBar"})
STANDARD_ACTIONS = frozenset({"AXPress", "AXScrollToVisible", "AXRaise", "AXShowDefaultUI", "AXShowAlternateUI"})
MAX_TEXT = 160


class Reader(Protocol):
    def attribute(self, element: Any, name: str) -> Any: ...

    def children(self, element: Any) -> list[Any]: ...

    def frame(self, element: Any) -> Frame | None: ...

    def actions(self, element: Any) -> list[str]: ...


@dataclass
class Element:
    index: int
    role: str
    title: str
    value: str
    frame: Frame | None
    ref: Any
    depth: int
    actions: tuple[str, ...] = ()
    enabled: bool = True
    focused: bool = False
    selected: bool = False
    placeholder: str = ""

    def center(self) -> tuple[float, float] | None:
        if self.frame is None:
            return None
        x, y, width, height = self.frame
        return (x + width / 2, y + height / 2)

    def line(self) -> str:
        parts = [f"[{self.index}]", "  " * min(self.depth, 12) + self.role]
        if self.title:
            parts.append(f'"{clip(self.title)}"')
        if self.value:
            parts.append(f"value={clip(self.value)!r}")
        if self.placeholder:
            parts.append(f"placeholder={clip(self.placeholder)!r}")
        if self.frame is not None:
            x, y, width, height = self.frame
            parts.append(f"({x:.0f},{y:.0f} {width:.0f}x{height:.0f})")
        flags = [name for name, present in (("disabled", not self.enabled), ("focused", self.focused),
                                            ("selected", self.selected)) if present]
        parts.extend(flags)
        extra = [action for action in self.actions if action not in STANDARD_ACTIONS]
        if extra:
            parts.append("actions=" + ",".join(extra))
        return " ".join(parts)


def clip(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def scalar(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return "" if value is None else str(value).lower()
    if isinstance(value, (str, int, float)):
        return str(value)
    return ""


def intersects(frame: Frame | None, window: Frame) -> bool:
    if frame is None:
        return True
    x, y, width, height = frame
    wx, wy, wwidth, wheight = window
    return x < wx + wwidth and x + width > wx and y < wy + wheight and y + height > wy


def serialize_tree(
    root: Any,
    reader: Reader,
    *,
    window_frame: Frame,
    max_elements: int = 700,
    max_depth: int = 60,
    max_nodes: int = 8000,
) -> list[Element]:
    """Depth-first walk that assigns an index to every element worth acting on or reading."""
    elements: list[Element] = []
    visited = 0
    origin_x, origin_y = window_frame[0], window_frame[1]
    stack: list[tuple[Any, int]] = [(root, 0)]
    while stack and len(elements) < max_elements and visited < max_nodes:
        element, depth = stack.pop()
        visited += 1
        role = scalar(reader.attribute(element, "AXRole")) or "AXUnknown"
        subrole = scalar(reader.attribute(element, "AXSubrole"))
        if role == "AXCheckBox" and subrole == "AXSwitch":
            role = "AXSwitch"
        frame = reader.frame(element)
        title = scalar(reader.attribute(element, "AXTitle")) or scalar(reader.attribute(element, "AXDescription"))
        raw_value = reader.attribute(element, "AXValue")
        value = scalar(raw_value)
        if not title and not value and role in ("AXStaticText", "AXHeading"):
            title = ""
        if role == "AXStaticText" and not title:
            title, value = value, ""
        placeholder = scalar(reader.attribute(element, "AXPlaceholderValue")) if role in INTERACTIVE_ROLES else ""
        onscreen = intersects(frame, window_frame)
        worth_listing = onscreen and (role in INTERACTIVE_ROLES or ((title or value) and role in TEXT_ROLES))
        if worth_listing:
            local = None
            if frame is not None:
                local = (frame[0] - origin_x, frame[1] - origin_y, frame[2], frame[3])
            enabled = reader.attribute(element, "AXEnabled")
            elements.append(Element(
                index=len(elements), role=role, title=title, value=value, frame=local, ref=element, depth=depth,
                actions=tuple(reader.actions(element)) if role in INTERACTIVE_ROLES else (),
                enabled=enabled is not False,
                # Table cells report AXFocused for their whole row; only editable and button-like elements matter.
                focused=role not in ("AXRow", "AXCell", "AXOutlineRow") and reader.attribute(element, "AXFocused") is True,
                selected=reader.attribute(element, "AXSelected") is True, placeholder=placeholder,
            ))
        if depth < max_depth:
            children = reader.children(element)
            for child in reversed(children):
                stack.append((child, depth + 1))
    return elements


def render(elements: list[Element], header: str, truncated: bool) -> str:
    lines = [header, *(element.line() for element in elements)]
    if truncated:
        lines.append("… (tree truncated; scroll or narrow the view to see more elements)")
    return "\n".join(lines)
