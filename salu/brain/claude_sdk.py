"""Claude brain via the Claude Agent SDK (runs the local Claude Code CLI).

Security model:
  * Claude Code's built-in tools (Bash, Edit, Write, WebFetch, ...) are disabled
    (`tools=[]`). Claude can only act through SALU's own tools, exposed as an
    in-process MCP server named "salu".
  * Every SALU tool call goes through ToolExecutor -> PermissionPolicy -> approval.
  * `can_use_tool` additionally denies anything outside the salu namespace.
  * User/project Claude Code settings, hooks and MCP servers are NOT loaded
    (`setting_sources=[]`, `strict_mcp_config=True`) so SALU's behaviour is
    defined only by SALU.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, CLINotFoundError,
                              PermissionResultAllow, PermissionResultDeny, ResultMessage, TextBlock,
                              create_sdk_mcp_server, tool)

from ..config import Settings
from ..tools.executor import ToolExecutor
from ..tools.registry import ToolRegistry
from .base import Brain, BrainEvent, BrainSession, HealthResult
from .claude_cli import cli_status, enforce_auth_mode, find_cli, is_api_billing

log = logging.getLogger("salu.claude")

MCP_SERVER = "salu"
MCP_PREFIX = f"mcp__{MCP_SERVER}__"


def build_mcp_server(registry: ToolRegistry, executor: ToolExecutor):
    """Expose registry tools to Claude; each call is routed through the executor."""
    sdk_tools = []
    for t in registry.all():
        if t.name in executor.policy.disabled_tools:
            continue

        def make_handler(name: str):
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                result = await executor.execute(name, args)
                payload: dict[str, Any] = {"content": [{"type": "text", "text": result.output or "(no output)"}]}
                if not result.ok:
                    payload["is_error"] = True
                return payload
            return handler

        desc = f"{t.description} [risk: {t.risk.label}]"
        sdk_tools.append(tool(t.name, desc, t.input_schema)(make_handler(t.name)))
    return create_sdk_mcp_server(name=MCP_SERVER, version="1.0.0", tools=sdk_tools)


async def _can_use_tool(tool_name: str, input_data: dict, context) -> PermissionResultAllow | PermissionResultDeny:
    if tool_name.startswith(MCP_PREFIX):
        return PermissionResultAllow()  # SALU's executor enforces the real permission policy
    log.warning("blocked non-SALU tool request: %s", tool_name)
    return PermissionResultDeny(message=f"{tool_name} is not available. Use SALU tools only.")


class ClaudeSdkSession(BrainSession):
    """One persistent Claude conversation, owned by a dedicated asyncio task.

    The SDK client must be used from the task that connected it, so a single
    worker task owns it and requests are passed in through a queue.
    """

    def __init__(self, brain: "ClaudeSdkBrain", executor: ToolExecutor, registry: ToolRegistry, system_prompt: str):
        self.brain = brain
        self.executor = executor
        self.registry = registry
        self.system_prompt = system_prompt
        self.sdk_session_id: str | None = None
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._client: ClaudeSDKClient | None = None

    def _options(self) -> ClaudeAgentOptions:
        cfg = self.brain.settings.claude
        return ClaudeAgentOptions(
            system_prompt=self.system_prompt,
            tools=[],
            mcp_servers={MCP_SERVER: build_mcp_server(self.registry, self.executor)},
            strict_mcp_config=True,
            setting_sources=[],
            can_use_tool=_can_use_tool,
            permission_mode="default",
            model=cfg.model or None,
            max_turns=cfg.max_turns,
            cli_path=self.brain.cli_path,
            cwd=str(self.brain.settings.root),
            resume=self.sdk_session_id,  # reconnects continue the same conversation
            stderr=lambda line: log.debug("claude-cli: %s", line.rstrip()),
        )

    async def send(self, text: str) -> AsyncIterator[BrainEvent]:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="salu-claude-session")
        out: asyncio.Queue = asyncio.Queue()
        await self._inbox.put((text, out))
        while True:
            ev = await out.get()
            if ev is None:
                return
            yield ev

    async def _run(self) -> None:
        timeout = self.brain.settings.claude.request_timeout_seconds
        try:
            while True:
                text, out = await self._inbox.get()
                if text is None:
                    return
                try:
                    if self._client is None:
                        self._client = ClaudeSDKClient(self._options())
                        await self._client.connect()
                        log.info("Claude session connected resume=%s", bool(self.sdk_session_id))
                    await asyncio.wait_for(self._turn(self._client, text, out), timeout=timeout)
                except asyncio.TimeoutError:
                    await out.put(BrainEvent("error", f"Claude did not finish within {timeout}s."))
                    await self._reset()
                except CLINotFoundError as e:
                    await out.put(BrainEvent("error", f"Claude Code CLI not found: {e}"))
                    await self._reset()
                except Exception as e:
                    log.exception("Claude session error")
                    await out.put(BrainEvent("error", f"Claude connection error: {type(e).__name__}: {e}"))
                    await self._reset()
                finally:
                    await out.put(None)
        finally:
            await self._reset()

    async def _turn(self, client: ClaudeSDKClient, text: str, out: asyncio.Queue) -> None:
        await client.query(text)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                if getattr(msg, "error", None):
                    await out.put(BrainEvent("error", f"Claude error: {msg.error}"))
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        await out.put(BrainEvent("text", block.text))
            elif isinstance(msg, ResultMessage):
                self.sdk_session_id = msg.session_id or self.sdk_session_id
                usage = msg.usage or {}
                data = {"turns": msg.num_turns, "duration_ms": msg.duration_ms,
                        "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                        "subtype": msg.subtype}
                log.info("Claude turn finished subtype=%s turns=%s duration_ms=%s tokens_in=%s tokens_out=%s",
                         msg.subtype, msg.num_turns, msg.duration_ms, data["input_tokens"], data["output_tokens"])
                if msg.is_error:
                    reason = msg.result or getattr(msg, "terminal_reason", None) or msg.subtype
                    await out.put(BrainEvent("error", f"Claude reported an error: {reason}", data))
                else:
                    await out.put(BrainEvent("done", msg.result or "", data))

    async def _reset(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log.debug("disconnect failed", exc_info=True)

    async def interrupt(self) -> None:
        if self._client is not None:
            try:
                await self._client.interrupt()
            except Exception as e:
                log.warning("interrupt failed: %s", e)

    async def close(self) -> None:
        if self._worker and not self._worker.done():
            await self._inbox.put((None, None))
            try:
                await asyncio.wait_for(self._worker, timeout=10)
            except (asyncio.TimeoutError, Exception):
                self._worker.cancel()


class ClaudeSdkBrain(Brain):
    name = "claude-agent-sdk"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.removed_env = enforce_auth_mode(settings.claude.auth_mode)
        self.cli_path = find_cli(settings.claude.cli_path)

    async def health_check(self) -> HealthResult:
        st = await cli_status(self.settings.claude.cli_path)
        info = {"cli": st.path, "cli_version": st.version, "auth_method": st.auth_method,
                "subscription": st.subscription, "provider": st.provider,
                "auth_mode": self.settings.claude.auth_mode, "model": self.settings.claude.model}
        if st.error:
            return HealthResult(False, st.error, info)
        if not st.logged_in:
            return HealthResult(False, "Claude Code is not logged in. Run `claude` and use /login "
                                       "(or `claude auth login`) with your Claude Pro account.", info)
        mode = self.settings.claude.auth_mode
        if mode == "subscription" and is_api_billing(st.auth_method):
            return HealthResult(False, f"Claude Code is authenticated via '{st.auth_method}' (API billing), "
                                       "but SALU is in subscription mode. Log in with your claude.ai account.", info)
        if mode == "subscription" and st.provider not in (None, "firstParty"):
            return HealthResult(False, f"Claude Code is configured for provider '{st.provider}', not your subscription.", info)
        label = f"{st.subscription or 'subscription'} via {st.auth_method}" if mode == "subscription" else f"API key ({st.auth_method})"
        return HealthResult(True, f"Claude Code {st.version} · {label}", info)

    def new_session(self, executor: ToolExecutor, registry: ToolRegistry, system_prompt: str) -> ClaudeSdkSession:
        return ClaudeSdkSession(self, executor, registry, system_prompt)
