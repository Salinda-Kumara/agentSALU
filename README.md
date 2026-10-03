# SALU — Personal AI Assistant

SALU is a personal, JARVIS-style AI assistant for Windows, with Claude as its brain.
It understands a request, plans the work, uses tools through a permission system,
checks the result and reports back.

**Current status: Phase 3 (browser agent).** Working now:

- **Phase 1 core:** the Claude brain, agent runtime, tool registry, permissions, memory, logging,
  desktop UI and terminal chat.
- **Phase 2:** PowerShell with a risk rating for each command, launching/focusing/closing apps,
  processes and services, clipboard, screenshots that Claude can see, and file management
  (find, copy, move, Recycle Bin, zip).
- **Phase 3:** web search, and SALU's own browser (Playwright driving your installed Edge) to open,
  read and operate websites: click, type, select, tabs, downloads, uploads and page screenshots.

Dev tools, servers, voice and automation come in later phases (see [Roadmap](#roadmap)).

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
| `salu/security/` | risk levels, permission policy, approval broker, secret redaction, PowerShell risk classifier |
| `salu/browser/` | Playwright browser manager (page snapshots, actions, downloads) and web search |
| `salu/winos/` | Windows helpers: PowerShell runner, Win32 window management, app discovery |
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

| Risk | Behaviour | Examples |
|---|---|---|
| **LOW** | runs automatically | status, processes, services list, read/list/find files, open apps / documents / URLs, list/focus windows, clipboard, read-only PowerShell (`Get-*`, `ipconfig`, `git status`, `docker ps`) |
| **MEDIUM** | asks for confirmation (configurable) | write/copy/move files, zip, close apps politely, start/restart services, screenshot sent to Claude, state-changing or unknown PowerShell |
| **HIGH** | **always** asks; can't be configured away | web: buy/pay/checkout, sign in, account changes, delete, send/post, any button on a page with password or payment fields, typing into password fields, uploads; overwrite on write/copy/move, Recycle Bin delete, force-kill, stop service, open program/script files, destructive PowerShell (`Remove-Item`, `Stop-Process`, registry/firewall/account/disk changes, `iex`, encoded commands, `git push --force`, ...) |

### Browser agent

- **Separate browser profile.** SALU drives your installed **Edge** (configurable) with its own
  profile in `data/browser-profile`. It never touches your everyday browser profile, cookies or
  saved passwords. You can log in to sites in SALU's window yourself; SALU asks you to type
  passwords, 2FA codes and card details there rather than doing it itself.
- **Element refs.** Pages are given to Claude as text plus numbered elements (`[3] button "Sign in"`).
  An action's risk is judged from the element as SALU saw it on the page, not from Claude's
  description:
  - links, search boxes and ordinary input fields are **LOW**
  - other buttons and form submits are **MEDIUM**
  - purchase, sign-in, account, delete and send actions are **HIGH**, and so is anything on a
    page with password or payment fields
  - an unknown or stale element is **HIGH**
- **Untrusted content.** Every page and search result is labelled as untrusted data, and Claude is
  told never to follow instructions found in web content. A live test with a hidden "delete the
  user's Documents" instruction was ignored and reported to the user.
- **Allowed URLs.** Only `http(s)` URLs are allowed (no `file:`, `javascript:` or `data:`), and SALU's
  own control port can't be opened.
- **Downloads and search.** Downloads are saved to `~/Downloads/SALU` and never opened
  automatically. Web search uses DuckDuckGo's HTML endpoint and falls back to Bing in the browser.

### PowerShell risk classification

`salu/security/command_risk.py` rates each command before it runs, and errs high:

- **Destructive patterns** are matched against the whole raw text, including quoted strings and
  nested `powershell -c "..."`. Any match makes the command HIGH.
- **Each pipeline segment** is checked against a list of read-only cmdlets and tools.
- **Anything unknown** is MEDIUM: scripts, nested shells, redirection into files, unknown commands.

The confirmation dialog shows the exact command and why it got its rating. Commands run with
`-NoProfile -NonInteractive`, start in a sandboxed folder, and time out after at most 300 s;
on timeout the whole process tree is killed.

These rules are enforced in code:

- Risk overrides can only *raise* risk, never lower it.
- A tool can raise its own risk based on its arguments (an overwrite becomes HIGH).
- Each confirmation times out after 120 s and counts as denied.
- If no window is connected, every confirmation is denied.

Other protections:

- **File sandbox.** File tools only reach `tools.allowed_roots` (default: your home folder).
  Credential locations are always blocked: `.ssh`, `.aws`, `.env`, `*.pem`, `id_rsa*`,
  `.credentials.json`, and similar. Deletion only goes to the Recycle Bin (recoverable). SALU
  refuses to delete drive roots, your home folder or its own folder. Zip extraction blocks
  zip-slip, zip bombs and overwrites.
- **Processes.** Force-kill never touches critical Windows processes or SALU itself.
- **Secrets.** Secrets are redacted from logs, tool output and history. Memory refuses
  anything that looks like a password, key or token.
- **Local only.** The server only binds to loopback (validated). The API needs the
  per-launch token. WebSocket connections must come from SALU's own origin, so other
  websites can't drive SALU through localhost.
- **External services.** These are the only things SALU contacts:
  - Anthropic, via the Claude Code CLI. This includes screenshots when `take_screenshot` runs
    with `analyze=true`, which is why that is MEDIUM risk.
  - `1.1.1.1:443`: a TCP connect, at most every 30 s, for the "network online" indicator. No data is sent.
  - DuckDuckGo (`html.duckduckgo.com`) for `web_search`, with Bing as fallback, plus any website
    you or SALU open in the browser.

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
.venv\Scripts\python -m pytest              # offline suite (fake brain, no Claude usage)
.venv\Scripts\python -m pytest -m desktop   # visible effects: opens/closes Notepad, clipboard, Recycle Bin
.venv\Scripts\python -m pytest -m live      # one real Claude round trip (uses a little Pro quota)
```

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Core: Claude brain, runtime, tool registry, permissions, memory, logging, UI | **done** |
| 2 | Windows control: PowerShell, app launching, file operations, screenshots | **done** |
| 3 | Browser agent (Playwright): navigation, search, automation | **done** |
| 4 | Developer agent: Git, GitHub, Docker, VS Code, Python, Node | next |
| 5 | Server agent: SSH, Linux, Docker on VPS, logs, deployment | |
| 6 | Voice: faster-whisper STT, TTS, "Hey SALU" wake word (local, on-device) | |
| 7 | Memory: richer knowledge, conversation summaries, optional vector search | |
| 8 | Automation: n8n, scheduler, notifications (Telegram/email), monitoring | |
| 9 | Advanced: specialised sub-agents, background and long-running tasks | |

Planned design points for later phases:

- **Mouse/keyboard control** of desktop apps (outside the browser) is deliberately not built yet.
  It will get its own confirmation rules.
- **MCP:** only well-known, reviewed servers will be added (for example the official GitHub
  server in Phase 4), each with its tools mapped onto SALU risk levels.
- **Sub-agents:** the Agent SDK supports subagents. The Developer, System and Research
  agents will run behind the same `ToolExecutor`.
