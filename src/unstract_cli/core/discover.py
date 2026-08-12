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
    if param.default is not None and not isinstance(param, click.Argument):
        entry["default"] = param.default
    return entry


def _describe(command: click.Command, tier: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"help": (command.help or "").strip().split("\n")[0]}
    if tier == "full" and not isinstance(command, click.Group):
        entry["params"] = [
            _param(p) for p in command.params if p.name not in ("help", "discover")
        ]
        # Which field `--output raw` prints for this command, where it has one.
        if raw := getattr(command, "raw_field", None):
            entry["raw_field"] = raw
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
        return {
            "tier": tier,
            "groups": [
                {"name": name, "help": (sub.help or "").strip().split("\n")[0]}
                for name, sub in sorted(root.commands.items())
            ],
        }

    payload: dict[str, Any] = {
        "tier": tier,
        "commands": {
            name: _describe(sub, tier) for name, sub in sorted(root.commands.items())
        },
    }
    if tier == "full":
        payload["exit_codes"] = exit_codes()
        payload["contract"] = contract()
    return payload


__all__ = ["TIERS", "contract", "discover", "exit_codes"]
