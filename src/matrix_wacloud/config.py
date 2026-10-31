"""Configuration loading.

The YAML file may reference environment variables as ``${NAME}`` (required) or
``${NAME:-default}`` (optional). This keeps secrets in a ``.env`` file / the
container environment instead of in the YAML.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def _expand(value: Any, path: str = "") -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"Environment variable {name} is not set (used in '{path}')")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v, f"{path}.{k}" if path else str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


@dataclass
class HomeserverConfig:
    address: str
    domain: str
    verify_ssl: bool = True


@dataclass
class AppServiceConfig:
    id: str
    bot_localpart: str
    as_token: str
    hs_token: str
    address: str  # URL the homeserver uses to reach this service
    hostname: str = "0.0.0.0"
    port: int = 29330
    bot_displayname: str = "Avisos WhatsApp"


@dataclass
class WhatsAppConfig:
    phone_number_id: str
    access_token: str
    verify_token: str
    app_secret: str = ""
    api_base: str = "https://graph.facebook.com"
    api_version: str = "v26.0"
    webhook_path: str = "/whatsapp/webhook"
    default_template: str = "hello_world"
    default_language: str = "en_US"
    # How the template body parameters ({{1}}, {{2}}, ...) are filled, in order.
    # Supported tokens: "label" (room label), "text" (message text),
    # "sender" (display name of who posted). ``body_params`` is the default;
    # ``template_params`` overrides it per template (empty list = no params).
    body_params: list[str] = field(default_factory=lambda: ["label", "text"])
    template_params: dict[str, list[str]] = field(default_factory=lambda: {"hello_world": []})
    max_concurrency: int = 5
    max_retries: int = 3

    def params_for(self, template: str) -> list[str]:
        return self.template_params.get(template, self.body_params)


@dataclass
class Config:
    homeserver: HomeserverConfig
    appservice: AppServiceConfig
    whatsapp: WhatsAppConfig
    database: str = "matrix-wacloud.db"
    state_file: str = "mx-state.json"
    admins: list[str] = field(default_factory=list)
    log_level: str = "INFO"

    @property
    def bot_mxid(self) -> str:
        return f"@{self.appservice.bot_localpart}:{self.homeserver.domain}"


def _section(raw: dict, name: str, cls):
    data = raw.get(name)
    if not isinstance(data, dict):
        raise ConfigError(f"Missing section '{name}'")
    try:
        return cls(**data)
    except TypeError as e:
        raise ConfigError(f"Invalid section '{name}': {e}") from None


def parse_config(raw: dict) -> Config:
    raw = _expand(raw)
    cfg = Config(
        homeserver=_section(raw, "homeserver", HomeserverConfig),
        appservice=_section(raw, "appservice", AppServiceConfig),
        whatsapp=_section(raw, "whatsapp", WhatsAppConfig),
        database=raw.get("database", "matrix-wacloud.db"),
        state_file=raw.get("state_file", "mx-state.json"),
        admins=list(raw.get("admins") or []),
        log_level=str(raw.get("log_level", "INFO")).upper(),
    )
    all_lists = [cfg.whatsapp.body_params, *cfg.whatsapp.template_params.values()]
    for tok in (t for lst in all_lists for t in (lst or [])):
        if tok not in ("label", "text", "sender"):
            raise ConfigError(f"Unknown template parameter token: {tok!r}")
    cfg.whatsapp.template_params = {k: list(v or []) for k, v in cfg.whatsapp.template_params.items()}
    if not cfg.admins:
        raise ConfigError("'admins' must list at least one Matrix user ID")
    return cfg


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return parse_config(raw)
