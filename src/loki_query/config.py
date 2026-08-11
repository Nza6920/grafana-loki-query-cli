from __future__ import annotations

import os
from pathlib import Path
import re
import tomllib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when the CLI configuration cannot be used."""


@dataclass(frozen=True)
class Profile:
    name: str
    grafana_url: str
    datasource_uid: str
    token_env: str
    default_selector: str | None = None


@dataclass(frozen=True)
class AppConfig:
    profiles: dict[str, Profile]


_PROFILE_KEYS = {"grafana_url", "datasource_uid", "token_env", "default_selector"}
_ENVIRONMENT_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def default_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "loki-query" / "config.toml"


def load_config(path: Path) -> AppConfig:
    try:
        with path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise ConfigurationError(f"Configuration file not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"Invalid TOML in {path}: {error}") from error

    unsupported_top_level = sorted(set(document) - {"profiles"})
    if unsupported_top_level:
        raise ConfigurationError(
            "Configuration has unsupported top-level keys: "
            + ", ".join(unsupported_top_level)
            + "."
        )

    raw_profiles = document.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ConfigurationError("Configuration must define at least one profile.")

    profiles: dict[str, Profile] = {}
    for name, raw_profile in raw_profiles.items():
        if not isinstance(name, str) or not isinstance(raw_profile, dict):
            raise ConfigurationError("Every profile must be a named TOML table.")
        profiles[name] = _parse_profile(name, raw_profile)
    return AppConfig(profiles=profiles)


def _parse_profile(name: str, values: dict[str, Any]) -> Profile:
    unsupported = sorted(set(values) - _PROFILE_KEYS)
    if unsupported:
        raise ConfigurationError(
            f"Profile {name!r} has unsupported keys: {', '.join(unsupported)}."
        )
    required: dict[str, str] = {}
    for key in ("grafana_url", "datasource_uid", "token_env"):
        value = values.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError(f"Profile {name!r} requires non-empty {key!r}.")
        required[key] = value.strip()

    grafana_url = urlsplit(required["grafana_url"])
    if grafana_url.scheme not in {"http", "https"} or not grafana_url.netloc:
        raise ConfigurationError(
            f"Profile {name!r} grafana_url must be an absolute HTTP(S) URL."
        )
    if grafana_url.username is not None or grafana_url.password is not None:
        raise ConfigurationError(
            f"Profile {name!r} grafana_url must not contain credentials."
        )
    if not _ENVIRONMENT_VARIABLE.fullmatch(required["token_env"]):
        raise ConfigurationError(
            f"Profile {name!r} token_env must be an environment variable name."
        )

    selector = values.get("default_selector")
    if selector is not None and (not isinstance(selector, str) or not selector.strip()):
        raise ConfigurationError(
            f"Profile {name!r} default_selector must be a non-empty string."
        )

    return Profile(
        name=name,
        grafana_url=required["grafana_url"].rstrip("/"),
        datasource_uid=required["datasource_uid"],
        token_env=required["token_env"],
        default_selector=selector.strip() if isinstance(selector, str) else None,
    )
