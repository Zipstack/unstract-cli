"""Profile-based configuration.

Two products with different hosts, different keys, and `org_id` as a URL *path
segment* rather than a flag. Named profiles (kubectl/aws style) hold per-product
host, key and org, plus deployment aliases so a deployment can be named instead
of spelled out.

The resolution chain -- **flag > env > profile > built-in default** -- is
implemented once here and used by every parameter. It is never re-implemented
per command.

The CLI is fully usable with **no config file at all**, driven entirely by
environment variables; that is the expected mode in CI and agent sandboxes.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

LLMWHISPERER = "llmwhisperer"
DOCSTUDIO = "docstudio"
PRODUCTS: tuple[str, ...] = (LLMWHISPERER, DOCSTUDIO)

#: Built-in defaults, lowest precedence.
DEFAULT_BASE_URLS: dict[str, str] = {
    LLMWHISPERER: "https://llmwhisperer-api.us-central.unstract.com/api/v2",
    DOCSTUDIO: "https://us-central.unstract.com",
}

#: Environment variables per (product, setting), checked before the config file.
ENV_VARS: dict[tuple[str, str], tuple[str, ...]] = {
    (LLMWHISPERER, "api_key"): ("LLMWHISPERER_API_KEY",),
    (LLMWHISPERER, "base_url"): ("LLMWHISPERER_BASE_URL",),
    (DOCSTUDIO, "api_key"): ("UNSTRACT_DEPLOYMENT_KEY",),
    (DOCSTUDIO, "base_url"): ("UNSTRACT_BASE_URL",),
    (DOCSTUDIO, "org_id"): ("UNSTRACT_ORG_ID",),
}

def settings_for(product: str) -> tuple[str, ...]:
    """The settings a product actually has.

    Products differ: `org_id` is a URL path segment for one and meaningless for
    the other, and reporting a setting a user has no way to supply reads as a
    misconfiguration they cannot fix.
    """
    return tuple(sorted(key for prod, key in ENV_VARS if prod == product))


#: Filename a project can commit to point the CLI at its own settings.
PROJECT_CONFIG_NAME = ".unstract.toml"

#: Where the config lives when nothing else selects one.
HOME_CONFIG = Path("~/.unstract/config.toml")


class ConfigError(Exception):
    """Configuration could not be loaded or resolved."""


#: Set by the root `--config` flag. Highest precedence, matching the
#: flag > env > file ordering used for every other setting.
_config_override: Path | None = None


def set_config_path(path: str | Path | None) -> None:
    """Point this process at a specific config file (the `--config` flag)."""
    global _config_override
    _config_override = Path(path).expanduser() if path else None


def find_project_config(start: Path | None = None) -> Path | None:
    """Search upward from the working directory for ``.unstract.toml``.

    Mirrors how git and ruff resolve project settings: running the CLI inside a
    project picks up that project's config with no flag. The search stops at the
    filesystem root, and at ``$HOME`` so a stray file in a parent directory
    cannot silently capture every invocation.
    """
    current = (start or Path.cwd()).resolve()
    home = Path.home().resolve()
    for directory in (current, *current.parents):
        candidate = directory / PROJECT_CONFIG_NAME
        if candidate.is_file():
            return candidate
        if directory == home:
            break
    return None


def config_path() -> Path:
    """Location of the config file.

    Resolution: ``--config``, then ``$UNSTRACT_CONFIG``, then a project-local
    ``.unstract.toml`` found by upward search, then ``~/.unstract/config.toml``.

    Several config files coexisting is expected, not exceptional: a per-project
    file checked into a repo, a throwaway one in CI, and a personal default, each
    selected per invocation.
    """
    if _config_override is not None:
        return _config_override
    if override := os.environ.get("UNSTRACT_CONFIG"):
        return Path(override).expanduser()
    if local := find_project_config():
        return local
    return HOME_CONFIG.expanduser()


def _deref(value: Any) -> Any:
    """Resolve ``env:VAR_NAME`` indirection so config files hold no secrets.

    An unset variable resolves to ``None`` rather than the literal string, so a
    missing credential surfaces as "not configured" instead of being sent as the
    nonsense value ``"env:FOO"``.
    """
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:].strip()) or None
    return value


@dataclass
class ConfigFile:
    """Parsed contents of the config file."""

    default_profile: str | None = None
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: Path | None = None
    exists: bool = False
    #: Non-fatal diagnostics (e.g. loose file permissions), surfaced on stderr.
    warnings: tuple[str, ...] = ()


def load_config(path: Path | None = None) -> ConfigFile:
    """Load the config file. A missing file is normal, not an error."""
    target = path or config_path()
    if not target.exists():
        return ConfigFile(path=target, exists=False)

    try:
        with target.open("rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Could not read config at {target}: {exc}") from exc

    warnings: list[str] = []
    try:
        mode = target.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            warnings.append(
                f"Config file {target} is readable by other users "
                f"(mode {stat.filemode(mode)}); consider `chmod 600`."
            )
    except OSError:  # pragma: no cover - stat failure is not worth failing on
        pass

    profiles = raw.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError(f"`profiles` in {target} must be a table.")

    return ConfigFile(
        default_profile=raw.get("default_profile"),
        profiles=profiles,
        path=target,
        exists=True,
        warnings=tuple(warnings),
    )


def save_config(cfg: ConfigFile, path: Path | None = None) -> Path:
    """Write the config file with owner-only permissions."""
    target = path or cfg.path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    doc: dict[str, Any] = {}
    if cfg.default_profile:
        doc["default_profile"] = cfg.default_profile
    doc["profiles"] = cfg.profiles

    # Create with 0600 from the outset rather than widening then narrowing: a
    # world-readable window, however brief, is a window.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        tomli_w.dump(doc, fh)
    os.chmod(target, 0o600)
    return target


@dataclass
class ResolvedConfig:
    """Effective settings for one invocation.

    ``overrides`` holds command-line flags, which outrank everything else.
    """

    file: ConfigFile
    profile_name: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def active_profile(self) -> str | None:
        """Profile selected by flag, ``UNSTRACT_PROFILE``, or the file default."""
        return (
            self.profile_name
            or os.environ.get("UNSTRACT_PROFILE")
            or self.file.default_profile
        )

    def _profile(self) -> dict[str, Any]:
        name = self.active_profile
        if not name:
            return {}
        profile = self.file.profiles.get(name)
        if profile is None:
            if self.file.exists and self.file.profiles:
                known = ", ".join(sorted(self.file.profiles)) or "none"
                raise ConfigError(
                    f"Profile {name!r} not found in {self.file.path}. "
                    f"Known profiles: {known}"
                )
            return {}
        return profile if isinstance(profile, dict) else {}

    def _product_block(self, product: str) -> dict[str, Any]:
        # Exactly one accepted shape: settings nested under the product name. No
        # aliases and no flat fallback -- a config that looks applied but is not
        # is worse than one that plainly is not, because the failure surfaces
        # later as a missing-credential error with no obvious cause.
        block = self._profile().get(product)
        return block if isinstance(block, dict) else {}

    def get(self, product: str, key: str, default: Any = None) -> Any:
        """Resolve one setting: **flag > env > profile > built-in default**."""
        if (value := self.overrides.get(f"{product}.{key}")) is not None:
            return value
        if (value := self.overrides.get(key)) is not None:
            return value

        for env_var in ENV_VARS.get((product, key), ()):
            if value := os.environ.get(env_var):
                return value

        if (value := _deref(self._product_block(product).get(key))) is not None:
            return value

        if default is not None:
            return default
        if key == "base_url":
            return DEFAULT_BASE_URLS.get(product)
        return None

    def require(self, product: str, key: str) -> Any:
        """Resolve a setting, or raise a message naming exactly how to supply it."""
        if (value := self.get(product, key)) is not None:
            return value

        hints: list[str] = []
        if env_vars := ENV_VARS.get((product, key)):
            hints.append(f"set ${env_vars[0]}")
        hints.append(f"or add `{key}` to the [profiles.<name>.{product}] block")
        # Only suggest a flag that actually exists. Credentials have no flag by
        # design -- a secret on the command line lands in shell history and
        # process listings.
        if key != "api_key":
            hints.append(f"or pass --{key.replace('_', '-')}")
        raise ConfigError(
            f"Missing required setting {product}.{key}. To fix: {'; '.join(hints)}."
        )

    def deployment(self, alias: str) -> dict[str, Any]:
        """Resolve a deployment alias to its api_name, org and key.

        ``org_id`` and ``api_key`` are optional per alias and fall back to the
        profile's Document Studio block, so the common case is one line per
        deployment.
        """
        aliases = self._profile().get("deployments")
        entry = aliases.get(alias) if isinstance(aliases, dict) else None
        if not isinstance(entry, dict):
            known = (
                ", ".join(sorted(aliases))
                if isinstance(aliases, dict) and aliases
                else "none"
            )
            raise ConfigError(
                f"Deployment alias {alias!r} not found in profile "
                f"{self.active_profile!r}. Known aliases: {known}."
            )
        if not entry.get("api_name"):
            raise ConfigError(f"Deployment alias {alias!r} has no `api_name`.")
        return {
            "api_name": entry["api_name"],
            "org_id": _deref(entry.get("org_id")) or self.get(DOCSTUDIO, "org_id"),
            "api_key": _deref(entry.get("api_key")) or self.get(DOCSTUDIO, "api_key"),
        }

    def deployment_aliases(self) -> tuple[str, ...]:
        """Names of the deployment aliases defined in the active profile."""
        aliases = self._profile().get("deployments")
        return tuple(sorted(aliases)) if isinstance(aliases, dict) else ()

    def resolution_source(self, product: str, key: str) -> dict[str, Any]:
        """Report where a setting resolves from, without echoing a secret.

        `config doctor` uses this to answer the question that costs the most
        time: "the CLI says the key is not configured, but I set it -- where is
        it looking?"
        """
        if (
            self.overrides.get(f"{product}.{key}") is not None
            or self.overrides.get(key) is not None
        ):
            return {"resolved": True, "source": "flag/override"}

        for env_var in ENV_VARS.get((product, key), ()):
            if os.environ.get(env_var):
                return {"resolved": True, "source": f"env:{env_var}"}

        raw = self._product_block(product).get(key)
        if isinstance(raw, str) and raw.startswith("env:"):
            var = raw[4:].strip()
            present = bool(os.environ.get(var))
            return {
                "resolved": present,
                "source": f"profile -> env:{var}",
                "detail": None
                if present
                else f"${var} is not set in this process's environment",
            }
        if raw not in (None, ""):
            return {"resolved": True, "source": "profile (literal)"}

        if key == "base_url" and DEFAULT_BASE_URLS.get(product):
            return {"resolved": True, "source": "built-in default"}
        return {"resolved": False, "source": "unset"}


def starter_profiles() -> dict[str, dict[str, Any]]:
    """Profile stubs written by `config init`.

    Every credential uses ``env:`` indirection: the generated file is a map of
    where secrets live, never a copy of them.
    """
    return {
        "cloud-us": {
            LLMWHISPERER: {
                "base_url": DEFAULT_BASE_URLS[LLMWHISPERER],
                "api_key": "env:LLMWHISPERER_API_KEY",
            },
            DOCSTUDIO: {
                "base_url": DEFAULT_BASE_URLS[DOCSTUDIO],
                "org_id": "",
                "api_key": "env:UNSTRACT_DEPLOYMENT_KEY",
            },
            "deployments": {},
        },
        "cloud-eu": {
            LLMWHISPERER: {
                "base_url": "https://llmwhisperer-api.eu-west.unstract.com/api/v2",
                "api_key": "env:LLMWHISPERER_API_KEY",
            },
        },
    }


__all__ = [
    "DEFAULT_BASE_URLS",
    "DOCSTUDIO",
    "ENV_VARS",
    "HOME_CONFIG",
    "LLMWHISPERER",
    "PRODUCTS",
    "PROJECT_CONFIG_NAME",
    "ConfigError",
    "ConfigFile",
    "ResolvedConfig",
    "config_path",
    "find_project_config",
    "load_config",
    "save_config",
    "set_config_path",
    "settings_for",
    "starter_profiles",
]
