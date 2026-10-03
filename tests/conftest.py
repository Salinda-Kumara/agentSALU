from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest

from salu.brain.base import Brain, BrainEvent, BrainSession, HealthResult
from salu.config import Settings, load_settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(
        "tools:\n  allowed_roots: [sandbox]\n  plugin_dirs: [plugins]\n"
        "memory:\n  database: data/test.db\n"
        "logging:\n  file: logs/test.log\n"
        "security:\n  approval_timeout_seconds: 2\n", encoding="utf-8")
    (tmp_path / "sandbox").mkdir()
    (tmp_path / "plugins").mkdir()
    return load_settings(root=tmp_path, env_file=tmp_path / ".env")


class ScriptedSession(BrainSession):
    """Fake brain: each script step is ("text", str) or ("tool", name, args) or ("error", str)."""

    def __init__(self, executor, script):
        self.executor = executor
        self.script = script
        self.tool_results = []

    async def send(self, text: str) -> AsyncIterator[BrainEvent]:
        for step in self.script:
            if step[0] == "tool":
                r = await self.executor.execute(step[1], step[2])
                self.tool_results.append(r)
            elif step[0] == "text":
                yield BrainEvent("text", step[1])
            elif step[0] == "error":
                yield BrainEvent("error", step[1])
                return
        yield BrainEvent("done", "", {"turns": 1})


class FakeBrain(Brain):
    name = "fake"

    def __init__(self, script=None, healthy=True):
        self.script = script or [("text", "Hello.")]
        self.healthy = healthy
        self.sessions: list[ScriptedSession] = []
        self.removed_env = []

    async def health_check(self) -> HealthResult:
        return HealthResult(self.healthy, "fake brain" if self.healthy else "not logged in", {"auth_mode": "fake"})

    def new_session(self, executor, registry, system_prompt):
        s = ScriptedSession(executor, self.script)
        s.system_prompt = system_prompt
        self.sessions.append(s)
        return s
