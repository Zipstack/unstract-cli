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

import errno
import os
import stat
import tomllib
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from unstract_cli.core.errors import remember_secret

LLMWHISPERER = "llmwhisperer"
DOCSTUDIO = "docstudio"
PLATFORM = "platform"
PRODUCTS: tuple[str, ...] = (LLMWHISPERER, DOCSTUDIO, PLATFORM)

#: Built-in defaults, lowest precedence.
DEFAULT_BASE_URLS: dict[str, str] = {
    LLMWHISPERER: "https://llmwhisperer-api.us-central.unstract.com/api/v2",
    DOCSTUDIO: "https://us-central.unstract.com",
    # The same host as docstudio: one deployment serves both the platform API
    # and the deployments it manages.
    PLATFORM: "https://us-central.unstract.com",
}

#: Environment variables per (product, setting), checked before the config file
#: and in the order given. The trailing names are the ones the published clients
#: themselves read: an environment already set up for a client must not leave
#: the CLI silently on its built-in default, which is production.
#:
#: `platform` deliberately has no `org_id` of its own. A platform key carries
#: its organisation, and `auth whoami` writes the one it resolves to the
#: docstudio block -- the block everything else already reads. Two `org_id`
#: settings would mean two rows in `config doctor` that a user has to keep in
#: agreement by hand.
ENV_VARS: dict[tuple[str, str], tuple[str, ...]] = {
    (LLMWHISPERER, "api_key"): ("LLMWHISPERER_API_KEY",),
    (LLMWHISPERER, "base_url"): ("LLMWHISPERER_BASE_URL", "LLMWHISPERER_BASE_URL_V2"),
    (DOCSTUDIO, "api_key"): ("UNSTRACT_DEPLOYMENT_KEY", "UNSTRACT_API_DEPLOYMENT_KEY"),
    (DOCSTUDIO, "base_url"): ("UNSTRACT_BASE_URL",),
    (DOCSTUDIO, "org_id"): ("UNSTRACT_ORG_ID",),
    (PLATFORM, "api_key"): ("UNSTRACT_PLATFORM_KEY",),
    (PLATFORM, "base_url"): ("UNSTRACT_BASE_URL",),
    # `clone` takes this as --api-prefix because a self-hosted deployment can
    # mount the Platform API somewhere other than api/v1. `OrgEndpoint` already
    # defaults to api/v1, which standard installs serve; this is what reaches
    # the ones that remount it, where whoami and `deployment ls` are otherwise
    # unreachable.
    (PLATFORM, "api_prefix"): ("UNSTRACT_API_PREFIX",),
}


#: Where the three credentials are minted. Quoted wherever the CLI reports one
#: as missing: knowing a key is unset is no help without knowing where one is
#: made.
KEY_SOURCES = (
    "Get an LLMWhisperer key from the LLMWhisperer console; a deployment key is "
    "shown on the API deployment's own page in the Unstract UI, and a key "
    "covering every deployment in the organisation is minted under "
    "Settings -> API Key Manager. A platform key, which identifies the "
    "organisation and lists what is in it but cannot run a deployment, is "
    "minted by an organisation admin under Settings -> Platform API Keys."
)


def settings_for(product: str) -> tuple[str, ...]:
    """The settings a product actually has.

    Products differ: `org_id` is a setting only for `docstudio` -- llmwhisperer
    has no organisation, and `platform` reads docstudio's -- and reporting a
    setting a user has no way to supply reads as a misconfiguration they cannot
    fix.
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

    A symlinked candidate is skipped rather than followed: the file it points at
    is chosen by whoever wrote the link, and this path is written to as well as
    read from -- `config set` and `config init --force` would rewrite the target.
    """
    current = (start or Path.cwd()).resolve()
    home = Path.home().resolve()
    for directory in (current, *current.parents):
        candidate = directory / PROJECT_CONFIG_NAME
        if candidate.is_file() and not candidate.is_symlink():
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
    return _resolve_config_path()[0]


