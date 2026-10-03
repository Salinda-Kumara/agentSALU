"""Tool definition contract.

A tool = name + description + JSON input schema + risk level + async handler.
Handlers receive validated arguments and a ToolContext, and return a ToolResult
(or raise — the executor converts exceptions into failed results; failures are
never hidden).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..security.permissions import Risk

if TYPE_CHECKING:
    from ..config import Settings
    from ..memory import MemoryStore

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


@dataclass
class ToolResult:
    ok: bool
    output: str
    data: dict[str, Any] | None = None

    @classmethod
    def success(cls, output: str, data: dict | None = None) -> "ToolResult":
        return cls(True, output, data)

    @classmethod
    def failure(cls, output: str, data: dict | None = None) -> "ToolResult":
        return cls(False, output, data)


@dataclass
class ToolContext:
    settings: "Settings"
    logger: logging.Logger
    task_id: str | None = None
    memory: "MemoryStore | None" = None


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: Risk
    handler: Handler
    category: str = "general"
    # Optional: compute a (higher) risk from the actual arguments, e.g. overwrite => HIGH.
    risk_fn: Callable[[dict[str, Any]], Risk] | None = None
    # Optional: human-readable lines for confirmation dialogs.
    describe_fn: Callable[[dict[str, Any]], list[str]] | None = None
    timeout_seconds: float = 60

    def __post_init__(self):
        if not NAME_RE.match(self.name):
            raise ValueError(f"invalid tool name {self.name!r} (lowercase letters, digits, underscore)")
        if not self.description.strip():
            raise ValueError(f"tool {self.name} needs a description")
        if self.input_schema.get("type") != "object":
            raise ValueError(f"tool {self.name}: input_schema must be a JSON-schema object")

    def risk_for(self, args: dict[str, Any]) -> Risk:
        """Risk for these arguments. Can raise but never lower the declared risk."""
        if self.risk_fn is None:
            return self.risk
        try:
            return max(self.risk, Risk.parse(self.risk_fn(args)))
        except Exception:
            return Risk.HIGH  # if risk can't be computed, assume the worst

    def describe(self, args: dict[str, Any]) -> list[str]:
        if self.describe_fn:
            try:
                return list(self.describe_fn(args))
            except Exception:
                pass
        return [f"{k}: {v!r}"[:200] for k, v in args.items()]


def salu_tool(name: str, description: str, *, risk: Risk | str = Risk.LOW,
              properties: dict[str, Any] | None = None, required: list[str] | None = None,
              category: str = "general", risk_fn=None, describe_fn=None,
              timeout_seconds: float = 60) -> Callable[[Handler], Tool]:
    """Decorator that turns an async handler into a Tool."""
    schema = {"type": "object", "properties": properties or {}, "required": required or [],
              "additionalProperties": False}

    def wrap(fn: Handler) -> Tool:
        return Tool(name=name, description=description, input_schema=schema, risk=Risk.parse(risk),
                    handler=fn, category=category, risk_fn=risk_fn, describe_fn=describe_fn,
                    timeout_seconds=timeout_seconds)

    return wrap
