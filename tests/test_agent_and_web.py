import os

import pytest
from fastapi.testclient import TestClient

from salu.app import SaluApp
from salu.brain.claude_cli import enforce_auth_mode, is_api_billing
from salu.config import ConfigError, load_settings
from salu.memory import MemoryStore

from .conftest import FakeBrain


# ---- config ------------------------------------------------------------------
def test_config_rejects_network_exposure(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("server:\n  host: 0.0.0.0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(root=tmp_path)


def test_config_rejects_unknown_keys(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("claude:\n  api_key: oops\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(root=tmp_path)


def test_subscription_mode_strips_api_billing_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real-000000")
    removed = enforce_auth_mode("subscription")
    assert "ANTHROPIC_API_KEY" in removed and "ANTHROPIC_API_KEY" not in os.environ


def test_api_key_mode_keeps_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real-000000")
    assert enforce_auth_mode("api_key") == [] and os.environ["ANTHROPIC_API_KEY"]


def test_is_api_billing():
    assert not is_api_billing("claude.ai")
    assert is_api_billing("api_key") and is_api_billing("console")


# ---- memory --------------------------------------------------------------------
def test_task_history_and_interrupted_tasks():
    m = MemoryStore(":memory:")
    t1 = m.start_task("check server")
    m.finish_task(t1, "done", "ok", ["system_status"])
    m.start_task("crashed one")
    assert m.mark_interrupted_tasks() == 1
    statuses = {t["request"]: t["status"] for t in m.recent_tasks()}
    assert statuses == {"check server": "done", "crashed one": "failed"}


# ---- startup + agent -------------------------------------------------------------
async def test_startup_checks_are_real(settings):
    salu = SaluApp(settings, brain=FakeBrain(healthy=False), console_log=False)
    checks = await salu.startup_checks()
    by = {c.name: c for c in checks}
    assert not by["Claude connection"].ok and not salu.online
    assert by["Memory"].ok and by["Tool registry"].ok and by["Security system"].ok
    await salu.shutdown()


async def test_agent_runs_task_with_tools(settings):
    brain = FakeBrain([("tool", "current_datetime", {}), ("text", "It is now.")])
    salu = SaluApp(settings, brain=brain, console_log=False)
    events = []

    async def sink(kind, data):
        events.append((kind, data))

    res = await salu.agent.handle("what time is it?", sink)
    assert res["ok"] and res["tools_used"] == ["current_datetime"]
    kinds = [k for k, _ in events]
    assert kinds[0] == "task_started" and "tool_result" in kinds and kinds[-2] == "task_finished"
    task = salu.memory.recent_tasks()[0]
    assert task["status"] == "done" and task["tools_used"] == ["current_datetime"]
    assert "SALU" in brain.sessions[0].system_prompt
    await salu.shutdown()


async def test_agent_reports_failures(settings):
    salu = SaluApp(settings, brain=FakeBrain([("error", "usage limit reached")]), console_log=False)
    events = []

    async def sink(kind, data):
        events.append((kind, data))

    res = await salu.agent.handle("hi", sink)
    assert not res["ok"]
    assert ("error", ) == tuple(k for k, _ in events if k == "error")
    assert salu.memory.recent_tasks()[0]["status"] == "failed"
    await salu.shutdown()


# ---- web server ----------------------------------------------------------------------
@pytest.fixture
def web(settings):
    from salu.web.server import create_web_app
    brain = FakeBrain([("tool", "write_text_file", {"path": str(settings.root / "sandbox" / "w.txt"),
                                                    "content": "hi"}),
                       ("text", "Done.")])
    salu = SaluApp(settings, brain=brain, console_log=False)
    app = create_web_app(salu, token="tok123")
    with TestClient(app) as client:
        yield client, salu, settings


ORIGIN = {"origin": "http://127.0.0.1:8765"}


def test_api_requires_token(web):
    client, _, _ = web
    assert client.get("/api/state").status_code == 401
    r = client.get("/api/state", headers={"X-Salu-Token": "tok123"})
    assert r.status_code == 200 and len(r.json()["checks"]) == 5
    assert client.get("/").status_code == 200


def test_ws_rejects_bad_token_or_origin(web):
    client, _, _ = web
    from starlette.websockets import WebSocketDisconnect
    for url, headers in (("/ws?t=wrong", ORIGIN), ("/ws?t=tok123", {"origin": "https://evil.example"})):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url, headers=headers) as ws:
                ws.receive_text()


def test_ws_chat_with_confirmation_flow(web):
    client, salu, settings = web
    with client.websocket_connect("/ws?t=tok123", headers=ORIGIN) as ws:
        ws.send_json({"type": "chat", "text": "write a file"})
        seen = []
        while True:
            ev = ws.receive_json()
            seen.append(ev["type"])
            if ev["type"] == "approval_required":
                assert ev["risk"] == "medium" and ev["tool"] == "write_text_file"
                ws.send_json({"type": "approve", "id": ev["id"], "approved": True})
            if ev["type"] == "task_finished":
                assert ev["status"] == "done"
                break
    assert "approval_required" in seen and "tool_result" in seen and "text" in seen
    assert (settings.root / "sandbox" / "w.txt").read_text(encoding="utf-8") == "hi"


def test_ws_denied_confirmation_does_not_execute(web):
    client, salu, settings = web
    with client.websocket_connect("/ws?t=tok123", headers=ORIGIN) as ws:
        ws.send_json({"type": "chat", "text": "write a file"})
        while True:
            ev = ws.receive_json()
            if ev["type"] == "approval_required":
                ws.send_json({"type": "approve", "id": ev["id"], "approved": False})
            if ev["type"] == "tool_result":
                assert not ev["ok"] and ev["stage"] == "denied"
            if ev["type"] == "task_finished":
                break
    assert not (settings.root / "sandbox" / "w.txt").exists()
