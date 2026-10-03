"""SaluApp: wires all components together and runs the real startup checks."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass

from . import __version__
from .agent import Agent
from .brain import Brain, create_brain
from .browser import browser
from .browser.manager import find_browser_executable
from .config import Settings, load_settings
from .logging_setup import setup_logging
from .memory import MemoryStore
from .security.permissions import ApprovalBroker, PermissionPolicy, Risk
from .tools.registry import ToolRegistry, build_registry

log = logging.getLogger("salu")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class SaluApp:
    def __init__(self, settings: Settings | None = None, brain: Brain | None = None, console_log: bool = True):
        self.settings = settings or load_settings()
        s = self.settings
        setup_logging(s.logging.level, s.path(s.logging.file), s.logging.max_bytes, s.logging.backup_count,
                      console=console_log)
        log.info("SALU %s starting", __version__)
        self.memory = MemoryStore(s.path(s.memory_database))
        self.registry: ToolRegistry = build_registry([s.path(d) for d in s.tools.plugin_dirs])
        self.policy = PermissionPolicy(s.security.auto_approve, s.security.risk_overrides, s.security.disabled_tools)
        self.broker = ApprovalBroker(s.security.approval_timeout_seconds)
        self.brain: Brain = brain or create_brain(s)
        self.agent = Agent(s, self.brain, self.registry, self.policy, self.broker, self.memory)
        browser.configure(s)
        self.checks: list[Check] = []
        self.brain_info: dict = {}
        self.online = False

    async def startup_checks(self) -> list[Check]:
        """Each check really exercises its component; nothing is displayed as OK unless it is."""
        checks: list[Check] = []

        async def run(name, fn):
            t = time.perf_counter()
            try:
                ok, detail = await fn()
            except Exception as e:
                log.exception("startup check failed: %s", name)
                ok, detail = False, f"{type(e).__name__}: {e}"
            checks.append(Check(name, ok, detail, round(time.perf_counter() - t, 2)))
            log.log(logging.INFO if ok else logging.ERROR, "startup check %s: %s - %s",
                    name, "OK" if ok else "FAILED", detail)

        async def agent_runtime():
            ok = self.agent.state == "idle" and self.agent.brain is self.brain
            return ok, f"runtime ready · provider {self.brain.name}"

        async def claude():
            h = await self.brain.health_check()
            self.brain_info = h.info
            return h.ok, h.detail

        async def tools():
            n = len(self.registry)
            errs = self.registry.load_errors
            if n == 0:
                return False, "no tools registered"
            detail = f"{n} tools · " + ", ".join(sorted({t.category for t in self.registry.all()}))
            if errs:
                return False, f"{detail} · {len(errs)} load error(s): {errs[0]}"
            return True, detail

        async def memory():
            self.memory.self_test()
            interrupted = self.memory.mark_interrupted_tasks()
            extra = f" · {interrupted} interrupted task(s) marked failed" if interrupted else ""
            return True, f"SQLite OK ({self.settings.memory_database}){extra}"

        async def security():
            problems = []
            if Risk.HIGH in self.policy.auto_approve:
                problems.append("HIGH risk is auto-approved")
            probe = self.policy.evaluate("__probe__", Risk.HIGH)
            if not probe.needs_approval:
                problems.append("HIGH risk does not require approval")
            for t in self.registry.all():
                if not isinstance(t.risk, Risk):
                    problems.append(f"{t.name} has no risk level")
            auto = ",".join(sorted(r.label for r in self.policy.auto_approve)) or "none"
            removed = getattr(self.brain, "removed_env", [])
            note = f" · removed {', '.join(removed)} (no API billing)" if removed else ""
            if problems:
                return False, "; ".join(problems)
            return True, f"permission gate active · auto-approve: {auto} · high: always confirm{note}"

        await run("Agent runtime", agent_runtime)
        await run("Claude connection", claude)
        await run("Tool registry", tools)
        async def browser_check():
            try:
                import playwright  # noqa: F401
            except ImportError:
                return False, "Playwright not installed (pip install playwright)"
            exe = find_browser_executable(self.settings.browser.channel)
            if not exe:
                return False, f"{self.settings.browser.channel} not found; set browser.channel in settings.yaml"
            mode = "headless" if self.settings.browser.headless else "visible"
            return True, f"Playwright ready · {self.settings.browser.channel} ({mode}, starts on first use)"

        await run("Memory", memory)
        await run("Browser", browser_check)
        await run("Security system", security)
        self.checks = checks
        self.online = all(c.ok for c in checks)
        log.info("SYSTEM %s", "ONLINE" if self.online else "DEGRADED")
        return checks

    async def shutdown(self) -> None:
        await self.agent.shutdown()
        await browser.close()
        self.memory.close()
        log.info("SALU stopped")
