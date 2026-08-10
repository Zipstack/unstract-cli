#!/usr/bin/env python
"""Generate the Document Studio OpenAPI spec from an ``unstract`` checkout.

Runs drf-spectacular against the backend's own venv with zero repo changes: the
one annotation the POC needs is applied at runtime from ``annotations.py``.

Usage:
    ~/zipstuff/unstract/backend/.venv/bin/python tools/gen_docstudio_spec.py \
        --urlconf api_v2.execution_urls --out specs/docstudio.json
"""

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = Path.home() / "zipstuff" / "unstract"


def bootstrap_django(backend: Path) -> None:
    from dotenv import load_dotenv

    # .env holds an inline JSON value that breaks shell / uv --env-file parsers.
    load_dotenv(backend / ".env")
    os.environ["DB_HOST"] = "localhost"
    os.environ["DB_PORT"] = "5432"
    os.environ["DB_SCHEMA"] = "public"  # the .env default `unstract` won't exist
    os.environ["DJANGO_SETTINGS_MODULE"] = "backend.settings.test"

    os.chdir(backend)
    sys.path.insert(0, str(backend))

    import django
    from django.conf import settings

    settings.INSTALLED_APPS = list(settings.INSTALLED_APPS) + ["drf_spectacular"]
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    }
    settings.SPECTACULAR_SETTINGS = {
        "TITLE": "Unstract Document Studio",
        "VERSION": "v1",
        "PREPROCESSING_HOOKS": ["drf_spectacular.hooks.preprocess_exclude_path_format"],
        # Keep generation off the network and off the DB where we can.
        "SERVE_INCLUDE_SCHEMA": False,
    }
    django.setup()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument(
        "--urlconf",
        default="deployment",
        help=(
            "'deployment' (the execute/status routes, mounted at their real "
            "prefix), or any importable urlconf e.g. backend.urls_v2."
        ),
    )
    ap.add_argument("--out", type=Path, default=REPO / "specs" / "docstudio.json")
    ap.add_argument(
        "--no-annotate",
        action="store_true",
        help="Skip the Phase 2 runtime annotation (shows the unannotated baseline).",
    )
    args = ap.parse_args()

    backend = args.source / "backend"
    if not (backend / "manage.py").exists():
        print(f"no backend at {backend}", file=sys.stderr)
        return 2

    bootstrap_django(backend)

    if not args.no_annotate:
        sys.path.insert(0, str(REPO / "tools"))
        import annotations

        annotations.annotate()

    from drf_spectacular.generators import SchemaGenerator

    urlconf = args.urlconf
    if urlconf == "deployment":
        # A sub-urlconf generates paths without the prefix it is mounted at, so
        # the spec would describe URLs the server does not serve. Mirror
        # backend/base_urls.py's mount instead.
        from django.conf import settings
        from django.urls import include, path

        urlconf = SimpleNamespace(
            urlpatterns=[
                path(
                    f"{settings.API_DEPLOYMENT_PATH_PREFIX}/",
                    include("api_v2.execution_urls"),
                )
            ]
        )

    schema = SchemaGenerator(urlconf=urlconf).get_schema(request=None, public=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys is what makes the committed artifact a usable drift signal.
    args.out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")

    ops = sum(
        1
        for p in schema["paths"].values()
        for m in p
        if m in {"get", "post", "put", "patch", "delete"}
    )
    print(
        f"{args.out}: {len(schema['paths'])} paths, {ops} operations, "
        f"{len(schema.get('components', {}).get('schemas', {}))} schemas"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
