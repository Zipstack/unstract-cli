"""CLI entry point and the machine-readable command index.

`--discover` is the feature that makes this CLI usable by an agent without
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
import sys
from typing import Any

import click

from unstract_cli.commands.config_cmd import config_group
from unstract_cli.config.loader import set_config_path
from unstract_cli.core.generate import build_group_tree
from unstract_cli.core.model import Endpoint
from unstract_cli.endpoints import ALL_ENDPOINTS

__version__ = "0.1.0"

#: Flags added to every generated command; excluded from the per-command flag
#: listing in `--discover` so the endpoint's own parameters stand out.
_COMMON_FLAGS = frozenset(
    {
        "output", "profile", "base_url", "dry_run", "quiet", "verbose",
        "max_retries", "no_retry", "timeout", "help",
    }
)


def _param_info(param: click.Parameter) -> dict[str, Any]:
    """Describe one parameter using Click's own introspection.

    Uses `to_info_dict()` rather than reading private attributes, so the shape is
    a supported contract; `tests/test_cli.py` asserts the keys we rely
    on still exist, so a Click upgrade fails loudly rather than silently.

    Crucially this reports whether the parameter is an **option** (``--flag x``)
    or a positional **argument** (``x``). Getting that wrong makes the index
    actively misleading: an agent would construct a command line the parser
    rejects.
    """
    info = param.to_info_dict()
    type_info = info.get("type", {})
    kind = "argument" if info.get("param_type_name") == "argument" else "option"
    entry: dict[str, Any] = {
        "name": info.get("name"),
        "kind": kind,
        "type": type_info.get("param_type", "String").lower(),
        "required": info.get("required", False),
        "multiple": info.get("multiple", False),
        "help": info.get("help") or "",
    }
    if kind == "option":
        entry["flags"] = info.get("opts", [])
        entry["is_flag"] = info.get("is_flag", False)
    else:
        # Positional: how it is written on the command line, not a flag name.
        entry["usage"] = getattr(param, "metavar", None) or str(
            info.get("name", "")
        ).upper()
    if (default := info.get("default")) is not None:
        entry["default"] = default
    if choices := type_info.get("choices"):
        entry["choices"] = list(choices)
    return entry


def _usage_line(path: tuple[str, ...], command: click.Command) -> str:
    """Reconstruct the invocation form, so the index shows how to *call* it."""
    parts = ["unstract", *path]
    for param in command.params:
        info = param.to_info_dict()
        if info.get("param_type_name") != "argument":
            continue
        # A variadic argument's own name says nothing useful ("ARGS..."), so
        # prefer a declared metavar, which spells out the real shape. Click
        # omits metavar from to_info_dict(), so read the attribute directly.
        declared = getattr(param, "metavar", None)
        name = declared or str(info.get("name", "")).upper()
        if not declared and (info.get("multiple") or info.get("nargs", 1) == -1):
            name = f"{name}..."
        parts.append(name if info.get("required") else f"[{name}]")
    if any(
        p.to_info_dict().get("param_type_name") != "argument"
        and p.name not in _COMMON_FLAGS
        for p in command.params
    ):
        parts.append("[OPTIONS]")
    return " ".join(parts)


def _endpoint_info(endpoint: Endpoint, command: click.Command) -> dict[str, Any]:
    """Combine an endpoint record with its generated command."""
    entry: dict[str, Any] = {
        "command": "unstract " + " ".join(endpoint.command_path),
        "path": list(endpoint.command_path),
        "kind": "endpoint",
        "summary": endpoint.summary,
        # Which of Unstract's three products this belongs to, and which API
        # surface within it. Document Studio owns three API groups.
        "product": endpoint.product.value,
        "product_name": endpoint.product_label,
        "api_group": endpoint.api.value,
        "api": {"method": endpoint.method, "path": endpoint.path},
        "usage": _usage_line(endpoint.command_path, command),
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


#: Command groups written by hand rather than generated from `Endpoint` records.
#: Introspected like any other command so the index describes what actually runs.
_HAND_AUTHORED_GROUPS: dict[str, click.Group] = {"config": config_group}

#: Fields kept at `--detail summary`. Enough to choose a command, not to call it.
_SUMMARY_FIELDS = ("command", "kind", "summary")

#: Detail levels, cheapest first. Full detail for every command is ~50k tokens and
#: the flat summary list is ~4.5k -- both too much for an agent to read blind -- so
#: the default is `groups`: a ~1k-token map of the ~15 navigable groups with their
#: command counts, from which the agent drills into exactly the subtree it needs.
DETAIL_LEVELS = ("groups", "summary", "full")

#: Below this many (recursive) leaves, a group is a reasonable single drill target
#: and the overview stops descending into it; above it, the overview recurses so no
#: one drill returns an unwieldy slice. Tuned so `docstudio platform` fans out into
#: its resource subgroups while everything else stays one level deep.
_GROUP_OVERVIEW_MAX_LEAVES = 40


def _matches(entry: dict[str, Any], group: str | None, command: str | None) -> bool:
    """Filter one entry by group and/or command prefix.

    `command` matches on a path prefix, so "whisper webhook" selects all four
    webhook commands while "whisper extract" selects exactly one.
    """
    if group and entry["path"][0] != group:
        return False
    if command:
        wanted = command.removeprefix("unstract ").split()
        if entry["path"][: len(wanted)] != wanted:
            return False
    return True


def _navigable_groups(node: click.Group, path: tuple[str, ...]) -> list[dict[str, Any]]:
    """Walk the Click tree into a coarse map of navigable groups (drift-free).

    Emits one entry per group at the level where it is a sensible single drill
    target: a group with more than :data:`_GROUP_OVERVIEW_MAX_LEAVES` reachable
    commands (and subgroups to split it) is descended into instead of emitted, so
    `docstudio platform` fans out into its resource subgroups while smaller groups
    stay one line. Descriptions come from each group's own ``help``, and the
    `drill` command is the exact ``--discover --command`` prefix that selects the
    subtree -- verified by test to return the advertised count, because this
    project's rule is that discovery metadata must never lie about the parser.
    """
    leaves = _count_leaves(node)
    subgroups = sorted(
        (n, c) for n, c in node.commands.items() if isinstance(c, click.Group)
    )
    # Descend only when the group is both too big to be one drill target AND has
    # subgroups to split it on; otherwise emit it whole.
    if leaves > _GROUP_OVERVIEW_MAX_LEAVES and subgroups:
        out: list[dict[str, Any]] = []
        # A group with its own direct leaves plus oversized subgroups still needs a
        # line for those direct leaves (e.g. `docstudio platform` has one).
        if any(not isinstance(c, click.Group) for c in node.commands.values()):
            out.append(_group_entry(node, path, direct_only=True))
        for name, child in subgroups:
            out.extend(_navigable_groups(child, (*path, name)))
        return out
    return [_group_entry(node, path)]


def _count_leaves(node: click.Group) -> int:
    """Total leaf (non-group) commands reachable under a group."""
    total = 0
    for child in node.commands.values():
        total += _count_leaves(child) if isinstance(child, click.Group) else 1
    return total


def _group_entry(
    node: click.Group, path: tuple[str, ...], *, direct_only: bool = False
) -> dict[str, Any]:
    """One line in the group overview: what it is, how big, and how to drill in."""
    count = (
        sum(1 for c in node.commands.values() if not isinstance(c, click.Group))
        if direct_only
        else _count_leaves(node)
    )
    drill = "unstract --discover --command '" + " ".join(path) + "'"
    entry = {
        "group": " ".join(path),
        "commands": count,
        "summary": (node.short_help or node.help or "").strip().split("\n")[0],
        "drill": drill + " --detail summary",
    }
    if direct_only:
        entry["note"] = "direct commands only; subgroups are listed separately"
    return entry


def discover(
    *,
    group: str | None = None,
    command: str | None = None,
    detail: str = "summary",
) -> dict[str, Any]:
    """Build the machine-readable index of the command surface.

    Selection (`group`, `command`) and verbosity (`detail`) are independent, so
    any combination works: a summary of one group, full detail for one command,
    or full detail for everything.
    """
    groups = build_group_tree(list(ALL_ENDPOINTS))
    commands: list[dict[str, Any]] = []

    for endpoint in ALL_ENDPOINTS:
        node: click.Command | None = groups.get(endpoint.group)
        for part in endpoint.command_path[1:]:
            if isinstance(node, click.Group):
                node = node.commands.get(part)
        if node is not None:
            commands.append(_endpoint_info(endpoint, node))

    # Hand-authored groups are introspected from the *actual* Click commands,
    # never from a parallel description of them. A hand-maintained parameter list
    # is exactly the drift the generated half exists to avoid -- it once
    # advertised `--product/--key/--value` for `config set`, which really takes
    # three positional arguments.
    for group_name, click_group in _HAND_AUTHORED_GROUPS.items():
        for name, node in sorted(click_group.commands.items()):
            path = (group_name, name)
            commands.append(
                {
                    "command": "unstract " + " ".join(path),
                    "path": list(path),
                    # Flagged so an agent can tell local operations from API calls.
                    "kind": "local",
                    "summary": (node.short_help or node.help or "").strip(),
                    "usage": _usage_line(path, node),
                    "flags": [
                        _param_info(p)
                        for p in node.params
                        if p.name not in _COMMON_FLAGS
                    ],
                }
            )

    commands = [c for c in commands if _matches(c, group, command)]

    # `groups` is the cheap entry point and only makes sense unfiltered: a filter
    # already narrows to a subtree, so there it degrades to the flat summary list.
    if detail == "groups" and (group or command):
        detail = "summary"

    if detail == "summary":
        commands = [
            {k: c[k] for k in _SUMMARY_FIELDS if k in c} for c in commands
        ]

    envelope: dict[str, Any] = {
        "cli": "unstract",
        "version": __version__,
        "description": (
            "Unstract CLI -- one interface to the three products built by "
            "Unstract: Document Studio, LLMWhisperer and API Hub."
        ),
        "products": {
            "docstudio": {
                "name": "Document Studio",
                "group": "docstudio",
                "note": "Owns the platform, deployment and hitl API groups.",
            },
            "llmwhisperer": {"name": "LLMWhisperer", "group": "whisper"},
            "apihub": {"name": "API Hub", "group": "apihub"},
        },
        "detail": detail,
    }
    if group:
        envelope["group"] = group
    if command:
        envelope["command_filter"] = command

    # `groups`: the ~1k-token map of navigable groups, from which an agent drills
    # into exactly one subtree. This is the default and the entry point, so it also
    # carries the global boilerplate (exit codes, conventions, drill hints).
    if detail == "groups":
        tree = build_group_tree(list(ALL_ENDPOINTS))
        overview: list[dict[str, Any]] = []
        for name in sorted(tree):
            overview.extend(_navigable_groups(tree[name], (name,)))
        for name, hand in sorted(_HAND_AUTHORED_GROUPS.items()):
            overview.extend(_navigable_groups(hand, (name,)))
        return {
            **envelope,
            "group_count": len(overview),
            "command_count": len(commands),
            "how_to_drill": (
                "Each group's `drill` command lists its commands (names + summaries). "
                "Add --detail full to that command for flags and API paths, or use "
                "--detail summary here for the flat list of all commands."
            ),
            "groups": overview,
            **_GLOBAL_FACTS,
        }

    envelope["count"] = len(commands)

    if detail == "summary":
        # The whole point of the summary level is that an agent reads it first,
        # so it has to say how to get the rest.
        envelope["drill_down"] = {
            "one_group": "unstract --discover --group <group> --detail full",
            "one_command": "unstract --discover --command '<group> <cmd>' --detail full",
            "everything": "unstract --discover --detail full",
            "note": (
                "Full detail for every command is large (~60k tokens). Prefer "
                "filtering by group or command."
            ),
        }
        envelope["groups"] = sorted({c["command"].split()[1] for c in commands})

    # The exit-code table and conventions are global facts, not per-command ones.
    # An agent that has already read the unfiltered index knows them, and on a
    # narrow query they would outweigh the answer itself -- so they ship only
    # with the unfiltered view, which is the entry point.
    if group or command:
        return {**envelope, "commands": commands}

    return {
        **envelope,
        **_GLOBAL_FACTS,
        "commands": commands,
    }


#: Global facts an agent branches on: stable across commands, so they ride the
#: unfiltered entry points (the groups overview and the full unfiltered list) and
#: are omitted from narrow queries where they would outweigh the answer.
_GLOBAL_FACTS: dict[str, Any] = {
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
    # `--discover` and a bare `unstract` must both work without a
    # subcommand, so the group has to be invocable on its own.
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.version_option(__version__, "-V", "--version", prog_name="unstract")
@click.option(
    "--discover",
    "discover_flag",
    is_flag=True,
    default=False,
    help="Emit the command tree as JSON, for programmatic discovery.",
)
@click.option(
    "--group",
    default=None,
    help="Limit --discover to one product group (whisper, platform, ...).",
)
@click.option(
    "--command",
    "command_filter",
    default=None,
    help="Limit --discover to one command or command prefix, e.g. 'whisper extract'.",
)
@click.option(
    "--detail",
    type=click.Choice(DETAIL_LEVELS),
    default="groups",
    show_default=True,
    help=(
        "How much detail: 'groups' is the cheap map of navigable groups (default); "
        "'summary' is every command with a one-liner; 'full' adds flags and API paths."
    ),
)
@click.option("--profile", "-p", default=None, help="Config profile to use.")
@click.option(
    "--config",
    "config_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Config file to use, overriding $UNSTRACT_CONFIG and any .unstract.toml.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    discover_flag: bool,
    group: str | None,
    command_filter: str | None,
    detail: str,
    profile: str | None,
    config_file: str | None,
) -> None:
    """Unified, LLM-friendly CLI for the Unstract suite.

    Products: LLMWhisperer text extraction (`whisper`), deployed API workflows
    (`deployment`), platform management (`platform`), human review (`hitl`) and
    API Hub vertical extraction (`apihub`).

    \b
    Machine-readable discovery -- start cheap, then drill in:
      unstract --discover                              # all names + summaries
      unstract --discover --group whisper              # one product
      unstract --discover --command 'whisper extract' --detail full
      unstract --discover --detail full                # everything (large)

    Output defaults to JSON whenever stdout is not a terminal, so piping the CLI
    needs no extra flags.
    """
    # Applied before any subcommand runs, so every load_config() below -- in
    # generated commands and in the `config` group alike -- sees the same file.
    set_config_path(config_file)

    if discover_flag:
        payload = discover(
            group=group, command=command_filter, detail=detail
        )
        # A filtered query returns `commands`; the unfiltered groups overview
        # returns `groups`. "No match" only applies to a filter that hit nothing.
        if (group or command_filter) and not payload.get("commands"):
            from unstract_cli.core.errors import CLIError, ExitCode

            CLIError(
                "No commands matched"
                + (f" --group {group}" if group else "")
                + (f" --command {command_filter!r}" if command_filter else "")
                + ".",
                ExitCode.USAGE,
                hint="Run `unstract --discover` to list valid groups.",
            ).emit()
            ctx.exit(2)
        # Compact when piped: pretty-printing costs ~36% more tokens for an agent
        # that is only going to parse it anyway.
        indent = 2 if sys.stdout.isatty() else None
        click.echo(json.dumps(payload, indent=indent))
        ctx.exit(0)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(0)


@click.command("completion", help="Print a shell completion script.")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion(shell: str) -> None:
    """Emit the completion script for a shell.

    Click generates these from the same command tree, so completions cover every
    generated command without a separate registry.

    \b
    Install with, for example:
      unstract completion zsh > ~/.zfunc/_unstract
      unstract completion bash >> ~/.bashrc
    """
    click.echo(
        f'eval "$(_UNSTRACT_COMPLETE={shell}_source unstract)"'
        if shell != "fish"
        else "eval (env _UNSTRACT_COMPLETE=fish_source unstract)"
    )


def build_cli() -> click.Group:
    """Assemble the full command tree."""
    for group in build_group_tree(list(ALL_ENDPOINTS)).values():
        cli.add_command(group)
    cli.add_command(config_group)
    cli.add_command(completion)
    return cli


__all__ = ["__version__", "build_cli", "cli", "discover"]
