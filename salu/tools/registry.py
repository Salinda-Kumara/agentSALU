"""Tool registry with plugin discovery.

Built-in tools live in salu/tools/builtin/*.py. Extra tools can be dropped into
any folder listed in `tools.plugin_dirs` — no core changes needed. A module
contributes tools by defining a module-level `TOOLS = [...]` list of Tool objects.

Plugins are ordinary Python code running inside SALU: only install code you trust.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
from pathlib import Path

from .base import Tool

log = logging.getLogger("salu.tools")


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self.load_errors: list[str] = []

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise TypeError(f"not a Tool: {tool!r}")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: (t.category, t.name))

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def _register_module(self, module, origin: str) -> int:
        tools = getattr(module, "TOOLS", None)
        if tools is None:
            return 0
        count = 0
        for t in tools:
            try:
                self.register(t)
                count += 1
            except Exception as e:
                self.load_errors.append(f"{origin}: {e}")
                log.error("tool registration failed origin=%s error=%s", origin, e)
        return count

    def load_builtin(self) -> int:
        from . import builtin
        count = 0
        for info in pkgutil.iter_modules(builtin.__path__):
            name = f"{builtin.__name__}.{info.name}"
            try:
                count += self._register_module(importlib.import_module(name), name)
            except Exception as e:
                self.load_errors.append(f"{name}: {e}")
                log.exception("failed to load builtin tool module %s", name)
        return count

    def load_plugins(self, dirs: list[Path]) -> int:
        count = 0
        for d in dirs:
            if not d.is_dir():
                continue
            for path in sorted(d.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                mod_name = f"salu_plugin_{path.stem}"
                try:
                    spec = importlib.util.spec_from_file_location(mod_name, path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    n = self._register_module(module, str(path))
                    count += n
                    log.info("plugin loaded path=%s tools=%d", path, n)
                except Exception as e:
                    self.load_errors.append(f"{path}: {e}")
                    log.exception("failed to load plugin %s", path)
        return count


def build_registry(plugin_dirs: list[Path] | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    reg.load_builtin()
    reg.load_plugins(plugin_dirs or [])
    return reg
