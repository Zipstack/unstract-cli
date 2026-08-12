"""`unstract whisper ...` -- text and layout extraction.

Every flag below the command name is derived from the committed spec, so this
module holds only what the spec cannot say: which parameter is an argument,
which the CLI owns, and how a result is polled for and retrieved.
"""

from __future__ import annotations

from typing import Any

import click
from unstract.llmwhisperer.client_v2 import LLMWhispererClientV2

from unstract_cli.app import Context, pass_context, whisper_group
from unstract_cli.commands.common import finish, wait_options
from unstract_cli.core.clients import llmwhisperer, translated
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.params import requested, spec_options
from unstract_cli.core.poll import PollSpec, persist, wait_for_completion

PRODUCT = "llmwhisperer"

#: An extraction is finished when the *body* says so. `unknown` is terminal too:
#: the service reports it for a hash it no longer knows, and polling one forever
#: is worse than reporting it.
EXTRACT_POLL = PollSpec(
    handle_field="whisper_hash",
    terminal_success=("processed",),
    terminal_failure=("error", "unknown"),
    status_field="status",
)

#: `--output raw` prints one field rather than the whole payload. Extraction
#: results carry the text under this name.
RAW_FIELD = "result_text"


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


@whisper_group.command("extract")
@click.argument("source")
@wait_options()
@spec_options(
    PRODUCT,
    "extract",
    client_method=LLMWhispererClientV2.whisper,
    # `url` is the SOURCE argument when it looks like one.
    exclude=("url",),
)
@pass_context
def extract(
    ctx: Context,
    source: str,
    wait: bool,
    interval: float,
    wait_timeout: float,
    save: str | None,
    **params: Any,
) -> None:
    """Extract text from a document, given a file path or a URL.

    With --wait (the default) this returns the extracted text. With --no-wait it
    returns the whisper_hash, and `whisper status` and `whisper retrieve` take
    it from there.
    """
    client = llmwhisperer(ctx.config)
    sent = requested(params)

    if sent.get("use_webhook") and wait:
        raise CLIError(
            "--wait and --use-webhook are mutually exclusive.",
            ExitCode.USAGE,
            hint=(
                "A webhook delivers the result itself; pass --no-wait to submit "
                "and return immediately."
            ),
        )

    with translated(endpoint="whisper"):
        # The client has its own blocking loop; the CLI's is used instead so
        # that --interval, --timeout and the handle-on-timeout behaviour are the
        # same for every product.
        accepted = client.whisper(
            **({"url": source} if _is_url(source) else {"file_path": source}),
            **sent,
            wait_for_completion=False,
        )

        if not wait:
            finish(ctx, accepted)
            return

        result = wait_for_completion(
            initial=accepted,
            spec=EXTRACT_POLL,
            poll=client.whisper_status,
            retrieve=lambda handle: client.whisper_retrieve(handle).get("extraction"),
            save=save,
            interval=interval,
            timeout=wait_timeout,
            on_status=lambda status: (
                click.echo(f"status: {status}", err=True) if not ctx.quiet else None
            ),
        )
    finish(ctx, result, raw_field=RAW_FIELD)


@whisper_group.command("status")
@click.argument("whisper_hash")
@pass_context
def status(ctx: Context, whisper_hash: str) -> None:
    """Report the state of a submitted extraction."""
    client = llmwhisperer(ctx.config)
    with translated(endpoint="whisper-status"):
        finish(ctx, client.whisper_status(whisper_hash))


@whisper_group.command("retrieve")
@click.argument("whisper_hash")
@click.option(
    "--save",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write the result here before printing it.",
)
@pass_context
def retrieve(ctx: Context, whisper_hash: str, save: str | None) -> None:
    """Fetch a finished extraction.

    A result can be read exactly once, so --save writes it to disk before it is
    printed: a broken pipe or a full terminal buffer after the read cannot be
    recovered by asking again.
    """
    client = llmwhisperer(ctx.config)
    with translated(endpoint="whisper-retrieve"):
        payload = client.whisper_retrieve(whisper_hash)
    result = payload.get("extraction", payload)
    if save:
        persist(save, result)
    finish(ctx, result, raw_field=RAW_FIELD)


