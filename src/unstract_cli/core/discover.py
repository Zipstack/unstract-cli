"""`--discover`: the CLI describing itself, in three tiers.

An agent driving this CLI needs to know what exists before it can run anything,
and `--help` is prose scraped from a terminal. Discovery answers the same
question as JSON, at whichever depth the question needs:

* ``groups`` -- what products are here at all
* ``summary`` -- what commands each group has
* ``full`` -- every flag with its type, default and allowed values, plus the
  exit codes and the output contract, which is enough to construct a call and
  read its answer without a second round trip

Every tier is read back from Click itself. Describing commands from anywhere
else lets the description drift from what the parser accepts.
"""

from __future__ import annotations

from typing import Any

import click

from unstract_cli.core.errors import _ERROR_CODES, ExitCode
from unstract_cli.core.output import CONTRACT_VERSION

TIERS = ("groups", "summary", "full")

#: Click's marker for "no default was given". It stopped being `None` in 8.2 and
#: is not exported, so it is read off a bare option and tracks whichever version
#: is installed -- serialised, it would publish a string that reads as a value.
_NO_DEFAULT = click.Option(["--unset"]).default

#: The same question for a paired on/off flag, which answers it differently:
#: given no default, some versions report `False` and others their own sentinel.
#: Read the same way, so neither is mistaken for a default the flag really has.
_NO_FLAG_DEFAULT = click.Option(["--unset/--no-unset"], default=None).default


def contract() -> dict[str, Any]:
    """How to consume this CLI's output, published rather than assumed.

    Both halves of the compatibility bargain are written down here: what we
    promise not to break, and what a consumer has to do for that promise to be
    worth anything.
    """
    return {
        "version": CONTRACT_VERSION,
        "envelope": ["ok", "data", "error", "meta"],
        "rules": [
            "Pass `-o json`. The default format is for people and is free to "
            "change; json is the parseable one and never varies with the "
            "terminal, the config or the environment.",
            "Ignore fields you do not recognise. New ones are added within a "
            "major version.",
            "Refuse a `meta.contract_version` whose value is greater than the "
            "one you were written against: the shape has changed under you.",
            "Branch on the exit code, not on the message text.",
            "Read stdout for the envelope only. Diagnostics are on stderr.",
        ],
    }


def exit_codes() -> list[dict[str, Any]]:
    """The exit-code table, which is part of the contract callers branch on."""
    return [
        {
            "code": int(code),
            "name": code.name.lower(),
            "error_code": _ERROR_CODES.get(code, ""),
        }
        for code in ExitCode
    ]


def _param(param: click.Parameter) -> dict[str, Any]:
    """One flag or argument, in the terms a caller needs to supply it."""
    entry: dict[str, Any] = {
        "name": param.name,
        "kind": "argument" if isinstance(param, click.Argument) else "option",
        "type": getattr(param.type, "name", "text"),
        "required": bool(param.required),
    }
    if isinstance(param, click.Option):
        entry["flags"] = list(param.opts) + list(param.secondary_opts)
        entry["help"] = param.help or ""
        entry["repeatable"] = bool(param.multiple)
    if isinstance(param.type, click.Choice):
        entry["choices"] = list(param.type.choices)
    # What omitting the flag actually gets you, which is not what Click reports:
    # the same declaration answers differently across the supported range, so
    # reading `param.default` straight publishes a contract per version.
    default = param.default
    if param.secondary_opts and default is _NO_FLAG_DEFAULT:
        # An on/off flag the CLI declares with no default means "not passed, so
        # not sent". Publishing the `False` some versions report here would
        # promise a value the CLI does not send.
        default = None
    elif default is _NO_DEFAULT:
        default = False if getattr(param, "is_flag", False) else None
    if default is not None and not isinstance(param, click.Argument):
        entry["default"] = default
    return entry


def _params(command: click.Command) -> list[dict[str, Any]]:
    """The flags a caller can pass to one command, group or the root.

    A group carries the connection settings for everything beneath it, so
    describing only the leaves describes a call nobody can make.
    """
    return [_param(p) for p in command.params if p.name not in ("help", "discover")]


def _describe(command: click.Command, tier: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"help": (command.help or "").strip().split("\n")[0]}
    if tier == "full":
        entry["params"] = _params(command)
        # What `--output raw` prints for this command, best answer first: the
        # first of these the answer carries is the one printed, and an answer
        # carrying none of them fails rather than printing something else.
        if raw := getattr(command, "raw_fields", ()):
            entry["raw_fields"] = list(raw)
    if isinstance(command, click.Group):
        entry["commands"] = {
            name: _describe(sub, tier) for name, sub in sorted(command.commands.items())
        }
    return entry


def discover(root: click.Group, tier: str) -> dict[str, Any]:
    """Describe the CLI at one tier.

    ``groups`` stops at the top level rather than walking further, so the cheap
    question stays cheap: an agent starts here and drills down only where it
    needs to.
    """
    if tier not in TIERS:
        raise ValueError(f"Unknown discovery tier {tier!r}. One of: {', '.join(TIERS)}")

    if tier == "groups":
        top = sorted(root.commands.items())

        def summary(name: str, command: click.Command) -> dict[str, str]:
            return {"name": name, "help": (command.help or "").strip().split("\n")[0]}

        return {
            "tier": tier,
            "groups": [
                summary(name, sub) for name, sub in top if isinstance(sub, click.Group)
            ],
            # Leaf commands are listed apart from the groups, so a consumer
            # walking groups for their commands does not drop them.
            "commands": [
                summary(name, sub)
                for name, sub in top
                if not isinstance(sub, click.Group)
            ],
        }

    payload: dict[str, Any] = {
        "tier": tier,
        "commands": {
            name: _describe(sub, tier) for name, sub in sorted(root.commands.items())
        },
    }
    if tier == "full":
        payload["params"] = _params(root)
        payload["exit_codes"] = exit_codes()
        payload["contract"] = contract()
    return payload


__all__ = ["TIERS", "contract", "discover", "exit_codes"]
