"""`unstract clone` -- copying one organization's resources into another.

Two endpoints, each with its own key, so this command takes them as flags rather
than from a profile: a profile describes one connection.
"""

from __future__ import annotations

import logging
from typing import Any

import click

# The size grammar and the list syntax come from the client rather than a copy
# here, so both spellings of this command accept the same strings.
from unstract.clone.cli import _parse_size, _split_csv
from unstract.clone.context import (
    DEFAULT_CONCURRENCY,
    CloneOptions,
    OrgEndpoint,
)
from unstract.clone.exceptions import CloneError
from unstract.clone.orchestrator import clone as run_clone
from unstract.clone.report import CloneReport

from unstract_cli.app import Context, cli, pass_context
from unstract_cli.commands.common import finish
from unstract_cli.core.errors import (
    CLIError,
    ExitCode,
    known_secrets,
    remember_secret,
    scrub,
)
from unstract_cli.core.output import OutputFormat


@cli.command("clone")
@click.option("--source-url", required=True, help="Base URL of the source deployment.")
@click.option(
    "--source-org", required=True, help="Source organization_id (slug in the URL path)."
)
@click.option(
    "--source-key",
    envvar="UNSTRACT_SRC_PLATFORM_KEY",
    required=True,
    help="Source admin's Platform API key (or env UNSTRACT_SRC_PLATFORM_KEY).",
)
@click.option("--target-url", required=True, help="Base URL of the target deployment.")
@click.option(
    "--target-org", required=True, help="Target organization_id (slug in the URL path)."
)
@click.option(
    "--target-key",
    envvar="UNSTRACT_TGT_PLATFORM_KEY",
    required=True,
    help="Target admin's Platform API key (or env UNSTRACT_TGT_PLATFORM_KEY).",
)
@click.option(
    "--dry-run", is_flag=True, help="Plan only -- do not write anything to the target."
)
@click.option(
    "--include", default=None, help="Comma-separated phases to run (default: all)."
)
@click.option("--exclude", default=None, help="Comma-separated phases to skip.")
@click.option(
    "--on-name-conflict",
    type=click.Choice(["adopt", "abort"]),
    default="adopt",
    show_default=True,
    help="What to do when a like-named entity exists on the target.",
)
@click.option(
    "--api-prefix",
    default="api/v1",
    show_default=True,
    help="Backend URL prefix, matching the deployment's own.",
)
@click.option(
    "--file-strategy",
    type=click.Choice(["platform_api", "skip"]),
    default="platform_api",
    show_default=True,
    help="How to move Prompt Studio documents. 'skip' copies metadata only.",
)
@click.option("--skip-files", is_flag=True, help="Alias for --file-strategy=skip.")
@click.option(
    "--max-file-size",
    default="25MB",
    show_default=True,
    help="Per-file cap for the files phase. Oversize files are reported, not fatal.",
)
@click.option(
    "--concurrency",
    type=click.IntRange(min=1, max=32),
    default=DEFAULT_CONCURRENCY,
    show_default=True,
    help="Per-phase worker count. 1 is strictly sequential.",
)
@click.option(
    "--clone-group-members",
    is_flag=True,
    help="Also add group members on the target, matched by email.",
)
@pass_context
def clone(
    ctx: Context,
    source_url: str,
    source_org: str,
    source_key: str,
    target_url: str,
    target_org: str,
    target_key: str,
    **params: Any,
) -> None:
    """Copy an organization's resources into another organization.

    Adapters, connectors, workflows, pipelines, API deployments, Prompt Studio
    projects and their files, user groups and sharing state. Run --dry-run first:
    it reports what would be written without writing it.
    """
    for key in (source_key, target_key):
        remember_secret(key)
    _configure_logging(ctx)

    options = CloneOptions(
        dry_run=params["dry_run"],
        include=_split_csv(params["include"]),
        exclude=_split_csv(params["exclude"]) or (),
        on_name_conflict=params["on_name_conflict"],
        verbose=ctx.verbosity > 0,
        file_strategy="skip" if params["skip_files"] else params["file_strategy"],
        max_file_size=_parse_size(params["max_file_size"]),
        concurrency=params["concurrency"],
        clone_group_members=params["clone_group_members"],
    )

    def endpoint(url: str, org: str, key: str) -> OrgEndpoint:
        return OrgEndpoint(
            base_url=url,
            organization_id=org,
            platform_key=key,
            api_path_prefix=params["api_prefix"],
        )

    try:
        report = run_clone(
            endpoint(source_url, source_org, source_key),
            endpoint(target_url, target_org, target_key),
            options,
        )
    except CloneError as exc:
        raise CLIError(
            str(exc),
            ExitCode.USAGE,
            hint="The clone could not start. Check the URLs, orgs and keys.",
        ) from exc

    _finish(ctx, report)


def _configure_logging(ctx: Context) -> None:
    """Send the orchestrator's progress to stderr, at the run's own verbosity."""
    logging.basicConfig(
        level=logging.WARNING
        if ctx.quiet
        else (logging.DEBUG if ctx.verbosity else logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _finish(ctx: Context, report: CloneReport) -> None:
    """Emit the report, then fail if the clone did not fully succeed."""
    failure = None
    if report.aborted:
        failure = f"Clone aborted: {report.abort_reason}"
    elif failed := [phase.name for phase in report.phases if phase.failed]:
        failure = f"Clone completed with failures in: {', '.join(sorted(failed))}"

    # A person running this reads the report itself; every other format gets the
    # single envelope, which carries the same content as data.
    rendered = ctx.output is OutputFormat.TABLE
    if rendered:
        click.echo(scrub(report.render(), [*ctx.secrets(), *known_secrets()]))
    elif not failure:
        finish(ctx, report.as_dict())

    if failure:
        raise CLIError(
            failure,
            ExitCode.GENERIC,
            details=None if rendered else report.as_dict(),
            hint="The report lists what was copied and what was not. Re-running "
            "adopts what already exists on the target rather than duplicating it.",
        )


__all__ = ["clone"]