@whisper_group.command("detail")
@click.argument("whisper_hash")
@pass_context
def detail(ctx: Context, whisper_hash: str) -> None:
    """Report processing detail for one extraction."""
    client = llmwhisperer(ctx.config)
    with translated(endpoint="whisper-detail"):
        finish(ctx, client.whisper_detail(whisper_hash))


@whisper_group.command("highlights")
@click.argument("whisper_hash")
@spec_options(
    PRODUCT,
    "highlights",
    client_method=LLMWhispererClientV2.get_highlight_data,
    exclude=("whisper_hash",),
)
@click.option(
    "--target-width",
    type=int,
    default=None,
    help="Width of the page as displayed. With --target-height, adds a bounding box per line.",
)
@click.option(
    "--target-height",
    type=int,
    default=None,
    help="Height of the page as displayed.",
)
@pass_context
def highlights(
    ctx: Context,
    whisper_hash: str,
    target_width: int | None,
    target_height: int | None,
    **params: Any,
) -> None:
    """Fetch line metadata, optionally scaled to a page you are rendering.

    The scaling is arithmetic on the metadata, not a second request, so it is
    folded in here rather than being a command of its own.
    """
    client = llmwhisperer(ctx.config)
    with translated(endpoint="highlights"):
        data = client.get_highlight_data(whisper_hash, **requested(params))

    if target_width and target_height:
        data = {
            "lines": data,
            "rects": _bounding_boxes(client, data, target_width, target_height),
        }
    finish(ctx, data)


def _bounding_boxes(
    client: LLMWhispererClientV2,
    data: Any,
    target_width: int,
    target_height: int,
) -> dict[str, list[int]]:
    """(page, x1, y1, x2, y2) per line, for the lines that carry metadata."""
    if not isinstance(data, dict):
        return {}
    return {
        str(line): list(client.get_highlight_rect(metadata, target_width, target_height))
        for line, metadata in data.items()
        if isinstance(metadata, list)
        and len(metadata) >= 4
        and all(isinstance(v, (int, float)) for v in metadata)
    }


@whisper_group.command("usage")
@pass_context
def usage(ctx: Context) -> None:
    """Report this key's usage and remaining quota."""
    client = llmwhisperer(ctx.config)
    with translated(endpoint="get-usage-info"):
        finish(ctx, client.get_usage_info())


@whisper_group.group("webhook")
def webhook_group() -> None:
    """Manage the webhooks an extraction can deliver its result to."""


@webhook_group.command("create")
@click.argument("name")
@click.option("--url", required=True, help="Where the result is delivered.")
@click.option("--auth-token", required=True, help="Token sent with the delivery.")
@pass_context
def webhook_create(ctx: Context, name: str, url: str, auth_token: str) -> None:
    """Register a webhook."""
    client = llmwhisperer(ctx.config)
    with translated(endpoint="whisper-manage-callback"):
        finish(ctx, client.register_webhook(url, auth_token, name))


@webhook_group.command("update")
@click.argument("name")
@click.option("--url", required=True, help="Where the result is delivered.")
@click.option("--auth-token", required=True, help="Token sent with the delivery.")
@pass_context
def webhook_update(ctx: Context, name: str, url: str, auth_token: str) -> None:
    """Replace a webhook's URL and token."""
    client = llmwhisperer(ctx.config)
    with translated(endpoint="whisper-manage-callback"):
        finish(ctx, client.update_webhook_details(name, url, auth_token))


@webhook_group.command("get")
@click.argument("name")
@pass_context
def webhook_get(ctx: Context, name: str) -> None:
    """Show one webhook's configuration."""
    client = llmwhisperer(ctx.config)
    with translated(endpoint="whisper-manage-callback"):
        finish(ctx, client.get_webhook_details(name))


@webhook_group.command("delete")
@click.argument("name")
@pass_context
def webhook_delete(ctx: Context, name: str) -> None:
    """Remove a webhook."""
    client = llmwhisperer(ctx.config)
    with translated(endpoint="whisper-manage-callback"):
        finish(ctx, client.delete_webhook(name))


__all__ = ["extract", "highlights", "retrieve", "status", "usage", "webhook_group"]
