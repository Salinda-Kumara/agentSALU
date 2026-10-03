"""Provider-neutral brain interface.

The agent runtime only talks to these abstractions, so Claude (via the Agent
SDK) can be swapped for another provider without touching tools or security.
All tool execution goes through the ToolExecutor passed to `new_session`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..tools.executor import ToolExecutor
from ..tools.registry import ToolRegistry


@dataclass
class BrainEvent:
    kind: str                     # "text" | "done" | "error"
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthResult:
    ok: bool
    detail: str
    info: dict[str, Any] = field(default_factory=dict)


class BrainSession(ABC):
    @abstractmethod
    def send(self, text: str) -> AsyncIterator[BrainEvent]:
        """Send one user message; yields events until the turn is complete."""

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        return None


class Brain(ABC):
    name: str = "brain"

    @abstractmethod
    async def health_check(self) -> HealthResult: ...

    @abstractmethod
    def new_session(self, executor: ToolExecutor, registry: ToolRegistry, system_prompt: str) -> BrainSession: ...