def _resolve_config_path() -> tuple[Path, bool]:
    """The config path, and whether it was *discovered* rather than named.

    The boolean is the trust signal: a path the user named (``--config`` or
    ``$UNSTRACT_CONFIG``) is trusted, one found by walking up from the working
    directory is not. See ``UNTRUSTED_PROJECT_KEYS``.
    """
    if _config_override is not None:
        return _config_override, False
    if override := os.environ.get("UNSTRACT_CONFIG"):
        return Path(override).expanduser(), False
    if local := find_project_config():
        return local, True
    return HOME_CONFIG.expanduser(), False


def _deref(value: Any) -> Any:
    """Resolve ``env:VAR_NAME`` indirection so config files hold no secrets.

    An unset variable resolves to ``None`` rather than the literal string, so a
    missing credential surfaces as "not configured" instead of being sent as the
    nonsense value ``"env:FOO"``.

    An empty string resolves the same way: the placeholders a generated config
    carries must not satisfy `require`.
    """
    if isinstance(value, str):
        if value.startswith("env:"):
            return os.environ.get(value[4:].strip()) or None
        return value or None
    return value


#: Settings a *discovered* project-local file may not supply: a checkout the
#: user did not write must not choose the host their key is sent to. Everything
#: else -- org_id, profile selection, deployment aliases -- is still honoured.
UNTRUSTED_PROJECT_KEYS = frozenset({"api_key", "base_url"})


@dataclass
class ConfigFile:
    """Parsed contents of the config file."""

    default_profile: str | None = None
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: Path | None = None
    exists: bool = False
    #: Non-fatal diagnostics (e.g. loose file permissions), surfaced on stderr.
    warnings: tuple[str, ...] = ()
    #: True when `path` was found by walking up from the working directory rather
    #: than named. Such a file is not trusted with credentials or hosts.
    is_project_local: bool = False
    #: Keys withheld from an untrusted file, as ``{(profile, *blocks, key): value}``.
    #: Excluded from resolution, but kept so a write-back does not drop them.
    withheld: dict[tuple[str, ...], Any] = field(default_factory=dict)


def _strip_untrusted(profiles: dict[str, Any]) -> dict[tuple[str, ...], Any]:
    """Remove the untrusted keys from a profile tree, in place, reporting what went."""
    withheld: dict[tuple[str, ...], Any] = {}

    def walk(node: Any, trail: tuple[str, ...]) -> None:
        if not isinstance(node, dict):
            return
        for key in list(node):
            if key in UNTRUSTED_PROJECT_KEYS:
                withheld[(*trail, key)] = node.pop(key)
            else:
                walk(node[key], (*trail, key))

    walk(profiles, ())
    return withheld


def _is_discovered(path: Path) -> bool:
    """Whether this path is the file an upward search would have found.

    Trust follows the file, not the call: naming the project-local file that
    discovery would have picked anyway does not make its contents any more the
    user's own. ``--config`` and ``$UNSTRACT_CONFIG`` are a deliberate choice and
    are resolved before this, so they stay trusted.
    """
    candidate = find_project_config()
    return candidate is not None and candidate.resolve() == path.resolve()


def load_config(path: Path | None = None) -> ConfigFile:
    """Load the config file. A missing file is normal, not an error."""
    if path is not None:
        target, project_local = path, _is_discovered(path)
    else:
        target, project_local = _resolve_config_path()
    if not target.exists():
        return ConfigFile(path=target, exists=False, is_project_local=project_local)

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

    # Said out loud rather than dropped in silence; the rest of the file still
    # applies.
    withheld: dict[tuple[str, ...], Any] = {}
    if project_local:
        withheld = _strip_untrusted(profiles)
        if withheld:
            names = ", ".join(sorted(".".join(trail) for trail in withheld))
            warnings.append(
                f"Ignoring {names} from project config {target}: a discovered "
                f"{PROJECT_CONFIG_NAME} may not supply credentials or base URLs. "
                "Pass --config explicitly, or set the environment variable instead."
            )

    return ConfigFile(
        default_profile=raw.get("default_profile"),
        profiles=profiles,
        path=target,
        exists=True,
        warnings=tuple(warnings),
        is_project_local=project_local,
        withheld=withheld,
    )


