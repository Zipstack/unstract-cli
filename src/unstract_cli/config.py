"""Profile-based configuration.

Two products with different hosts, different keys, and `org_id` as a URL *path
segment* rather than a flag. Named profiles (kubectl/aws style) hold per-product
host, key and org, plus a key per deployment for the ones that need their own.

The resolution chain -- **flag > env > profile > built-in default** -- is
implemented once here and used by every parameter. It is never re-implemented
per command.

The CLI is fully usable with **no config file at all**, driven entirely by
environment variables; that is the expected mode in CI and agent sandboxes.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
import tomllib
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from unstract_cli.core.errors import remember_secret, warn

LLMWHISPERER = "llmwhisperer"
DOCSTUDIO = "docstudio"
PRODUCTS: tuple[str, ...] = (LLMWHISPERER, DOCSTUDIO)

#: Settings whose values are credentials: registered for scrubbing when
#: resolved, withheld from a discovered project file, never echoed.
SECRET_SETTINGS = frozenset({"api_key", "platform_key"})

#: Built-in defaults, lowest precedence.
DEFAULT_BASE_URLS: dict[str, str] = {
    LLMWHISPERER: "https://llmwhisperer-api.us-central.unstract.com/api/v2",
    DOCSTUDIO: "https://us-central.unstract.com",
}

#: Environment variables per (product, setting), checked before the config file
#: and in the order given. The trailing names are the ones the published clients
#: themselves read: an environment already set up for a client must not leave
#: the CLI silently on its built-in default, which is production.
#:
#: The platform key sits on the docstudio block beside the deployment key: one
#: deployment serves both the platform API and the deployments it manages, so
#: the two keys share a host and an organisation.
ENV_VARS: dict[tuple[str, str], tuple[str, ...]] = {
    (LLMWHISPERER, "api_key"): ("LLMWHISPERER_API_KEY",),
    (LLMWHISPERER, "base_url"): ("LLMWHISPERER_BASE_URL", "LLMWHISPERER_BASE_URL_V2"),
    (DOCSTUDIO, "api_key"): ("UNSTRACT_DEPLOYMENT_KEY", "UNSTRACT_API_DEPLOYMENT_KEY"),
    (DOCSTUDIO, "base_url"): ("UNSTRACT_BASE_URL",),
    (DOCSTUDIO, "org_id"): ("UNSTRACT_ORG_ID",),
    (DOCSTUDIO, "platform_key"): ("UNSTRACT_PLATFORM_KEY",),
}


#: Where the three credentials are minted. Quoted by `config doctor`, by the
#: starter file `config init` writes, and wherever the CLI reports one as
#: missing: knowing a key is unset is no help without knowing where one is made.
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

    Products differ: `org_id` and `platform_key` are settings only for
    `docstudio` -- llmwhisperer has no organisation -- and reporting a setting a
    user has no way to supply reads as a misconfiguration they cannot fix.
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


def init_path() -> Path:
    """Where `config init` writes when no config file was named.

    Discovery is for reading. A file found by walking up from the working
    directory is not trusted with credentials or hosts, so a starter config
    written there is one the next command refuses to honour -- and writing to a
    checked-in file the user never named is a surprise in its own right.
    """
    path, discovered = _resolve_config_path()
    return HOME_CONFIG.expanduser() if discovered else path


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


def _deref(value: Any, *, allow_env: bool) -> Any:
    """Resolve ``env:VAR_NAME`` indirection so config files hold no secrets.

    An unset variable resolves to ``None`` rather than the literal string, so a
    missing credential surfaces as "not configured" instead of being sent as the
    nonsense value ``"env:FOO"``.

    An empty string resolves the same way: the placeholders a generated config
    carries must not satisfy `require`.

    ``allow_env`` is the trust boundary, and has no default: the permissive
    branch is the one that splices an attacker-chosen variable into a request
    URL, so a caller has to ask for it. A discovered project-local file may not
    name the variable to read, because whatever it names is then spliced into a
    request URL and echoed back in any error about it.
    """
    if isinstance(value, str):
        if value.startswith("env:"):
            return os.environ.get(value[4:].strip()) or None if allow_env else None
        return value or None
    return value


#: Settings a *discovered* project-local file may not supply as literals: a
#: checkout the user did not write must not choose the host their key is sent
#: to. Separately, and for every key, such a file may not name an environment
#: variable to read either -- see `ResolvedConfig._env_refused`.
UNTRUSTED_PROJECT_KEYS = SECRET_SETTINGS | {"base_url"}


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
    #: The file as it was parsed. A write rebuilds only the tables this CLI owns,
    #: so anything else in the file survives being written through.
    raw: dict[str, Any] = field(default_factory=dict)


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
        raw=raw,
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

    # Started from the file as it was read: a table this CLI does not know about
    # is not a table it may delete, and `config set` would otherwise drop
    # whatever else the user or a later version keeps here.
    doc: dict[str, Any] = {k: v for k, v in cfg.raw.items() if k != "profiles"}
    doc.pop("default_profile", None)
    if cfg.default_profile:
        doc["default_profile"] = cfg.default_profile
    doc["profiles"] = _restored_profiles(cfg, target)

    # The path is not always one the user chose, and replacing a symlink would
    # silently turn a deliberate one into a regular file.
    if target.is_symlink():
        raise ConfigError(
            f"Refusing to write config through the symlink at {target}: it would "
            f"overwrite {os.readlink(target)} instead. Pass --config with the path "
            "of the real file."
        )

    # Written through a temporary file and renamed into place. Truncating the
    # real one first would destroy a working config if anything below it failed,
    # and `mkstemp` both names the temporary unpredictably -- a guessable
    # sibling in a shared directory is a symlink waiting to be planted -- and
    # creates it 0600, which is the mode the rename then gives the config, with
    # no window in which the new credential is readable more widely.
    try:
        handle_fd, name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    except OSError as exc:
        # Renaming into place is what makes the write atomic, and that needs the
        # directory, not just the file. Writing the file in place instead would
        # put back the truncate this replaced.
        raise ConfigError(
            f"Cannot write {target}: its directory {target.parent} is not "
            f"writable, and the config is replaced rather than overwritten so a "
            f"failed write cannot destroy it ({exc.strerror})."
        ) from exc
    tmp = Path(name)
    try:
        with os.fdopen(handle_fd, "wb") as fh:
            tomli_w.dump(doc, fh)
            # The rename only replaces one whole config with another if the new
            # bytes are on the disk before it happens. Without this a crash can
            # leave the rename standing over content that never landed.
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        # And the rename is itself a directory change that has to be persisted;
        # syncing the file does not cover the entry that now points at it.
        with contextlib.suppress(OSError):  # not every platform syncs a directory
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target


@dataclass
class ResolvedConfig:
    """Effective settings for one invocation.

    ``overrides`` holds command-line flags, which outrank everything else.
    """

    file: ConfigFile
    profile_name: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    #: `env:` references already refused, so one is reported once per run.
    _reported: set[str] = field(default_factory=set, repr=False, init=False)

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

    def unknown_settings(self, product: str) -> tuple[str, ...]:
        """Keys written under a product that nothing will ever read back."""
        try:
            written = set(self._product_block(product))
        except ConfigError:
            return ()
        return tuple(sorted(written - set(settings_for(product))))

    def get(self, product: str, key: str, default: Any = None) -> Any:
        """Resolve one setting: **flag > env > profile > built-in default**."""
        value = self._resolve(product, key, default)
        if key in SECRET_SETTINGS:
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
        if key in SECRET_SETTINGS:
            remember_secret(value)
        return value

    def _tiers(self, product: str, key: str) -> Iterator[Any]:
        """What each tier says, in order -- flag, env, profile -- unset as `None`.

        Lazy on purpose: reading the profile block resolves the profile name,
        which raises for one that does not exist. A caller answered by an
        earlier tier must not be failed by a later one it never consulted.
        """
        yield self.overrides.get(f"{product}.{key}")
        yield next(
            (v for e in ENV_VARS.get((product, key), ()) if (v := os.environ.get(e))),
            None,
        )
        raw = self._product_block(product).get(key)
        yield _deref(raw, allow_env=self._env_allowed(raw))

    def _explicit(self, product: str, key: str) -> Any:
        """The tiers a human supplied: flag, then environment, then profile."""
        return next((v for v in self._tiers(product, key) if v is not None), None)

    def _resolve(self, product: str, key: str, default: Any = None) -> Any:
        if (value := self._explicit(product, key)) is not None:
            return value

        if default is not None:
            return default
        if key == "base_url":
            return DEFAULT_BASE_URLS.get(product)
        return None

    def _env_refused(self, raw: Any) -> bool:
        """Whether this value names an environment variable this file may not read.

        Pure, so a report can ask the same question `_resolve` does without the
        side effect of warning about a value it is only describing.
        """
        return self.file.is_project_local and (
            isinstance(raw, str) and raw.startswith("env:")
        )

    def _env_allowed(self, raw: Any) -> bool:
        """Whether this value may name an environment variable to read."""
        if not self._env_refused(raw):
            return True
        # Straight to stderr rather than onto `file.warnings`: those are
        # reported when the file is loaded, and this is found while resolving.
        if raw not in self._reported:
            self._reported.add(raw)
            warn(
                f"warning: ignoring {raw!r} in the project-local "
                f"{self.file.path}: a config file found by searching upwards "
                "may not choose which environment variable is read."
            )
        return False

    def _env_refusal_detail(self, raw: Any) -> str:
        """Why an `env:` reference was not followed."""
        return (
            f"{self.file.path} is a discovered {PROJECT_CONFIG_NAME}, which may "
            f"not choose which environment variable is read, so {raw!r} is ignored"
        )

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
        if key not in SECRET_SETTINGS:
            hints.append(f"or pass --{key.replace('_', '-')}")
        raise ConfigError(
            f"Missing required setting {product}.{key}. To fix: {'; '.join(hints)}."
        )

    def deployment_names(self) -> tuple[str, ...]:
        """The deployments the active profile holds a key of its own for."""
        table = self._profile().get("deployments")
        return tuple(sorted(table)) if isinstance(table, dict) else ()

    def deployment_key(self, api_name: str) -> Any:
        """The key to run one deployment with, or ``None`` if nothing names one.

        **flag > env > per-deployment entry > profile key**. The entry is the
        most specific value *within* the profile tier, not a tier of its own: a
        file value that outranked the environment would let a stale entry hijack
        a run the caller set up with ``$UNSTRACT_DEPLOYMENT_KEY``.
        """
        tiers = self._tiers(DOCSTUDIO, "api_key")
        flag, env = next(tiers), next(tiers)
        value = flag if flag is not None else env
        if value is None:
            value = self._entry_key(api_name)
        if value is None:
            value = next(tiers)
        remember_secret(value)
        return value

    def _entry_key(self, api_name: str) -> Any:
        """The key the deployment's own entry names, if it names one.

        An ``env:`` reference that does not resolve is not silence. Falling back
        there runs the deployment with the profile's key and reports success.
        """
        table = self._profile().get("deployments")
        entry = table.get(api_name) if isinstance(table, dict) else None
        raw = entry.get("api_key") if isinstance(entry, dict) else None
        # A discovered project file never gets this far: its keys are withheld
        # at load time, so an `env:` reference seen here is always trusted.
        if isinstance(raw, str) and raw.startswith("env:"):
            if value := _deref(raw, allow_env=True):
                return value
            raise ConfigError(
                f"Deployment {api_name!r} sets api_key to {raw!r}, and "
                f"${raw[4:].strip()} is not set in this process's environment."
            )
        return raw or None

    def deployment_key_sources(self, api_name: str) -> tuple[str, ...]:
        """Every place a key for this deployment could have come from, in order.

        Quoted when none of them did: a caller told only that a key is missing
        has to guess which of four places the CLI looked in.
        """
        profile = self.active_profile or "<name>"
        return (
            "--api-key",
            f"${ENV_VARS[(DOCSTUDIO, 'api_key')][0]}",
            f'[profiles.{profile}.deployments."{api_name}"] api_key',
            f"[profiles.{profile}.docstudio] api_key",
        )

    def resolution_source(self, product: str, key: str) -> dict[str, Any]:
        """Report where a setting resolves from, without echoing a secret.

        `config doctor` uses this to answer the question that costs the most
        time: "the CLI says the key is not configured, but I set it -- where is
        it looking?"
        """
        if self.overrides.get(f"{product}.{key}") is not None:
            return {"resolved": True, "source": "flag/override"}

        for env_var in ENV_VARS.get((product, key), ()):
            if os.environ.get(env_var):
                return {"resolved": True, "source": f"env:{env_var}"}

        raw = self._product_block(product).get(key)
        if isinstance(raw, str) and raw.startswith("env:"):
            var = raw[4:].strip()
            if self._env_refused(raw):
                return {
                    "resolved": False,
                    "source": f"profile -> env:{var} (refused)",
                    "detail": self._env_refusal_detail(raw),
                }
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
        product/key pair so a deployment entry's own key -- nested a level deeper
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

    No ``platform_key`` and no ``deployments`` table on purpose: a platform key
    is optional and an ``env:`` reference to an unset variable is a `config
    doctor` problem, and one deployment key normally covers the organisation,
    so a per-deployment key is the exception rather than the shape to start
    from.
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
        },
    }


__all__ = [
    "DEFAULT_BASE_URLS",
    "DOCSTUDIO",
    "ENV_VARS",
    "HOME_CONFIG",
    "KEY_SOURCES",
    "LLMWHISPERER",
    "PRODUCTS",
    "PROJECT_CONFIG_NAME",
    "SECRET_SETTINGS",
    "UNTRUSTED_PROJECT_KEYS",
    "ConfigError",
    "ConfigFile",
    "ResolvedConfig",
    "config_path",
    "find_project_config",
    "init_path",
    "load_config",
    "save_config",
    "set_config_path",
    "settings_for",
    "starter_profiles",
]
