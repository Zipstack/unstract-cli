"""CLI entry point and the machine-readable command index.

`--dump-commands` is the feature that makes this CLI usable by an agent without
external documentation (SPEC.md §5.3). It emits the full command tree as JSON,
combining two sources:

* the **`Endpoint` records**, which carry what Click cannot know -- HTTP method
  and path, the documentation file each record was authored from, the required
  key permission, and one-shot semantics;
* the **introspected Click tree**, which carries the flags exactly as the parser
  will accept them.

Emitting both means the index describes the *API*, not merely the CLI, and cannot
drift from the commands actually registered.
"""

from __future__ import annotations

import json
from typing import Any

import click

from unstract_cli.commands.config_cmd import CONFIG_COMMANDS, config_group
from unstract_cli.core.generate import build_group_tree
from unstract_cli.core.model import Endpoint
from unstract_cli.endpoints import ALL_ENDPOINTS

__version__ = "0.1.0"

#: Flags added to every generated command; excluded from the per-command flag
#: listing in `--dump-commands` so the endpoint's own parameters stand out.
_COMMON_FLAGS = frozenset(
    {
        "output", "profile", "base_url", "dry_run", "quiet", "verbose",
        "max_retries", "no_retry", "timeout", "help",
    }
)


def _param_info(param: click.Parameter) -> dict[str, Any]:
    """Describe one flag using Click's own introspection.

    Uses `to_info_dict()` rather than reading private attributes, so the shape is
    a supported contract; `tests/test_dump_commands.py` asserts the keys we rely
    on still exist, so a Click upgrade fails loudly rather than silently.
    """
    info = param.to_info_dict()
    type_info = info.get("type", {})
    entry: dict[str, Any] = {
        "name": info.get("name"),
        "flags": info.get("opts", []),
        "type": type_info.get("param_type", "String").lower(),
        "required": info.get("required", False),
        "multiple": info.get("multiple", False),
        "is_flag": info.get("is_flag", False),
        "help": info.get("help") or "",
    }
    if (default := info.get("default")) is not None:
        entry["default"] = default
    if choices := type_info.get("choices"):
        entry["choices"] = list(choices)
    return entry


def _endpoint_info(endpoint: Endpoint, command: click.Command) -> dict[str, Any]:
    """Combine an endpoint record with its generated command."""
    entry: dict[str, Any] = {
        "command": "unstract " + " ".join(endpoint.command_path),
        "path": list(endpoint.command_path),
        "kind": "endpoint",
        "summary": endpoint.summary,
        "product": endpoint.product.value,
        "api": {"method": endpoint.method, "path": endpoint.path},
        "flags": [
            _param_info(p)
            for p in command.params
            if p.name not in _COMMON_FLAGS
        ],
    }
    if endpoint.description:
        entry["description"] = endpoint.description
    if endpoint.permission:
        entry["required_permission"] = endpoint.permission.value
    if endpoint.constraints:
        entry["constraints"] = [c.describe() for c in endpoint.constraints]
    if endpoint.poll:
        entry["supports_wait"] = True
        entry["one_shot"] = endpoint.poll.one_shot
    if endpoint.doc_source:
        entry["doc_source"] = endpoint.doc_source
    if endpoint.doc_conflict:
        entry["doc_conflict"] = endpoint.doc_conflict
    if endpoint.examples:
        entry["examples"] = list(endpoint.examples)
    if any(p.client_side and p.name == "save" for p in endpoint.params):
        entry["supports_save"] = True
    return entry


def dump_commands() -> dict[str, Any]:
    """Build the machine-readable index of every command."""
    groups = build_group_tree(list(ALL_ENDPOINTS))
    commands: list[dict[str, Any]] = []

    for endpoint in ALL_ENDPOINTS:
        node: click.Command | None = groups.get(endpoint.group)
        for part in endpoint.command_path[1:]:
            if isinstance(node, click.Group):
                node = node.commands.get(part)
        if node is not None:
            commands.append(_endpoint_info(endpoint, node))

    for hand in CONFIG_COMMANDS:
        commands.append(
            {
                "command": "unstract " + " ".join(hand.command_path),
                "path": list(hand.command_path),
                # Flagged so an agent can distinguish local operations from API calls.
                "kind": "local",
                "summary": hand.summary,
                "flags": [
                    {
                        "name": p.name,
                        "flags": [p.cli_flag],
                        "required": p.required,
                        "help": p.help,
                    }
                    for p in hand.params
                ],
            }
        )

    return {
        "cli": "unstract",
        "version": __version__,
        "description": "Unified CLI for the Unstract suite of products.",
        "exit_codes": {
            "0": "success",
            "1": "generic error",
            "2": "usage error (bad or missing flags)",
            "3": "authentication or authorization failure",
            "4": "not found",
            "5": "validation error rejected by the API",
            "6": "rate limited or quota exceeded",
            "7": "timed out waiting for a terminal state",
            "8": "remote server error",
            "9": "result already consumed (one-shot read)",
        },
        "conventions": {
            "output": (
                "--output json|yaml|table|raw. JSON is the default when stdout is "
                "not a TTY. Payloads go to stdout; diagnostics and structured "
                "errors go to stderr."
            ),
            "errors": (
                "Failures emit a JSON object on stderr with code, message, hint "
                "and retryable."
            ),
            "never_interactive": "No command ever prompts; every input is a flag or env var.",
            "one_shot": (
                "Commands marked one_shot return their result exactly once. Use "
                "--save to persist it; a second read exits 9."
            ),
            "wait": (
                "Commands with supports_wait accept --wait to poll to completion, "
                "plus --poll-interval and --wait-timeout."
            ),
        },
        "commands": commands,
    }


class UnstractCLI(click.Group):
    """Root group that suggests a correction for an unknown command."""

    def resolve_command(self, ctx: click.Context, args: list[str]):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            import difflib

            given = args[0] if args else ""
            if matches := difflib.get_close_matches(given, self.list_commands(ctx), n=3):
                # Suggestions go to stderr so stdout stays parseable.
                click.echo(
                    f"Unknown command {given!r}. Did you mean: {', '.join(matches)}?",
                    err=True,
                )
            raise


@click.group(
    cls=UnstractCLI,
    # `--dump-commands` and a bare `unstract` must both work without a
    # subcommand, so the group has to be invocable on its own.
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.version_option(__version__, "-V", "--version", prog_name="unstract")
@click.option(
    "--dump-commands",
    "dump",
    is_flag=True,
    default=False,
    help="Emit the full command tree as JSON, for programmatic discovery.",
)
@click.option("--profile", "-p", default=None, help="Config profile to use.")
@click.pass_context
def cli(ctx: click.Context, dump: bool, profile: str | None) -> None:
    """Unified, LLM-friendly CLI for the Unstract suite.

    Products: LLMWhisperer text extraction (`whisper`), deployed API workflows
    (`deployment`), platform management (`platform`), human review (`hitl`) and
    API Hub vertical extraction (`apihub`).

    Machine-readable discovery:

      unstract --dump-commands

    Output defaults to JSON whenever stdout is not a terminal, so piping the CLI
    needs no extra flags.
    """
    if dump:
        click.echo(json.dumps(dump_commands(), indent=2))
        ctx.exit(0)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(0)


def build_cli() -> click.Group:
    """Assemble the full command tree."""
    for group in build_group_tree(list(ALL_ENDPOINTS)).values():
        cli.add_command(group)
    cli.add_command(config_group)
    return cli


__all__ = ["__version__", "build_cli", "cli", "dump_commands"]