def _restored_profiles(cfg: ConfigFile, target: Path) -> dict[str, Any]:
    """The profiles to write, with anything withheld put back.

    Withholding a key from resolution is the security property; deleting it from
    the user's file is not, and `config set` loads, mutates and saves the whole
    document. Restored **only** when writing back to the file they came from --
    into any other path this would copy untrusted values somewhere they are
    trusted.
    """
    if not cfg.withheld or cfg.path is None or target.resolve() != cfg.path.resolve():
        return cfg.profiles

    profiles = deepcopy(cfg.profiles)
    for (*parents, leaf), value in cfg.withheld.items():
        node: dict[str, Any] = profiles
        for segment in parents:
            child = node.get(segment)
            if not isinstance(child, dict):
                child = node[segment] = {}
            node = child
        node.setdefault(leaf, value)
    return profiles


def save_config(cfg: ConfigFile, path: Path | None = None) -> Path:
    """Write the config file with owner-only permissions."""
    target = path or cfg.path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    doc: dict[str, Any] = {}
    if cfg.default_profile:
        doc["default_profile"] = cfg.default_profile
    doc["profiles"] = _restored_profiles(cfg, target)

    # O_NOFOLLOW because this write truncates, and the path is not always one
    # the user chose.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o600)
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.EMLINK):
            raise
        raise ConfigError(
            f"Refusing to write config through the symlink at {target}: it would "
            f"overwrite {os.readlink(target)} instead. Pass --config with the path "
            "of the real file."
        ) from exc
    # The mode above only applies to a file this call creates, so an existing
    # wider one is narrowed before any content goes through the descriptor:
    # after the write is a window in which the new secret is world-readable.
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as fh:
        tomli_w.dump(doc, fh)
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
        # One accepted shape only, settings nested under the product name: a
        # config that looks applied but is not fails later with no obvious cause.
        block = self._profile().get(product)
        return block if isinstance(block, dict) else {}

    def get(self, product: str, key: str, default: Any = None) -> Any:
        """Resolve one setting: **flag > env > profile > built-in default**."""
        value = self._resolve(product, key, default)
        if key == "api_key":
            remember_secret(value)
        return value

    def get_explicit(self, product: str, key: str) -> Any:
        """Resolve through **flag > env > profile** only, stopping before defaults.

        `get` cannot answer "did anyone actually name this?" -- it returns
        `DEFAULT_BASE_URLS[product]` for an unset `base_url`, so a caller who
        deliberately named the default host and one who named nothing come back
        as the same string. Anything that must treat those two differently asks
        here instead of comparing the answer against the default, which reads
        the caller's own choice as silence.
        """
        value = self._explicit(product, key)
        if key == "api_key":
            remember_secret(value)
        return value

    def _explicit(self, product: str, key: str) -> Any:
        """The tiers a human supplied: flag, then environment, then profile."""
        if (value := self.overrides.get(f"{product}.{key}")) is not None:
            return value
        if (value := self.overrides.get(key)) is not None:
            return value

        for env_var in ENV_VARS.get((product, key), ()):
            if value := os.environ.get(env_var):
                return value

        return _deref(self._product_block(product).get(key))

    def _resolve(self, product: str, key: str, default: Any = None) -> Any:
        if (value := self._explicit(product, key)) is not None:
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
        # `--api-key` exists but is not suggested: a secret on the command line
        # lands in shell history and in the process list.
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
        api_key = self._alias_setting(alias, entry, "api_key")
        remember_secret(api_key)
        return {
            "api_name": entry["api_name"],
            "org_id": self._alias_setting(alias, entry, "org_id"),
            "api_key": api_key,
        }

    def _alias_setting(self, alias: str, entry: dict[str, Any], key: str) -> Any:
        """One alias setting, falling back to the profile only where the alias is silent.

        An ``env:`` reference that does not resolve is not silence. Falling back
        there runs the deployment against the profile's organisation, with the
        profile's key, and reports success.
        """
        raw = entry.get(key)
        if isinstance(raw, str) and raw.startswith("env:"):
            if value := _deref(raw):
                return value
            raise ConfigError(
                f"Deployment alias {alias!r} sets {key} to {raw!r}, and "
                f"${raw[4:].strip()} is not set in this process's environment."
            )
        return raw or self.get(DOCSTUDIO, key)

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

        report: dict[str, Any] = (
            {"resolved": True, "source": "built-in default"}
            if key == "base_url" and DEFAULT_BASE_URLS.get(product)
            else {"resolved": False, "source": "unset"}
        )
        if detail := self.withheld_detail(product, key):
            report["detail"] = detail
        return report

    def withheld_detail(self, *trail: str) -> str | None:
        """Why a setting the config file plainly holds did not arrive, if that is why.

        Reporting only where a value came *from* would leave the user staring at
        a setting they can see in the file. Takes a trail rather than a
        product/key pair so a deployment alias's own key -- nested a level deeper
        -- is answerable too.
        """
        if (self.active_profile, *trail) not in self.file.withheld:
            return None
        return (
            f"{self.file.path} sets {trail[-1]}, and a discovered "
            f"{PROJECT_CONFIG_NAME} is not trusted with it."
        )


