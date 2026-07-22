"""Profile-based configuration (SPEC.md §4).

Three products, three incompatible auth schemes, region-specific and on-prem
hosts, and `org_id` as a URL *path segment* rather than a flag. Named profiles
(kubectl/aws style) hold per-product host, key and org.

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

from unstract_cli.core.model import ApiGroup, Product

#: Config blocks are nested by product, then by API group:
#:
#:     [profiles.cloud-us.docstudio.platform]
#:     [profiles.cloud-us.llmwhisperer]
#:
#: Document Studio owns three API groups with distinct hosts and keys, so they
#: keep separate sub-blocks rather than being flattened into one.
GROUP_PATH: dict[str, tuple[str, ...]] = {
    ApiGroup.PLATFORM.value: ("docstudio", "platform"),
    ApiGroup.DEPLOYMENT.value: ("docstudio", "deployment"),
    ApiGroup.HITL.value: ("docstudio", "hitl"),
    ApiGroup.LLMWHISPERER.value: ("llmwhisperer",),
    ApiGroup.APIHUB.value: ("apihub",),
}

#: The names `config get`/`config set` accept, derived from GROUP_PATH so the
#: command line and the file layout can never disagree. A group owned by a
#: product must be addressed through it -- `docstudio.platform`, never a bare
#: `platform` -- because the bare form hides which product a setting belongs to.
TARGET_NAMES: tuple[str, ...] = tuple(
    ".".join(path) for path in GROUP_PATH.values()
)


def resolve_target(name: str) -> str | None:
    """Map a user-supplied target onto its API group.

    Accepts either separator, so `docstudio.platform` and `docstudio platform`
    are the same thing -- the latter is what a shell user reaches for after
    typing `unstract docstudio platform ...`.
    """
    wanted = tuple(name.replace(".", " ").split())
    for group, path in GROUP_PATH.items():
        if wanted == path:
            return group
    return None

#: Built-in defaults, lowest precedence. API Hub deliberately has **no** default
#: base URL: its public hostname is unconfirmed (SPEC.md §11.1), and inventing
#: one would send an agent's documents to a host we cannot vouch for.
DEFAULT_BASE_URLS: dict[str, str] = {
    ApiGroup.LLMWHISPERER.value: "https://llmwhisperer-api.us-central.unstract.com/api/v2",
    ApiGroup.PLATFORM.value: "https://us-central.unstract.com",
    ApiGroup.DEPLOYMENT.value: "https://us-central.unstract.com",
    ApiGroup.HITL.value: "https://us-central.unstract.com",
}

#: Environment variables per (product, setting), checked before the config file.
ENV_VARS: dict[tuple[str, str], tuple[str, ...]] = {
    (ApiGroup.LLMWHISPERER.value, "api_key"): ("LLMWHISPERER_API_KEY",),
    (ApiGroup.LLMWHISPERER.value, "base_url"): ("LLMWHISPERER_BASE_URL",),
    (ApiGroup.PLATFORM.value, "api_key"): ("UNSTRACT_PLATFORM_KEY",),
    (ApiGroup.PLATFORM.value, "base_url"): ("UNSTRACT_BASE_URL",),
    (ApiGroup.PLATFORM.value, "org_id"): ("UNSTRACT_ORG_ID",),
    (ApiGroup.DEPLOYMENT.value, "api_key"): ("UNSTRACT_DEPLOYMENT_KEY",),
    (ApiGroup.DEPLOYMENT.value, "base_url"): ("UNSTRACT_BASE_URL",),
    (ApiGroup.DEPLOYMENT.value, "org_id"): ("UNSTRACT_ORG_ID",),
    (ApiGroup.HITL.value, "api_key"): ("UNSTRACT_DEPLOYMENT_KEY", "UNSTRACT_PLATFORM_KEY"),
    (ApiGroup.HITL.value, "base_url"): ("UNSTRACT_BASE_URL",),
    (ApiGroup.HITL.value, "org_id"): ("UNSTRACT_ORG_ID",),
    (ApiGroup.APIHUB.value, "api_key"): ("UNSTRACT_APIHUB_KEY",),
    (ApiGroup.APIHUB.value, "base_url"): ("UNSTRACT_APIHUB_BASE_URL",),
    (ApiGroup.APIHUB.value, "anthropic_key"): ("UNSTRACT_ANTHROPIC_API_KEY",),
    (ApiGroup.APIHUB.value, "llmwhisperer_key"): ("LLMWHISPERER_API_KEY",),
}


class ConfigError(Exception):
    """Configuration could not be loaded or resolved."""


#: Set by the root `--config` flag. Highest precedence, matching the
#: flag > env > file ordering used for every other setting (SPEC.md §4.1).
_config_override: Path | None = None


def set_config_path(path: str | Path | None) -> None:
    """Point this process at a specific config file (the `--config` flag)."""
    global _config_override
    _config_override = Path(path).expanduser() if path else None


def config_path() -> Path:
    """Location of the config file.

    Resolution: ``--config`` flag, then ``$UNSTRACT_CONFIG``, then
    ``$XDG_CONFIG_HOME/unstract/config.toml``, then
    ``~/.config/unstract/config.toml``.

    Several config files are expected, not exceptional: a per-project file
    checked into a repo, a throwaway one in CI, and a personal default can all
    coexist, selected per invocation.
    """
    if _config_override is not None:
        return _config_override
    if override := os.environ.get("UNSTRACT_CONFIG"):
        return Path(override).expanduser()
    if local := find_project_config():
        return local
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "unstract" / "config.toml"


#: Filename a project can commit to point the CLI at its own settings.
PROJECT_CONFIG_NAME = ".unstract.toml"


def find_project_config(start: Path | None = None) -> Path | None:
    """Search upward from the working directory for ``.unstract.toml``.

    Mirrors how git, ruff and similar tools resolve project settings: running the
    CLI inside a project picks up that project's config without any flag. The
    search stops at the filesystem root, and at ``$HOME`` so a stray file in a
    parent directory cannot silently capture every invocation.
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
    """Parsed contents of ``config.toml``."""

    default_profile: str | None = None
    profiles: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
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

    # Create with 0600 from the outset rather than widening then narrowing:
    # a world-readable window, however brief, is a window.
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

    def _profile_block(self, product: str) -> dict[str, Any]:
        name = self.active_profile
        if not name:
            return {}
        profile = self.file.profiles.get(name)
        if profile is None:
            if self.file.exists and self.file.profiles:
                known = ", ".join(sorted(self.file.profiles)) or "none"
                raise ConfigError(
                    f"Profile {name!r} not found in {self.file.path}. Known profiles: {known}"
                )
            return {}
        # Exactly one accepted shape: the API group nested under its product,
        # e.g. [profiles.X.docstudio.platform]. LLMWhisperer and API Hub own a
        # single group each, so their block sits directly under the product name.
        #
        # No aliases and no flat fallback. A block written any other way is not
        # silently picked up -- a config that looks applied but is not is worse
        # than one that plainly is not, because the failure surfaces later as a
        # missing-credential error with no obvious cause.
        node: Any = profile
        for segment in GROUP_PATH.get(product, (product,)):
            node = node.get(segment) if isinstance(node, dict) else None
            if node is None:
                return {}
        return node if isinstance(node, dict) else {}

    def get(self, product: str | ApiGroup | Product, key: str, default: Any = None) -> Any:
        """Resolve one setting: **flag > env > profile > built-in default**."""
        product = product.value if isinstance(product, (ApiGroup, Product)) else product

        if (value := self.overrides.get(f"{product}.{key}")) is not None:
            return value
        if (value := self.overrides.get(key)) is not None:
            return value

        for env_var in ENV_VARS.get((product, key), ()):
            if value := os.environ.get(env_var):
                return value

        if (value := _deref(self._profile_block(product).get(key))) is not None:
            return value

        if default is not None:
            return default
        if key == "base_url":
            return DEFAULT_BASE_URLS.get(product)
        return None

    def require(self, product: str | ApiGroup | Product, key: str) -> Any:
        """Resolve a setting, or raise a message naming exactly how to supply it."""
        if (value := self.get(product, key)) is not None:
            return value

        product = product.value if isinstance(product, (ApiGroup, Product)) else product
        hints: list[str] = []
        if env_vars := ENV_VARS.get((product, key)):
            hints.append(f"set ${env_vars[0]}")
        block = ".".join(GROUP_PATH.get(product, (product,)))
        hints.append(f"or add `{key}` to the [profiles.<name>.{block}] block")
        # Only suggest a flag that actually exists. Credentials have no flag by
        # design -- a secret on the command line lands in shell history and
        # process listings.
        if key != "api_key":
            hints.append(f"or pass --{key.replace('_', '-')}")
        raise ConfigError(
            f"Missing required setting {product}.{key}. To fix: {'; '.join(hints)}."
        )


