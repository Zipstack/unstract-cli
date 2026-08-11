"""The root Click application: global options and the command groups.

Global options are declared once here and reach every command through the Click
context, so no command re-implements profile selection or output formatting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import click

from unstract_cli.commands.config_cmd import config_group
from unstract_cli.config import ConfigError, ResolvedConfig, load_config, set_config_path
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.output import OutputFormat, diagnostic


@dataclass
class Context:
    """Everything a command needs from the global options."""

    output: OutputFormat = OutputFormat.JSON
    quiet: bool = False
    verbosity: int = 0
    profile: str | None = None
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
            self._config = ResolvedConfig(file=cfg, profile_name=self.profile)
        return self._config

    def secrets(self) -> list[str]:
        """Resolved credentials, for scrubbing anything on its way to a stream."""
        from unstract_cli.config import DOCSTUDIO, LLMWHISPERER

        out: list[str] = []
        for product in (LLMWHISPERER, DOCSTUDIO):
            try:
                if value := self.config.get(product, "api_key"):
                    out.append(str(value))
            except ConfigError:
                continue
        return out


pass_context = click.make_pass_decorator(Context, ensure=True)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
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
    type=click.Choice([f.value for f in OutputFormat]),
    default=OutputFormat.JSON.value,
    help="Output format. JSON is the default everywhere, including a terminal.",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    default=False,
    help="Suppress diagnostics on stderr. stdout is unaffected.",
)
@click.option("--verbose", "-v", count=True, help="Increase diagnostic detail.")
@click.version_option(package_name="unstract-cli")
@click.pass_context
def cli(
    ctx: click.Context,
    config_file: str | None,
    profile: str | None,
    output: str,
    quiet: bool,
    verbose: int,
) -> None:
    """Unstract CLI: extract documents and run API deployments.

    stdout always carries one JSON envelope -- {ok, data, error, meta} -- so
    output parses without checking whether a terminal is attached. Diagnostics go
    to stderr.
    """
    set_config_path(config_file)
    ctx.obj = Context(
        output=OutputFormat(output),
        quiet=quiet,
        verbosity=verbose,
        profile=profile,
    )


@cli.group("whisper")
def whisper_group() -> None:
    """Extract text and layout from documents with LLMWhisperer."""


@cli.group("docstudio")
def docstudio_group() -> None:
    """Run Document Studio API deployments."""


@docstudio_group.group("deployment")
def deployment_group() -> None:
    """Work with a deployed API."""


cli.add_command(config_group)


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
