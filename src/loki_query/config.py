from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import re
import sys
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


def default_config_path(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    environment = environ if environ is not None else os.environ
    if config_path := environment.get("LOKI_QUERY_CONFIG"):
        return Path(config_path).expanduser()
    if config_home := environment.get("XDG_CONFIG_HOME"):
        return Path(config_home).expanduser() / "loki-query" / "config.toml"
    user_home = home if home is not None else Path.home()
    if (platform if platform is not None else sys.platform) == "win32":
        appdata = environment.get("APPDATA")
        base = Path(appdata).expanduser() if appdata else user_home / "AppData" / "Roaming"
    else:
        base = user_home / ".config"
    return base / "loki-query" / "config.toml"


def load_config(path: Path) -> AppConfig:
    try:
        with path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise ConfigurationError(f"Configuration file not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"Invalid TOML in {path}: {error}") from error
    except UnicodeDecodeError as error:
        raise ConfigurationError(
            f"Configuration file must be UTF-8: {path}."
        ) from error
    except OSError as error:
        detail = error.strerror or error.__class__.__name__
        raise ConfigurationError(
            f"Cannot read configuration file {path}: {detail}."
        ) from error

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

    try:
        grafana_url = urlsplit(required["grafana_url"])
    except ValueError as error:
        raise ConfigurationError(
            f"Profile {name!r} grafana_url must be an absolute HTTP(S) URL."
        ) from error
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
