"""Terminal chat interface (python -m salu chat)."""

from __future__ import annotations

import asyncio
import sys

from .app import SaluApp
from .security.permissions import ApprovalRequest

C = {"dim": "\033[2m", "cyan": "\033[96m", "green": "\033[92m", "red": "\033[91m", "yellow": "\033[93m",
     "bold": "\033[1m", "reset": "\033[0m"}


def _c(color: str, text: str) -> str:
    return f"{C[color]}{text}{C['reset']}" if sys.stdout.isatty() else text


def print_checks(salu: SaluApp) -> None:
    print(_c("bold", "\n  S A L U"))
    print(_c("dim", "  Personal AI Assistant\n"))
    for c in salu.checks:
        mark = _c("green", "✓") if c.ok else _c("red", "✗")
        print(f"  {mark} {c.name:<18} {_c('dim', c.detail)}")
    print()
    print("  " + (_c("green", "SYSTEM ONLINE") if salu.online else _c("yellow", "SYSTEM DEGRADED — see failed checks")))
    print()


async def _ainput(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def run_chat(salu: SaluApp) -> int:
    await salu.startup_checks()
    print_checks(salu)
    if not salu.online:
        claude = next((c for c in salu.checks if c.name == "Claude connection"), None)
        if claude and not claude.ok:
            return 1

    async def approve(req: ApprovalRequest) -> None:
        color = "red" if req.risk.label == "high" else "yellow"
        print("\n" + _c(color, f"  ┌─ SALU CONFIRMATION · {req.risk.label.upper()} RISK"))
        print(f"  │ {req.summary}\n  │ Tool: {req.tool}")
        for d in req.details:
            print(f"  │ • {d}")
        ans = (await _ainput(_c(color, "  └─ Proceed? [y/N] "))).strip().lower()
        salu.broker.resolve(req.id, ans in ("y", "yes"))

    salu.broker.attach(approve)

    async def sink(kind: str, data: dict) -> None:
        if kind == "text":
            print(f"\n{_c('cyan', 'SALU')}  {data['text']}")
        elif kind == "tool_call":
            print(_c("dim", f"  → {data['tool']} [{data['risk']}]"))
        elif kind == "tool_result":
            mark = "✓" if data["ok"] else "✗"
            print(_c("dim" if data["ok"] else "red", f"  {mark} {data['tool']} ({data['duration']}s)"
                     + ("" if data["ok"] else f": {data['output'][:300]}")))
        elif kind == "error":
            print(_c("red", f"\nTask failed.\n{data['message']}"))

    print(_c("dim", "  Type your request. /new = new conversation, /quit = exit.\n"))
    try:
        while True:
            try:
                text = (await _ainput(_c("bold", "\nYou › "))).strip()
            except EOFError:
                break
            if not text:
                continue
            if text in ("/quit", "/exit"):
                break
            if text == "/new":
                await salu.agent.reset_session()
                print(_c("dim", "  New conversation."))
                continue
            await salu.agent.handle(text, sink)
    except KeyboardInterrupt:
        pass
    finally:
        await salu.shutdown()
    return 0


async def run_check(salu: SaluApp) -> int:
    await salu.startup_checks()
    print_checks(salu)
    await salu.shutdown()
    return 0 if salu.online else 1
