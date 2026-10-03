import asyncio
from pathlib import Path

import pytest

from salu.memory import MemoryStore
from salu.security.permissions import ApprovalBroker, PermissionPolicy, Risk
from salu.tools import Tool, ToolExecutor, ToolResult, build_registry, salu_tool
from salu.tools.registry import ToolRegistry


def make_executor(settings, registry=None, approve=None, auto=("low",)):
    events = []
    broker = ApprovalBroker(timeout_seconds=1)
    if approve is not None:
        async def ui(req):
            events.append(("approval_required", req.to_dict()))
            asyncio.get_running_loop().call_soon(broker.resolve, req.id, approve)
        broker.attach(ui)

    async def sink(kind, data):
        events.append((kind, data))

    ex = ToolExecutor(registry or build_registry(), PermissionPolicy(list(auto)), broker, settings,
                      events=sink, memory=MemoryStore(":memory:"))
    return ex, events


# ---- registry -----------------------------------------------------------------
def test_builtin_tools_registered():
    reg = build_registry()
    names = {t.name for t in reg.all()}
    assert {"system_status", "current_datetime", "list_directory", "read_text_file", "write_text_file",
            "remember", "recall", "forget"} <= names
    assert not reg.load_errors
    for t in reg.all():
        assert isinstance(t.risk, Risk) and t.description and t.input_schema["type"] == "object"


def test_plugin_loading(tmp_path):
    (tmp_path / "hello.py").write_text(
        "from salu.tools import ToolResult, salu_tool\n"
        "@salu_tool('hello_plugin', 'Say hello.', risk='low')\n"
        "async def h(args, ctx):\n    return ToolResult.success('hi')\n"
        "TOOLS = [h]\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    reg = ToolRegistry()
    assert reg.load_plugins([tmp_path]) == 1
    assert "hello_plugin" in reg
    assert any("boom" in e for e in reg.load_errors)


def test_duplicate_and_invalid_names_rejected():
    reg = ToolRegistry()

    async def h(a, c):
        return ToolResult.success("")

    t = Tool("dup", "d", {"type": "object"}, Risk.LOW, h)
    reg.register(t)
    with pytest.raises(ValueError):
        reg.register(t)
    with pytest.raises(ValueError):
        Tool("Bad Name", "d", {"type": "object"}, Risk.LOW, h)


# ---- executor -------------------------------------------------------------------
async def test_low_risk_runs_without_approval(settings):
    ex, events = make_executor(settings)
    r = await ex.execute("current_datetime", {})
    assert r.ok
    kinds = [k for k, _ in events]
    assert kinds == ["tool_call", "tool_result"]


async def test_unknown_tool_and_invalid_input_fail(settings):
    ex, _ = make_executor(settings)
    assert not (await ex.execute("nope", {})).ok
    r = await ex.execute("read_text_file", {"wrong": 1})
    assert not r.ok and "Invalid input" in r.output


async def test_medium_risk_denied_without_approver(settings):
    ex, _ = make_executor(settings)
    target = settings.root / "sandbox" / "new.txt"
    r = await ex.execute("write_text_file", {"path": str(target), "content": "x"})
    assert not r.ok and "no SALU window is connected" in r.output
    assert not target.exists()


async def test_medium_risk_denied_by_user(settings):
    ex, _ = make_executor(settings, approve=False)
    target = settings.root / "sandbox" / "new.txt"
    r = await ex.execute("write_text_file", {"path": str(target), "content": "x"})
    assert not r.ok and "not approved by the user" in r.output
    assert not target.exists()


async def test_medium_risk_runs_when_approved(settings):
    ex, events = make_executor(settings, approve=True)
    target = settings.root / "sandbox" / "new.txt"
    r = await ex.execute("write_text_file", {"path": str(target), "content": "hello"})
    assert r.ok, r.output
    assert target.read_text(encoding="utf-8") == "hello"
    assert ("approval_resolved", {"id": events[2][1]["id"], "approved": True}) in events


async def test_overwrite_escalates_to_high(settings):
    target = settings.root / "sandbox" / "exists.txt"
    target.write_text("old", encoding="utf-8")
    ex, events = make_executor(settings, approve=False, auto=("low", "medium"))
    r = await ex.execute("write_text_file", {"path": str(target), "content": "new", "overwrite": True})
    assert not r.ok
    call = next(d for k, d in events if k == "tool_call")
    assert call["risk"] == "high" and call["needs_approval"]
    assert target.read_text(encoding="utf-8") == "old"


async def test_tool_exception_is_reported_not_hidden(settings):
    reg = ToolRegistry()

    @salu_tool("crashy", "Crashes.", risk="low")
    async def crashy(args, ctx):
        raise RuntimeError("disk on fire")

    reg.register(crashy)
    ex, events = make_executor(settings, registry=reg)
    r = await ex.execute("crashy", {})
    assert not r.ok and "disk on fire" in r.output
    assert any(k == "tool_result" and not d["ok"] for k, d in events)


async def test_tool_output_is_redacted(settings):
    reg = ToolRegistry()

    @salu_tool("leaky", "Leaks.", risk="low")
    async def leaky(args, ctx):
        return ToolResult.success("key: sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV")

    reg.register(leaky)
    ex, _ = make_executor(settings, registry=reg)
    assert "ABCDEFGHIJ" not in (await ex.execute("leaky", {})).output


# ---- file sandbox -------------------------------------------------------------------
async def test_file_tools_respect_allowed_roots(settings, tmp_path):
    ex, _ = make_executor(settings)
    outside = Path.home()
    r = await ex.execute("list_directory", {"path": str(outside)})
    assert not r.ok and "outside the allowed folders" in r.output


async def test_sensitive_files_blocked(settings):
    sandbox = settings.root / "sandbox"
    (sandbox / ".env").write_text("SECRET=1", encoding="utf-8")
    (sandbox / ".ssh").mkdir()
    (sandbox / ".ssh" / "config").write_text("x", encoding="utf-8")
    ex, _ = make_executor(settings)
    for p in (sandbox / ".env", sandbox / ".ssh" / "config"):
        r = await ex.execute("read_text_file", {"path": str(p)})
        assert not r.ok and "protected" in r.output


async def test_read_and_list(settings):
    sandbox = settings.root / "sandbox"
    (sandbox / "notes.md").write_text("line1\nline2\n", encoding="utf-8")
    ex, _ = make_executor(settings)
    assert "notes.md" in (await ex.execute("list_directory", {"path": str(sandbox)})).output
    assert (await ex.execute("read_text_file", {"path": str(sandbox / "notes.md")})).output.startswith("line1")


async def test_system_status(settings):
    ex, _ = make_executor(settings)
    r = await ex.execute("system_status", {})
    assert r.ok and r.data["ram_total_gb"] > 0 and "cpu_percent" in r.data


# ---- memory tools ------------------------------------------------------------------
async def test_memory_refuses_secrets(settings):
    ex, _ = make_executor(settings)
    r = await ex.execute("remember", {"key": "github", "value": "token is ghp_abcdefghijklmnopqrstuvwxyz0123456789"})
    assert not r.ok and "secret" in r.output.lower()
    assert (await ex.execute("remember", {"key": "project.main", "value": "result-system", "category": "project"})).ok
    assert "result-system" in (await ex.execute("recall", {"query": "project"})).output
