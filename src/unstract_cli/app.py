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
    ConfigError,
    ResolvedConfig,
    load_config,
    set_config_path,
)
from unstract_cli.core.discover import TIERS, discover
from unstract_cli.core.errors import CLIError, ExitCode
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
    transport_timeout: float | None = None
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
            if key == "api_key":
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
        for product in (LLMWHISPERER, DOCSTUDIO):
            try:
                if value := self.config.get(product, "api_key"):
                    out.append(str(value))
            except ConfigError:
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
    help="Output format. Defaults to table; pass json to parse the output.",
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
    help="Describe this CLI as JSON instead of running a command.",
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

    Extract documents with LLMWhisperer and run API deployments. `--discover
    groups` maps every command as JSON; pass `-o json` when scripting or parsing
    the output.
    """
    set_config_path(config_file)
    ctx.obj = Context(
        output=resolve_format(output, agent),
        quiet=quiet,
        verbosity=verbose,
        profile=profile,
    )
    if discover_tier:
        # Answered without a subcommand and without touching configuration:
        # discovery is how a caller finds out what to run, so it must work
        # before anything is set up.
        emit_result(discover(cli, discover_tier), ctx.obj.output)
        ctx.exit(int(ExitCode.SUCCESS))
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(int(ExitCode.SUCCESS))


def _connection_options(*, org_id: bool = False) -> Callable[[Any], Any]:
    """The per-product connection settings, as flags.

    They sit on the product group rather than on each command: they say where to
    connect, which is the same question for every command underneath.
    """
    options = [
        click.option("--base-url", default=None, help="Service URL to use."),
        click.option("--api-key", default=None, help="API key to use."),
    ]
    if org_id:
        options.append(
            click.option("--org-id", default=None, help="Organisation to run against.")
        )

    def decorate(func: Any) -> Any:
        for option in reversed(options):
            func = option(func)
        return func

    return decorate


@cli.group("whisper")
@_connection_options()
@pass_context
def whisper_group(ctx: Context, **overrides: str | None) -> None:
    """Extract text and layout from documents with LLMWhisperer."""
    ctx.override(LLMWHISPERER, overrides)


@cli.group("docstudio")
@_connection_options(org_id=True)
@click.option(
    "--transport-timeout",
    type=float,
    default=None,
    help="Seconds before a stalled connection is given up on. Unset means it "
    "is not, which is what the client has always done.",
)
@pass_context
def docstudio_group(
    ctx: Context, transport_timeout: float | None, **overrides: str | None
) -> None:
    """Run Document Studio API deployments."""
    ctx.transport_timeout = transport_timeout
    ctx.override(DOCSTUDIO, overrides)


@docstudio_group.group("deployment")
def deployment_group() -> None:
    """Work with a deployed API."""


cli.add_command(config_group)

# Imported for their side effect of registering commands, and imported last
# because those modules hang their commands off the groups declared just above.
from unstract_cli.commands import clone_cmd, docstudio_cmd, whisper_cmd  # noqa: E402,F401


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
