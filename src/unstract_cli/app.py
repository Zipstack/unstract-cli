"""The root Click application: global options and the command groups.

Global options are declared once here and reach every command through the Click
context, so no command re-implements profile selection or output formatting.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import click

from unstract_cli.commands.config_cmd import config_group
from unstract_cli.config import (
    DOCSTUDIO,
    LLMWHISPERER,
    SECRET_SETTINGS,
    ConfigError,
    ResolvedConfig,
    load_config,
    set_config_path,
)
from unstract_cli.core.clients import DEFAULT_TRANSPORT_TIMEOUT
from unstract_cli.core.discover import TIERS, discover
from unstract_cli.core.errors import CLIError, ExitCode, set_warning_sink
from unstract_cli.core.output import (
    AgentMode,
    OutputFormat,
    diagnostic,
    emit_result,
    resolve_format,
)


@dataclass
class Context:
    """Everything a command needs from the global options."""

    output: OutputFormat = OutputFormat.TABLE
    quiet: bool = False
    verbosity: int = 0
    profile: str | None = None
    #: Socket timeout for the deployment client, which has none of its own.
    transport_timeout: float | None = DEFAULT_TRANSPORT_TIMEOUT
    #: Command-line overrides, keyed `product.setting` -- the top tier of
    #: flag > env > profile > default.
    overrides: dict[str, Any] = field(default_factory=dict)
    _config: ResolvedConfig | None = field(default=None, repr=False)

    @property
    def config(self) -> ResolvedConfig:
        """Load the config lazily, so commands that need none never read a file."""
        if self._config is None:
            try:
                cfg = load_config()
            except ConfigError as exc:
                raise CLIError(str(exc), ExitCode.USAGE) from exc
            for warning in cfg.warnings:
                diagnostic(warning, quiet=self.quiet, verbosity=self.verbosity)
            self._config = ResolvedConfig(
                file=cfg, profile_name=self.profile, overrides=self.overrides
            )
        return self._config

    def override(self, product: str, values: dict[str, Any]) -> None:
        """Record the connection flags given for one product.

        Called from the product group, before any command runs, so the flag tier
        is populated by the time a command resolves anything.
        """
        for key, value in values.items():
            if value is None:
                continue
            if key in SECRET_SETTINGS:
                diagnostic(
                    "warning: a key passed on the command line lands in shell "
                    "history and in the process list. Prefer the environment "
                    "variable or `env:` indirection in a profile.",
                    quiet=self.quiet,
                    verbosity=self.verbosity,
                )
            self.overrides[f"{product}.{key}"] = value

    def secrets(self) -> list[str]:
        """Resolved credentials, for scrubbing anything on its way to a stream."""
        out: list[str] = []
        for product, key in (
            (LLMWHISPERER, "api_key"),
            (DOCSTUDIO, "api_key"),
            (DOCSTUDIO, "platform_key"),
        ):
            try:
                if value := self.config.get(product, key):
                    out.append(str(value))
            except (ConfigError, CLIError):
                # A credential that cannot be resolved is one that cannot be
                # printed either. Raising here would replace a finished report
                # with a config error, after the work it describes is done.
                continue
        return out


pass_context = click.make_pass_decorator(Context, ensure=True)


# `invoke_without_command` so `--discover` is answerable on its own: it is
# how a caller learns which commands exist, so it cannot require one.
@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--config",
    "config_file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Config file to use, overriding discovery.",
)
@click.option("--profile", "-p", default=None, help="Configuration profile to use.")
@click.option(
    "--output",
    "-o",
    default=None,
    type=click.Choice([f.value for f in OutputFormat]),
    help="Output format. Defaults to table, or to json when --agent resolves "
    "to yes; pass it explicitly to parse the output.",
)
@click.option(
    "--agent",
    type=click.Choice([m.value for m in AgentMode]),
    default=AgentMode.AUTO.value,
    help="Whether a coding agent is driving this: sets the default format to "
    "json. Only the default -- an explicit --output always wins.",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    default=False,
    help="Suppress diagnostics on stderr. stdout is unaffected.",
)
@click.option("--verbose", "-v", count=True, help="Increase diagnostic detail.")
@click.option(
    "--discover",
    "discover_tier",
    type=click.Choice(TIERS),
    default=None,
    help="Describe this CLI as JSON instead of running a command, useful for agents.",
)
@click.version_option(package_name="unstract-cli")
@click.pass_context
def cli(
    ctx: click.Context,
    config_file: str | None,
    profile: str | None,
    output: str | None,
    agent: str,
    quiet: bool,
    verbose: int,
    discover_tier: str | None,
) -> None:
    """The official CLI for Unstract.

    LLMWhisperer extracts text and layout from documents; Document Studio runs
    them through API deployments that return structured JSON.

    Scripting or driving this from an agent: `-o json` prints one
    `{ok, data, error, meta}` envelope on stdout and nothing else, failures
    exit non-zero with a stable code, and `--discover groups|summary|full`
    describes the commands, their flags and the output contract as JSON without
    running anything.
    """
    set_config_path(config_file)
    # Filled in rather than replaced: the entry point holds this object so that
    # a failure anywhere below renders in the format resolved here.
    obj = ctx.ensure_object(Context)
    obj.output = resolve_format(output, agent)
    obj.quiet = quiet
    obj.verbosity = verbose
    obj.profile = profile
    # Modules the output layer imports cannot import it back, so their notes
    # reach it through here rather than going straight to stderr unfiltered.
    set_warning_sink(
        lambda message: diagnostic(message, quiet=obj.quiet, verbosity=obj.verbosity)
    )
    if discover_tier:
        # Discovery is how a caller learns what to run, so it has to answer
        # before any configuration exists -- and always as JSON, because the
        # only consumer of a machine-readable description is a machine.
        emit_result(discover(cli, discover_tier), OutputFormat.JSON)
        ctx.exit(int(ExitCode.SUCCESS))
    if ctx.invoked_subcommand is None:
        if obj.output is not OutputFormat.TABLE:
            # stdout carries one envelope and nothing else, and a run naming no
            # command ran nothing -- printing help there and exiting 0 tells a
            # parser the work succeeded and hands it a page of prose.
            raise CLIError(
                "No command given.",
                ExitCode.USAGE,
                hint="`--discover groups` lists what can be run, as JSON.",
            )
        click.echo(ctx.get_help())
        ctx.exit(int(ExitCode.SUCCESS))


#: The connection flags a product group can carry, named after the setting
#: each one overrides.
_CONNECTION_FLAGS: dict[str, str] = {
    "base_url": "Service URL to use.",
    "api_key": "API key to use.",
    "org_id": "Organisation to run against.",
    "platform_key": "Platform key to use, for the commands that take one.",
}


def _connection_options(*settings: str) -> Callable[[Any], Any]:
    """The per-product connection settings, as flags.

    They sit on the product group rather than on each command: they say where to
    connect, which is the same question for every command underneath.
    """
    options = [
        click.option(
            f"--{name.replace('_', '-')}", default=None, help=_CONNECTION_FLAGS[name]
        )
        for name in ("base_url", *settings)
    ]

    def decorate(func: Any) -> Any:
        for option in reversed(options):
            func = option(func)
        return func

    return decorate


@cli.group("whisper")
@_connection_options("api_key")
@pass_context
def whisper_group(ctx: Context, **overrides: str | None) -> None:
    """Extract text and layout from documents with LLMWhisperer."""
    ctx.override(LLMWHISPERER, overrides)


@cli.group("docstudio")
@_connection_options("api_key", "org_id", "platform_key")
@click.option(
    "--transport-timeout",
    type=click.FloatRange(min=0),
    default=DEFAULT_TRANSPORT_TIMEOUT,
    show_default=True,
    help="Seconds before a stalled connection is given up on. 0 removes the "
    "bound for `deployment run` and `status`, and leaves `deployment ls` on the "
    "platform client's own 60s default.",
)
@pass_context
def docstudio_group(
    ctx: Context, transport_timeout: float, **overrides: str | None
) -> None:
    """Run Document Studio API deployments."""
    ctx.transport_timeout = transport_timeout or None
    ctx.override(DOCSTUDIO, overrides)


@docstudio_group.group("deployment")
def deployment_group() -> None:
    """Work with a deployed API."""


@cli.group("auth")
@_connection_options("platform_key")
@click.option(
    "--transport-timeout",
    type=float,
    default=None,
    help="Seconds before a stalled connection is given up on. Unset means the "
    "client's own default, which is 60.",
)
@pass_context
def auth_group(
    ctx: Context, transport_timeout: float | None, **overrides: str | None
) -> None:
    """Sign in, and identify the credential you are using.

    Its flags configure the platform key, which is the credential that knows
    which organisation it belongs to. A deployment key does not: it authenticates
    against the deployment it was minted for and never reaches this endpoint.
    """
    ctx.transport_timeout = transport_timeout
    ctx.override(DOCSTUDIO, overrides)


cli.add_command(config_group)

# Imported for their side effect of registering commands, and imported last
# because those modules hang their commands off the groups declared just above.
from unstract_cli.commands import (  # noqa: E402,F401
    clone_cmd,
    docstudio_cmd,
    platform_cmd,
    whisper_cmd,
)


def command_tree() -> dict[str, Any]:
    """The registered command tree, read back from Click itself.

    Describing commands anywhere but from the parser lets the description drift
    from what the parser accepts, so discovery and help always read this.
    """

    def walk(command: click.Command) -> dict[str, Any]:
        entry: dict[str, Any] = {"help": (command.help or "").strip().split("\n")[0]}
        if isinstance(command, click.Group):
            entry["commands"] = {
                name: walk(sub) for name, sub in sorted(command.commands.items())
            }
        return entry

    return walk(cli)["commands"]


__all__ = ["Context", "cli", "command_tree", "pass_context"]