def starter_profiles() -> dict[str, dict[str, dict[str, Any]]]:
    """Profile stubs written by `config init`.

    Every credential uses ``env:`` indirection: the generated file is a map of
    where secrets live, never a copy of them.
    """
    return {
        "cloud-us": {
            # Document Studio -- one product, three API groups with distinct
            # hosts and credentials.
            "docstudio": {
                "platform": {
                    "base_url": DEFAULT_BASE_URLS[ApiGroup.PLATFORM.value],
                    "org_id": "",
                    "api_key": "env:UNSTRACT_PLATFORM_KEY",
                },
                "deployment": {
                    "base_url": DEFAULT_BASE_URLS[ApiGroup.DEPLOYMENT.value],
                    "org_id": "",
                    "api_key": "env:UNSTRACT_DEPLOYMENT_KEY",
                },
                "hitl": {
                    "base_url": DEFAULT_BASE_URLS[ApiGroup.HITL.value],
                    "org_id": "",
                    "api_key": "env:UNSTRACT_DEPLOYMENT_KEY",
                },
            },
            "llmwhisperer": {
                "base_url": DEFAULT_BASE_URLS[ApiGroup.LLMWHISPERER.value],
                "api_key": "env:LLMWHISPERER_API_KEY",
            },
            "apihub": {"base_url": "", "api_key": "env:UNSTRACT_APIHUB_KEY"},
        },
        "cloud-eu": {
            "llmwhisperer": {
                "base_url": "https://llmwhisperer-api.eu-west.unstract.com/api/v2",
                "api_key": "env:LLMWHISPERER_API_KEY",
            },
        },
    }


__all__ = [
    "DEFAULT_BASE_URLS",
    "ENV_VARS",
    "PROJECT_CONFIG_NAME",
    "TARGET_NAMES",
    "resolve_target",
    "find_project_config",
    "set_config_path",
    "ConfigError",
    "ConfigFile",
    "ResolvedConfig",
    "config_path",
    "load_config",
    "save_config",
    "starter_profiles",
]
