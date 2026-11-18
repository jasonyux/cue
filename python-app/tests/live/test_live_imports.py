"""Every relative import in the live package resolves, including the lazy ones inside functions that only run
on a real Mac. A module moved one directory deeper must update its dots; this catches it without executing anything."""

import ast
import importlib.util
from pathlib import Path

import app.live

LIVE = Path(app.live.__file__).parent


def test_every_relative_import_in_the_live_package_resolves():
    unresolved = []
    for path in LIVE.rglob("*.py"):
        package = "app.live" + "".join(f".{part}" for part in path.relative_to(LIVE).parent.parts)
        module = package if path.name == "__init__.py" else f"{package}.{path.stem}"
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                target = importlib.util.resolve_name("." * node.level + (node.module or ""), module.rsplit(".", 1)[0])
                try:
                    found = importlib.util.find_spec(target) is not None
                except ModuleNotFoundError:
                    found = False
                if not found:
                    unresolved.append(f"{path.relative_to(LIVE)}:{node.lineno} -> {target}")
    assert unresolved == []