def starter_profiles() -> dict[str, dict[str, Any]]:
    """Profile stubs written by `config init`.

    Every credential uses ``env:`` indirection: the generated file is a map of
    where secrets live, never a copy of them.

    One key on the product block, and aliases that carry only ``api_name``: a
    key can cover every deployment in the organisation, so a key per alias is
    the exception -- for an organisation whose deployments hold separate keys --
    rather than the shape to start from.
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
            # No `api_key` on purpose. A platform key is optional -- holding
            # only a deployment key is the common case -- and an `env:`
            # reference to an unset variable is a `config doctor` problem,
            # which would exit 1 for every user who does not hold one.
            PLATFORM: {"base_url": DEFAULT_BASE_URLS[PLATFORM]},
            "deployments": {"example": {"api_name": "your-api-deployment-name"}},
        },
        "cloud-eu": {
            LLMWHISPERER: {
                "base_url": "https://llmwhisperer-api.eu-west.unstract.com/api/v2",
                "api_key": "env:LLMWHISPERER_API_KEY",
            },
        },
        # A shape to copy for a self-hosted install, not a profile to select:
        # its host is a placeholder and only the active profile is resolved.
        "onprem-example": {
            LLMWHISPERER: {
                "base_url": "https://llmwhisperer.unstract.internal.example/api/v2",
                "api_key": "env:LLMWHISPERER_API_KEY",
            },
            DOCSTUDIO: {
                "base_url": "https://unstract.internal.example",
                "org_id": "",
                "api_key": "env:UNSTRACT_DEPLOYMENT_KEY",
            },
            # No `api_key` -- see the cloud-us block.
            PLATFORM: {"base_url": "https://unstract.internal.example"},
        },
    }


__all__ = [
    "DEFAULT_BASE_URLS",
    "DOCSTUDIO",
    "ENV_VARS",
    "HOME_CONFIG",
    "KEY_SOURCES",
    "LLMWHISPERER",
    "PLATFORM",
    "PRODUCTS",
    "PROJECT_CONFIG_NAME",
    "UNTRUSTED_PROJECT_KEYS",
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
