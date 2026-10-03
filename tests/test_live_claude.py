"""Live test against the real Claude backend (uses a little of your subscription quota).

Run explicitly:  python -m pytest -m live
"""

import pytest

from salu.app import SaluApp
from salu.config import load_settings

pytestmark = pytest.mark.live


async def test_live_claude_uses_salu_tool():
    salu = SaluApp(load_settings(), console_log=False)
    checks = await salu.startup_checks()
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    events = []

    async def sink(kind, data):
        events.append((kind, data))

    res = await salu.agent.handle("What is the current date and time? Use your tool.", sink)
    await salu.shutdown()
    assert res["ok"], res["error"]
    assert "current_datetime" in res["tools_used"]
    assert res["reply"]
