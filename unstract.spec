# -*- mode: python ; coding: utf-8 -*-
"""One-file build of the `unstract` CLI.

Generated once with

    pyinstaller --onefile --console --name unstract src/unstract_cli/__main__.py

and hand-edited since. This file is the build, not that command line: the
options below are decisions, and a regenerated spec would drop them silently.
"""

from PyInstaller.utils.hooks import collect_data_files

# Three packaged files are read through `importlib.resources` -- `overlay.toml`
# and the two vendored specs -- and all three are read at *import* time, by the
# `@spec_options` decorators the command modules apply at module scope. They are
# data, not modules, so nothing puts them in the PYZ. This also picks up
# `specs/provenance.json`, which records which spec revision the flags were
# derived from and belongs with them.
datas = collect_data_files("unstract_cli")

hiddenimports = [
    # `unstract.clone.report.CloneReport.render` imports these inside the
    # function, behind `except ImportError: return self._render_plain()`. The
    # module graph does follow function-level imports, but a miss here degrades
    # `unstract clone`'s table to plain text without failing anything, so the
    # dependency is stated rather than inferred.
    "rich.console",
    "rich.table",
]

# A local build runs in a `.[dev]` venv, so the test and lint tooling is on the
# path even though nothing reaches it from the entry point. CI installs only the
# runtime dependencies, where these are no-ops -- they keep the two builds the
# same size rather than being load-bearing. `unittest` is deliberately absent:
# the size it saves is small, and libraries reach for `unittest.mock` in
# surprising places.
excludes = [
    "pytest",
    "_pytest",
    "pluggy",
    "iniconfig",
    "ruff",
    "setuptools",
    "pkg_resources",
    "tkinter",
]

a = Analysis(
    ["src/unstract_cli/__main__.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    # Not 1 or 2, and never build with PYTHONOPTIMIZE set. Every derived flag's
    # help text comes from `inspect.getdoc()` on the published clients' methods
    # -- the specs carry no parameter descriptions -- so stripping docstrings
    # empties `--help` across the whole generated surface without failing a
    # single check.
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    # One file: the binaries and the data are folded into the executable rather
    # than collected beside it, so there is no COLLECT and nothing to unpack.
    a.binaries,
    a.datas,
    [],
    name="unstract",
    debug=False,
    bootloader_ignore_signals=False,
    # Stripping invalidates the ad-hoc signature an arm64 macOS binary needs in
    # order to run at all, and occasionally produces unloadable shared objects
    # on Linux. It saves a couple of megabytes out of twenty.
    strip=False,
    # UPX is unusable on macOS arm64, and on Linux it buys size back by adding
    # decompression to every start and by looking like packed malware to EDR.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    # The building interpreter's architecture. The runners are native, and no
    # universal binary is shipped.
    target_arch=None,
    # Unsigned by design. PyInstaller still applies the ad-hoc signature Apple
    # Silicon requires to execute a Mach-O at all; what is absent is a Developer
    # ID signature and notarisation, which is why a browser download needs its
    # quarantine attribute cleared.
    codesign_identity=None,
    entitlements_file=None,
)
