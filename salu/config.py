"""Configuration loading: config/settings.yaml + .env + SALU_* environment overrides.

Secrets never live in settings.yaml. They come from the environment (or .env),
and SALU's own code only ever checks whether they are *present*.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_MODES = ("subscription", "api_key")


class ConfigError(Exception):
    pass


@dataclass
class ClaudeConfig:
    auth_mode: str = "subscription"
    model: str | None = "sonnet"
    cli_path: str | None = None
    max_turns: int = 20
    request_timeout_seconds: int = 300


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    open_window: bool = True


@dataclass
class SecurityConfig:
    auto_approve: list[str] = field(default_factory=lambda: ["low"])
    approval_timeout_seconds: int = 120
    risk_overrides: dict[str, str] = field(default_factory=dict)
    disabled_tools: list[str] = field(default_factory=list)


@dataclass
class ToolsConfig:
    allowed_roots: list[str] = field(default_factory=list)
    plugin_dirs: list[str] = field(default_factory=lambda: ["plugins"])


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/salu.log"
    max_bytes: int = 2_000_000
    backup_count: int = 5


@dataclass
class Settings:
    assistant_name: str = "SALU"
    user_name: str = ""
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    memory_database: str = "data/salu.db"
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    root: Path = PROJECT_ROOT

    def path(self, p: str) -> Path:
        """Resolve a config path relative to the project root."""
        pp = Path(os.path.expandvars(os.path.expanduser(p)))
        return pp if pp.is_absolute() else (self.root / pp)

    def allowed_roots(self) -> list[Path]:
        roots = self.tools.allowed_roots or [str(Path.home())]
        return [self.path(r).resolve() for r in roots]

    def public_view(self) -> dict[str, Any]:
        """Settings safe to show in the UI (contains no secrets by design)."""
        return {
            "assistant": {"name": self.assistant_name, "user_name": self.user_name},
            "claude": vars(self.claude),
            "server": vars(self.server),
            "security": vars(self.security),
            "tools": {"allowed_roots": [str(p) for p in self.allowed_roots()],
                      "plugin_dirs": self.tools.plugin_dirs},
            "memory": {"database": self.memory_database},
            "logging": vars(self.logging),
        }


def _section(data: dict, key: str) -> dict:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"settings.yaml: '{key}' must be a mapping")
    return value


def _build(cls, values: dict, section: str):
    known = set(cls.__dataclass_fields__)
    unknown = set(values) - known
    if unknown:
        raise ConfigError(f"settings.yaml: unknown keys in '{section}': {sorted(unknown)}")
    return cls(**values)


def load_settings(config_file: Path | None = None, env_file: Path | None = None,
                  root: Path = PROJECT_ROOT) -> Settings:
    env_path = env_file if env_file is not None else root / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    cfg_path = config_file or root / "config" / "settings.yaml"
    data: dict = {}
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError("settings.yaml must contain a mapping at top level")

    assistant = _section(data, "assistant")
    s = Settings(
        assistant_name=str(assistant.get("name") or "SALU"),
        user_name=str(assistant.get("user_name") or ""),
        claude=_build(ClaudeConfig, _section(data, "claude"), "claude"),
        server=_build(ServerConfig, _section(data, "server"), "server"),
        security=_build(SecurityConfig, _section(data, "security"), "security"),
        tools=_build(ToolsConfig, _section(data, "tools"), "tools"),
        memory_database=str(_section(data, "memory").get("database") or "data/salu.db"),
        logging=_build(LoggingConfig, _section(data, "logging"), "logging"),
        root=root,
    )

    env = os.environ
    if v := env.get("SALU_CLAUDE_AUTH_MODE"):
        s.claude.auth_mode = v.strip().lower()
    if v := env.get("SALU_CLAUDE_MODEL"):
        s.claude.model = v.strip()
    if v := env.get("SALU_CLAUDE_CLI_PATH"):
        s.claude.cli_path = v.strip()
    if v := env.get("SALU_PORT"):
        s.server.port = int(v)
    if v := env.get("SALU_LOG_LEVEL"):
        s.logging.level = v.strip().upper()

    validate(s)
    return s


def validate(s: Settings) -> None:
    if s.claude.auth_mode not in AUTH_MODES:
        raise ConfigError(f"claude.auth_mode must be one of {AUTH_MODES}, got {s.claude.auth_mode!r}")
    if s.server.host not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("server.host must be a loopback address; SALU must not be exposed to the network")
    levels = {"low", "medium", "high"}
    bad = set(s.security.auto_approve) - levels
    if bad:
        raise ConfigError(f"security.auto_approve has invalid levels: {sorted(bad)}")
    for tool, level in s.security.risk_overrides.items():
        if level not in levels:
            raise ConfigError(f"security.risk_overrides.{tool}: invalid level {level!r}")
