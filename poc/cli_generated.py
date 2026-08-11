"""`deployment execute` / `deployment status` over the GENERATED SDK.

Flags are derived from the generated request model, so a new backend serializer
field becomes a CLI flag with no edit to this file. That claim is the POC's
headline measurement — see RUNBOOK.md §Phase 6.
"""

import inspect
import json
import typing

import attrs
import click
from facade import GeneratedAPIDeploymentsClient
from llmw_facade import GeneratedLLMWhispererClientV2
from sdk_docstudio.models import ExecuteRequest
from sdk_docstudio.types import UNSET
from sdk_llmwhisperer.api.whisper import extract

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


def whisper_options(fn):
    """One click option per query parameter of the generated `extract` operation.

    Derived from `_get_kwargs`, so a parameter added to the Flask handler becomes
    a CLI flag with no edit here — the same claim as `generated_options`, over a
    backend that has no serializers to introspect.
    """
    skip = {"body", "url_in_post", "url_query"}
    for name, spec in reversed(list(inspect.signature(extract._get_kwargs).parameters.items())):
        if name in skip or name == "kwargs":
            continue
        default = spec.default
        kwargs = {"default": None, "help": f"(generated) default={default!r}"}
        if isinstance(default, bool):
            kwargs["type"] = click.BOOL
        elif isinstance(default, int):
            kwargs["type"] = int
        elif isinstance(default, float):
            kwargs["type"] = float
        else:
            kwargs["type"] = str
        fn = click.option(f"--{name.replace('_', '-')}", name, **kwargs)(fn)
    return fn


def llmw_client(base_url, key, timeout):
    client = GeneratedLLMWhispererClientV2(base_url=base_url or "", api_key=key)
    client.api_timeout = timeout
    return client


@cli.group()
def whisper():
    pass


@whisper.command("extract")
@click.argument("file_path", type=click.Path(exists=True), required=False)
@click.option("--source-url", default="", help="Extract from a URL instead of a file")
@click.option("--base-url", envvar="LLMWHISPERER_BASE_URL_V2", default="")
@click.option("--key", envvar="LLMWHISPERER_API_KEY", required=True)
@click.option("--api-timeout", type=int, default=120)
@click.option("--wait/--no-wait", default=True, help="Poll until extraction completes")
@click.option("--wait-timeout", type=int, default=180)
@whisper_options
def whisper_extract(
    file_path, source_url, base_url, key, api_timeout, wait, wait_timeout, **extra
):
    passed = {k: v for k, v in extra.items() if v is not None}
    result = llmw_client(base_url, key, api_timeout).whisper(
        file_path=file_path or "",
        url=source_url,
        wait_for_completion=wait,
        wait_timeout=wait_timeout,
        **passed,
    )
    click.echo(json.dumps(result, indent=2))


@whisper.command("status")
@click.argument("whisper_hash")
@click.option("--base-url", envvar="LLMWHISPERER_BASE_URL_V2", default="")
@click.option("--key", envvar="LLMWHISPERER_API_KEY", required=True)
def whisper_status(whisper_hash, base_url, key):
    client = llmw_client(base_url, key, 120)
    click.echo(json.dumps(client.whisper_status(whisper_hash), indent=2))


@whisper.command("retrieve")
@click.argument("whisper_hash")
@click.option("--base-url", envvar="LLMWHISPERER_BASE_URL_V2", default="")
@click.option("--key", envvar="LLMWHISPERER_API_KEY", required=True)
@click.option("--text-only", is_flag=True, help="Print result_text instead of JSON")
def whisper_retrieve(whisper_hash, base_url, key, text_only):
    result = llmw_client(base_url, key, 120).whisper_retrieve(whisper_hash)
    if text_only:
        click.echo(result["extraction"].get("result_text", ""))
    else:
        click.echo(json.dumps(result, indent=2))


@whisper.command("detail")
@click.argument("whisper_hash")
@click.option("--base-url", envvar="LLMWHISPERER_BASE_URL_V2", default="")
@click.option("--key", envvar="LLMWHISPERER_API_KEY", required=True)
def whisper_detail(whisper_hash, base_url, key):
    client = llmw_client(base_url, key, 120)
    click.echo(json.dumps(client.whisper_detail(whisper_hash), indent=2))


@whisper.command("highlights")
@click.argument("whisper_hash")
@click.option("--lines", required=True, help="e.g. 1-5,7,21-")
@click.option("--extract-all-lines", is_flag=True)
@click.option("--base-url", envvar="LLMWHISPERER_BASE_URL_V2", default="")
@click.option("--key", envvar="LLMWHISPERER_API_KEY", required=True)
def whisper_highlights(whisper_hash, lines, extract_all_lines, base_url, key):
    client = llmw_client(base_url, key, 120)
    result = client.get_highlight_data(whisper_hash, lines, extract_all_lines)
    click.echo(json.dumps(result, indent=2))


@whisper.command("usage")
@click.option("--base-url", envvar="LLMWHISPERER_BASE_URL_V2", default="")
@click.option("--key", envvar="LLMWHISPERER_API_KEY", required=True)
def whisper_usage(base_url, key):
    click.echo(json.dumps(llmw_client(base_url, key, 120).get_usage_info(), indent=2))


@cli.group()
def webhook():
    pass


@webhook.command("register")
@click.argument("webhook_name")
@click.option("--callback-url", required=True)
@click.option("--auth-token", default="")
@click.option("--base-url", envvar="LLMWHISPERER_BASE_URL_V2", default="")
@click.option("--key", envvar="LLMWHISPERER_API_KEY", required=True)
def webhook_register(webhook_name, callback_url, auth_token, base_url, key):
    client = llmw_client(base_url, key, 120)
    result = client.register_webhook(callback_url, auth_token, webhook_name)
    click.echo(json.dumps(result, indent=2))


@webhook.command("update")
@click.argument("webhook_name")
@click.option("--callback-url", required=True)
@click.option("--auth-token", default="")
@click.option("--base-url", envvar="LLMWHISPERER_BASE_URL_V2", default="")
@click.option("--key", envvar="LLMWHISPERER_API_KEY", required=True)
def webhook_update(webhook_name, callback_url, auth_token, base_url, key):
    client = llmw_client(base_url, key, 120)
    result = client.update_webhook_details(webhook_name, callback_url, auth_token)
    click.echo(json.dumps(result, indent=2))


@webhook.command("get")
@click.argument("webhook_name")
@click.option("--base-url", envvar="LLMWHISPERER_BASE_URL_V2", default="")
@click.option("--key", envvar="LLMWHISPERER_API_KEY", required=True)
def webhook_get_cmd(webhook_name, base_url, key):
    client = llmw_client(base_url, key, 120)
    click.echo(json.dumps(client.get_webhook_details(webhook_name), indent=2))


@webhook.command("delete")
@click.argument("webhook_name")
@click.option("--base-url", envvar="LLMWHISPERER_BASE_URL_V2", default="")
@click.option("--key", envvar="LLMWHISPERER_API_KEY", required=True)
def webhook_delete_cmd(webhook_name, base_url, key):
    client = llmw_client(base_url, key, 120)
    click.echo(json.dumps(client.delete_webhook(webhook_name), indent=2))


if __name__ == "__main__":
    cli()
