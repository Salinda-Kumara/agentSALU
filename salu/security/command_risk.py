"""Risk classification for shell (PowerShell) commands.

Design rule: when in doubt, classify HIGHER. Patterns are matched against the
raw command text, including quoted strings and nested shells, so a destructive
word anywhere raises the risk. False positives only cost an extra confirmation;
false negatives could cost data.

  LOW    read-only cmdlets/aliases and known read-only native tools
  MEDIUM anything that changes state, unknown commands, file redirection,
         nested shells, script execution
  HIGH   deletion, disk/boot/registry/security/firewall/account changes,
         process killing, shutdown, obfuscated or downloaded code execution
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .permissions import Risk

_HIGH_PATTERNS: list[tuple[str, str]] = [
    (r"\b(remove-item|ri|rm|rmdir|rd|del|erase)\b", "deletes files or folders"),
    (r"\b(clear-content|clear-item|clear-recyclebin)\b", "erases content"),
    (r"\b(format-volume|clear-disk|initialize-disk|remove-partition|new-partition|resize-partition|diskpart)\b", "modifies disks or partitions"),
    (r"(^|[\s;&|])format(\.com)?\s+[a-z]:", "formats a drive"),
    (r"\b(bcdedit|bootrec|vssadmin|wbadmin)\b", "modifies boot configuration or backups"),
    (r"\bcipher(\.exe)?\s+/w", "wipes free space"),
    (r"\b(stop-computer|restart-computer|shutdown(\.exe)?|logoff)\b", "shuts down, restarts or logs off"),
    (r"\b(stop-process|taskkill(\.exe)?|kill|spps)\b", "kills processes"),
    (r"\b(stop-service|spsv)\b", "stops a service"),
    (r"\bsc(\.exe)?\s+(delete|config|stop|failure)\b", "changes a Windows service"),
    (r"\bset-executionpolicy\b", "changes the script execution policy"),
    (r"\b(set-mppreference|add-mppreference|disable-windowsoptionalfeature)\b", "changes Defender or Windows features"),
    (r"\b(netsh(\.exe)?\s+(advfirewall|firewall|interface|wlan\s+delete))|\b(new|set|remove|disable|enable)-netfirewall", "changes firewall/network configuration"),
    (r"\breg(\.exe)?\s+(delete|add|import|load|restore|copy)\b", "modifies the registry"),
    (r"\b(remove-itemproperty|set-itemproperty|new-itemproperty|rename-itemproperty)\b", "modifies the registry or item properties"),
    (r"\b(hklm|hkcu|hkey_local_machine|hkey_current_user)\b.*\b(set|new|remove)-item", "modifies the registry"),
    (r"\bnet(\.exe)?\s+(user|localgroup|share|accounts)\b", "changes accounts or shares"),
    (r"\b(new-localuser|set-localuser|remove-localuser|add-localgroupmember|remove-localgroupmember|disable-localuser)\b", "changes local accounts"),
    (r"\b(icacls|takeown|cacls)\b|\bset-acl\b", "changes file permissions or ownership"),
    (r"-verb\s+['\"]?runas", "requests administrator elevation"),
    (r"\bschtasks(\.exe)?\s+/(create|delete|change)|\b(register|unregister|set)-scheduledtask\b", "changes scheduled tasks"),
    (r"\b(uninstall-package|uninstall-module|remove-appxpackage|remove-windowscapability)\b|msiexec(\.exe)?\s+/x", "uninstalls software"),
    (r"\b(wevtutil(\.exe)?\s+cl|clear-eventlog|remove-eventlog)\b", "clears event logs"),
    (r"\b(invoke-expression|iex)\b", "executes dynamically built code"),
    (r"(^|\s)-e(nc|ncodedcommand|c)?\s+[a-z0-9+/=]{16,}", "runs an encoded (obfuscated) command"),
    (r"frombase64string|downloadstring|downloadfile|\bstart-bitstransfer\b", "downloads or decodes code"),
    (r"\b(add-type|\[system\.reflection|reflection\.assembly)\b", "compiles or loads code"),
    (r"\b(mimikatz|procdump|sekurlsa|lsass)\b", "accesses credentials"),
    (r"\b(dism(\.exe)?\s+/online\s+/(disable|remove)|sfc\s+/)", "modifies system components"),
    (r"\b(git\s+(push\s+.*--force|push\s+-f|reset\s+--hard|clean\s+-[a-z]*f))", "destructive git operation"),
    (r"\bdocker\s+(rm|rmi|system\s+prune|volume\s+(rm|prune)|container\s+(rm|prune)|image\s+(rm|prune))\b", "deletes Docker data"),
]
_HIGH_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in _HIGH_PATTERNS]

_LOW_VERBS = ("get-", "test-", "measure-", "select-", "where-", "sort-", "format-", "convertto-", "convertfrom-",
              "group-", "compare-", "resolve-", "find-", "show-", "search-")
_LOW_COMMANDS = {
    # cmdlets / aliases that only read or shape output
    "write-output", "write-host", "write-verbose", "write-warning", "write-error", "out-string", "out-host",
    "out-null", "out-default", "foreach-object", "start-sleep", "sleep",
    "join-path", "split-path", "ls", "dir", "gci", "cat", "gc", "type", "gps", "ps", "gsv", "pwd", "gl", "echo",
    "write", "where", "select", "sort", "%", "?", "ft", "fl", "fw", "measure", "gi", "gp", "gcm", "gm", "help",
    "man", "history", "h", "gal", "gv", "gdr", "findstr", "more",
    # native read-only tools
    "ipconfig", "whoami", "hostname", "systeminfo", "tasklist", "netstat", "ping", "nslookup", "tracert",
    "pathping", "where.exe", "ver", "getmac", "arp", "driverquery", "nvidia-smi", "query", "quser", "w32tm",
}
_READ_SUBCOMMANDS = {
    "git": {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "describe", "tag", "blame", "shortlog",
            "ls-files", "config", "stash list", "--version", "version"},
    "docker": {"ps", "images", "logs", "inspect", "version", "info", "stats", "top", "port", "compose ps",
               "compose logs", "compose config", "network ls", "volume ls", "image ls", "container ls"},
    "winget": {"list", "search", "show", "--version"},
    "wsl": {"-l", "--list", "--status", "--version"},
    "node": {"--version", "-v"}, "npm": {"--version", "-v", "ls", "list", "outdated", "view"},
    "python": {"--version", "-V"}, "py": {"--version", "-0", "--list"}, "pip": {"list", "show", "--version", "freeze"},
    "netsh": {"interface show", "wlan show", "interface ipv4 show"},
    "sc.exe": {"query", "qc", "queryex"},
    "reg": {"query"}, "reg.exe": {"query"},  # bare `sc` is the Set-Content alias in PowerShell 5.1
    "route": {"print"}, "wmic": set(), "schtasks": {"/query"},
}
_NESTED_SHELLS = {"powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe", "wsl", "wsl.exe", "bash",
                  "bash.exe", "wscript", "cscript", "mshta", "rundll32", "regsvr32", "start-process", "saps", "start",
                  "invoke-command", "icm", "start-job"}

_SPLIT_RE = re.compile(r"\|\||&&|[;|\n{}()]")
_REDIRECT_RE = re.compile(r"(?<![-\w])(\d|\*)?>>?\s*(?!\s*(\$null|&\d|\d))\S", re.IGNORECASE)


@dataclass
class CommandAssessment:
    risk: Risk
    reasons: list[str] = field(default_factory=list)


def _first_word(segment: str) -> tuple[str, str]:
    s = segment.strip()
    s = re.sub(r"^\$[\w:]+\s*(\+|-)?=\s*", "", s)  # $x = <command>
    s = s.lstrip("&. ").strip()
    if not s:
        return "", ""
    m = re.match(r"(\"[^\"]*\"|'[^']*'|\S+)\s*(.*)", s, re.DOTALL)
    word, rest = m.group(1), m.group(2)
    return word.strip("\"'").lower(), rest.strip().lower()


def _segment_risk(segment: str) -> tuple[Risk, str | None]:
    if re.match(r"^\s*[.,]\w", segment):
        return Risk.LOW, None  # member access after (...), e.g. ).Caption — not dot-sourcing (". .\x.ps1")
    word, rest = _first_word(segment)
    if not word:
        return Risk.LOW, None
    if word.startswith(("$", "[", "@", "-", ".", ",")) or word[0].isdigit() or segment.strip()[0] in "\"'":
        return Risk.LOW, None  # expressions / literals / variables / member access after (...)
    if "=" in word and re.match(r"^[\w.]+=", word):
        return Risk.LOW, None  # hashtable entry inside @{ ... } such as N='FreeGB'
    name = word.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if name in _NESTED_SHELLS:
        return Risk.MEDIUM, f"starts another program or shell ({name})"
    if name not in _READ_SUBCOMMANDS and name.endswith((".ps1", ".bat", ".cmd", ".vbs", ".js", ".exe", ".msi"))             and name not in _LOW_COMMANDS:
        base = name.removesuffix(".exe")
        if base not in _READ_SUBCOMMANDS:
            return Risk.MEDIUM, f"runs a program or script ({name})"
        name = base
    if name in _LOW_COMMANDS or name.startswith(_LOW_VERBS):
        return Risk.LOW, None
    if name in _READ_SUBCOMMANDS:
        allowed = _READ_SUBCOMMANDS[name]
        if any(rest == a or rest.startswith(a + " ") for a in allowed):
            return Risk.LOW, None
        return Risk.MEDIUM, f"{name} {rest.split(' ')[0] if rest else ''}".strip() + " may change state"
    return Risk.MEDIUM, f"'{name}' may change system state"


def classify_powershell(command: str) -> CommandAssessment:
    text = (command or "").strip()
    if not text:
        return CommandAssessment(Risk.LOW, [])
    risk, reasons = Risk.LOW, []
    for rx, why in _HIGH_RE:
        if rx.search(text):
            risk = Risk.HIGH
            if why not in reasons:
                reasons.append(why)
    if _REDIRECT_RE.search(text) or re.search(r"\b(out-file|set-content|add-content|sc(?!.exe)|ac|tee-object)\b", text, re.I):
        risk = max(risk, Risk.MEDIUM)
        reasons.append("writes to a file")
    for seg in _SPLIT_RE.split(text):
        seg_risk, why = _segment_risk(seg)
        if seg_risk > Risk.LOW:
            already_high = risk == Risk.HIGH
            risk = max(risk, seg_risk)
            if why and why not in reasons and not already_high:
                reasons.append(why)
    return CommandAssessment(risk, reasons)
