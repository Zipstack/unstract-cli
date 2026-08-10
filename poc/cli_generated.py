"""`deployment execute` / `deployment status` over the GENERATED SDK.

Flags are derived from the generated request model, so a new backend serializer
field becomes a CLI flag with no edit to this file. That claim is the POC's
headline measurement — see RUNBOOK.md §Phase 6.
"""

import json
import typing

import attrs
import click
from facade import GeneratedAPIDeploymentsClient
from sdk_docstudio.models import ExecuteRequest
from sdk_docstudio.types import UNSET

# `additional_properties` is generator bookkeeping, not an API parameter; the
# rest are handled by dedicated arguments/options.
NOT_A_FLAG = {"files", "timeout", "include_metadata", "additional_properties"}

# Generated models carry string annotations; resolve them once.
attrs.resolve_types(ExecuteRequest)


def _click_type(annotation):
    """attrs annotation -> (click type, is_flag, multiple)."""
    args = [a for a in typing.get_args(annotation) if a is not type(UNSET)]
    inner = args[0] if args else annotation
    origin = typing.get_origin(inner)
    if origin in (list, typing.List):  # noqa: UP006
        return str, False, True
    if inner is bool:
        return None, True, False
    if inner is int:
        return int, False, False
    if inner is float:
        return float, False, False
    return str, False, False


def generated_options(fn):
    """One click option per field of the generated request model."""
    for field in reversed(attrs.fields(ExecuteRequest)):
        if field.name in NOT_A_FLAG:
            continue
        ctype, is_flag, multiple = _click_type(field.type)
        kwargs = {"default": None, "help": f"(generated) {field.name}"}
        if is_flag:
            kwargs["is_flag"] = True
            kwargs["default"] = False
        else:
            kwargs["type"] = ctype
            kwargs["multiple"] = multiple
        fn = click.option(f"--{field.name.replace('_', '-')}", field.name, **kwargs)(fn)
    return fn


@click.group()
def cli():
    pass


@cli.group()
def deployment():
    pass


@deployment.command("execute")
@click.argument("files", nargs=-1, required=True)
@click.option("--url", envvar="UNSTRACT_API_URL", required=True)
@click.option("--key", envvar="UNSTRACT_API_KEY", required=True)
@click.option("--timeout", type=int, default=-1)
@click.option("--include-metadata", is_flag=True)
@generated_options
def execute_cmd(files, url, key, timeout, include_metadata, **extra):
    client = GeneratedAPIDeploymentsClient(
        api_url=url, api_key=key, api_timeout=timeout, include_metadata=include_metadata
    )
    passed = {k: v for k, v in extra.items() if v not in (None, (), False)}
    passed = {k: list(v) if isinstance(v, tuple) else v for k, v in passed.items()}
    click.echo(json.dumps(client.structure_file(list(files), **passed), indent=2))


@deployment.command("status")
@click.argument("status_endpoint")
@click.option("--url", envvar="UNSTRACT_API_URL", required=True)
@click.option("--key", envvar="UNSTRACT_API_KEY", required=True)
@click.option("--include-metadata", is_flag=True)
def status_cmd(status_endpoint, url, key, include_metadata):
    client = GeneratedAPIDeploymentsClient(
        api_url=url, api_key=key, include_metadata=include_metadata
    )
    click.echo(json.dumps(client.check_execution_status(status_endpoint), indent=2))


if __name__ == "__main__":
    cli()
