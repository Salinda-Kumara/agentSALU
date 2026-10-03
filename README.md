# SALU — Personal AI Assistant

SALU is a personal, JARVIS-style AI assistant for Windows, with Claude as its brain.
It understands a request, plans the work, uses tools through a permission system,
checks the result and reports back.

**Current status: Phase 1 (core).** The working parts are the Claude brain, the agent
runtime, the tool registry, permissions, memory, logging, the desktop UI and a
terminal chat. Computer control, browsing, dev tools, servers, voice and automation
come in later phases (see [Roadmap](#roadmap)).

---

## Quick start

```powershell
cd F:\agentSALU
.\start-salu.bat              # desktop window (or: .venv\Scripts\python -m salu)
```

Other modes:

```powershell
.venv\Scripts\python -m salu chat          # terminal chat
.venv\Scripts\python -m salu check         # run the startup checks only
.venv\Scripts\python -m salu --no-window   # server only; open the printed URL yourself
```

The desktop window is Edge (or Chrome) in app mode, served from `127.0.0.1:8765`.
Each launch prints a URL containing a one-time access token. Stop SALU with `Ctrl+C`.

## Setup (fresh machine)

Requirements: Windows 10/11, Python 3.11+ and Claude Code logged in to your Claude account.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env          # optional; the defaults are fine
.venv\Scripts\python -m salu check
```

**Claude Code CLI.** SALU runs the official Claude Code CLI. It looks for the CLI in this order:

1. `claude.cli_path` in settings
2. `claude.exe` on PATH
3. `~/.local/bin`
4. the SDK's bundled copy
5. the newest VS Code extension binary

For a stable setup, install the standalone CLI and log in once:

```powershell
irm https://claude.ai/install.ps1 | iex     # official native installer
claude                                      # then /login with your Claude Pro account
```

---

## Claude authentication and billing

SALU uses the **Claude Agent SDK**, which drives the local **Claude Code CLI**. The CLI
authenticates with whatever login you made through Claude Code's own sign-in flow. With
a Claude Pro login, SALU's requests count against your **Pro plan usage limits**. There is no
separate API bill.

Safeguards:

- **SALU never reads, copies or stores your Claude credentials.** At startup it only
  asks the CLI for its auth *status*: logged in, method, plan.
- **No silent API billing.** In `subscription` mode, SALU removes these variables from
  its own process before starting the CLI, because Claude Code would otherwise prefer an API key:
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY`,
  `ANTHROPIC_PROFILE`.
  The startup check also fails if the CLI reports an API-key login.
- API-key billing only happens if you set `SALU_CLAUDE_AUTH_MODE=api_key` yourself.

**Policy note.** Anthropic's docs ([Legal & compliance](https://code.claude.com/docs/en/legal-and-compliance),
[Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)) say that Pro/Max
limits "assume ordinary, individual usage of Claude Code and the Agent SDK". They also say
that developers *building products or services for others* must use API keys and must not
offer claude.ai login to their users.

SALU is designed as a **single-user, personal tool**: you run the unmodified Claude Code
binary under your own login, on your own machine. Keep it that way. Don't host SALU for
other people or share access to it. Heavy unattended automation (Phases 8–9) may go beyond
"ordinary individual usage". For that, switch to `api_key` mode or contact Anthropic.

---

## Architecture

```text
 Desktop UI (Edge app window)          Terminal chat
   salu/web/static/*                     salu/cli.py
          │ WebSocket + token                 │
          └──────────────┬────────────────────┘
                 ┌───────▼────────┐
                 │ Agent runtime  │  salu/agent/      tasks, sessions, events, history
                 └───────┬────────┘
                 ┌───────▼────────┐
                 │  Brain (ABC)   │  salu/brain/      provider-neutral interface
                 │ ClaudeSdkBrain │  claude_sdk.py    Claude Agent SDK -> Claude Code CLI
                 └───────┬────────┘
                         │ in-process MCP server "salu" (only SALU tools exposed)
                 ┌───────▼────────┐
                 │  ToolExecutor  │  salu/tools/executor.py
                 │ validate → risk → PermissionPolicy → approval → run → log → verify
                 └───────┬────────┘
            ┌────────────┼─────────────┐
       ToolRegistry   Security       Memory (SQLite)
       builtin/ +     permissions    tasks · messages · facts
       plugins/       redaction      (secrets refused)
```

| Path | Purpose |
|---|---|
| `salu/config.py` | `config/settings.yaml` + `.env` + `SALU_*` overrides, validated |
| `salu/logging_setup.py` | structured logs (`logs/salu.log`); every record passes secret redaction |
| `salu/security/` | risk levels, permission policy, approval broker, secret redaction |
| `salu/tools/` | `Tool` contract, registry with plugin discovery, executor (the single gate) |
| `salu/brain/` | `Brain` interface, Claude CLI locator/auth checks, Claude Agent SDK provider |
| `salu/agent/` | runtime + system prompt (SALU personality and rules) |
| `salu/memory/` | SQLite store |
| `salu/app.py` | wires components; **real** startup checks |
| `salu/web/` | FastAPI server + UI |
| `plugins/` | drop-in tool plugins (see `plugins/README.md`) |

### How Claude is sandboxed

- Claude Code's built-in tools are **disabled** (`tools=[]`): no Bash, file edit or web fetch.
  Claude can only act through SALU's tools.
- Your personal Claude Code settings, hooks and MCP servers are **not loaded**
  (`setting_sources=[]`, `strict_mcp_config=True`). SALU's behaviour is defined only by SALU.
- `can_use_tool` denies any tool outside the `salu` namespace.
- Every tool call goes through `ToolExecutor`. That gate lives in the core, not in the AI
  provider, so a future provider can't bypass it.

## Security and permissions

| Risk | Behaviour | Examples (Phase 1) |
|---|---|---|
| **LOW** | runs automatically | `system_status`, `current_datetime`, `list_directory`, `read_text_file`, `remember`, `recall` |
| **MEDIUM** | asks for confirmation (configurable) | `write_text_file` (new file), `forget` |
| **HIGH** | **always** asks; can't be configured away | `write_text_file` with overwrite |

These rules are enforced in code:

- Risk overrides can only *raise* risk, never lower it.
- A tool can raise its own risk based on its arguments (an overwrite becomes HIGH).
- Each confirmation times out after 120 s and counts as denied.
- If no window is connected, every confirmation is denied.

Other protections:

- **File sandbox.** File tools only reach `tools.allowed_roots` (default: your home folder).
  Credential locations are always blocked: `.ssh`, `.aws`, `.env`, `*.pem`, `id_rsa*`,
  `.credentials.json`, and similar. Phase 1 has no delete tool.
- **Secrets.** Secrets are redacted from logs, tool output and history. Memory refuses
  anything that looks like a password, key or token.
- **Local only.** The server only binds to loopback (validated). The API needs the
  per-launch token. WebSocket connections must come from SALU's own origin, so other
  websites can't drive SALU through localhost.
- **External services.** These are the only things SALU contacts:
  - Anthropic, via the Claude Code CLI.
  - `1.1.1.1:443`: a TCP connect, at most every 30 s, for the "network online" indicator. No data is sent.

## Configuration

`config/settings.yaml` holds behaviour; `.env` holds secrets and overrides. See `.env.example`.
Common settings:

- `claude.model`: `sonnet` by default (easier on Pro limits). `opus` or a full model ID also work.
- `tools.allowed_roots`: e.g. `["F:/", "~/Documents"]`
- `security.auto_approve: [low, medium]`: skips confirmation for medium-risk actions. HIGH always asks.
- `assistant.user_name`: the name SALU uses to greet you.

## Adding a tool

Drop a file into `plugins/`:

```python
from salu.tools import ToolResult, salu_tool

@salu_tool("disk_free", "Free space on a drive.", risk="low", category="system",
           properties={"drive": {"type": "string"}}, required=["drive"])
async def disk_free(args, ctx):
    import shutil
    u = shutil.disk_usage(args["drive"])
    return ToolResult.success(f"{u.free / 1e9:.1f} GB free")

TOOLS = [disk_free]
```

Restart SALU and the tool is registered, shown to Claude and gated by its risk level.

## Tests

```powershell
.venv\Scripts\python -m pytest           # offline suite (fake brain, no Claude usage)
.venv\Scripts\python -m pytest -m live   # one real Claude round trip (uses a little Pro quota)
```

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Core: Claude brain, runtime, tool registry, permissions, memory, logging, UI | **done** |
| 2 | Windows control: PowerShell, app launching, file operations, screenshots | next |
| 3 | Browser agent (Playwright): navigation, search, automation | |
| 4 | Developer agent: Git, GitHub, Docker, VS Code, Python, Node | |
| 5 | Server agent: SSH, Linux, Docker on VPS, logs, deployment | |
| 6 | Voice: faster-whisper STT, TTS, "Hey SALU" wake word (local, on-device) | |
| 7 | Memory: richer knowledge, conversation summaries, optional vector search | |
| 8 | Automation: n8n, scheduler, notifications (Telegram/email), monitoring | |
| 9 | Advanced: specialised sub-agents, background and long-running tasks | |

Planned design points for later phases:

- **Phase 2:** PowerShell gets per-command risk classification. Read-only cmdlets are LOW;
  anything else is MEDIUM or HIGH. Destructive patterns are always HIGH.
- **MCP:** only well-known, reviewed servers will be added (for example the official GitHub
  server in Phase 4), each with its tools mapped onto SALU risk levels.
- **Sub-agents:** the Agent SDK supports subagents. The Developer, System and Research
  agents will run behind the same `ToolExecutor`.
