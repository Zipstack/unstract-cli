"""`deployment execute` / `deployment status` over the PUBLISHED client.

The baseline for the Phase 5 comparison. Note what is missing: the published
`structure_file()` sends only `timeout` and `include_metadata`, so the other ten
backend params cannot be exposed here without editing the SDK first.
"""

import json

import click
from unstract.api_deployments.client import APIDeploymentsClient


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
def execute_cmd(files, url, key, timeout, include_metadata):
    client = APIDeploymentsClient(
        api_url=url, api_key=key, api_timeout=timeout, include_metadata=include_metadata
    )
    click.echo(json.dumps(client.structure_file(list(files)), indent=2))


@deployment.command("status")
@click.argument("status_endpoint")
@click.option("--url", envvar="UNSTRACT_API_URL", required=True)
@click.option("--key", envvar="UNSTRACT_API_KEY", required=True)
@click.option("--include-metadata", is_flag=True)
def status_cmd(status_endpoint, url, key, include_metadata):
    client = APIDeploymentsClient(
        api_url=url, api_key=key, include_metadata=include_metadata
    )
    click.echo(json.dumps(client.check_execution_status(status_endpoint), indent=2))


if __name__ == "__main__":
    cli()
