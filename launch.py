#!/usr/bin/env python3
"""Cross-platform launcher. Same commands on Linux, macOS and Windows.

    python launch.py                 serve on http://127.0.0.1:8000
    python launch.py serve --port 9000 --host 0.0.0.0
    python launch.py check           deployment preflight (20 checks)
    python launch.py check --offline skip live source checks
    python launch.py cycle           run one collection cycle and exit
    python launch.py tiles           warm the offline map cache for Nepal
    python launch.py setup           create the venv and install deps, nothing else

Bootstraps its own virtual environment on first run, then re-executes itself
inside it. Nothing outside the stdlib is imported before that happens, so the
script works from a bare Python with no dependencies installed.
"""
import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
BACKEND = ROOT / "backend"
REQUIREMENTS = BACKEND / "requirements.txt"

# The one real difference between platforms: where the venv puts its binaries.
BIN = "Scripts" if os.name == "nt" else "bin"
EXE = ".exe" if os.name == "nt" else ""
VENV_PY = VENV / BIN / f"python{EXE}"


def in_venv() -> bool:
    """True when we are already running inside this project's venv."""
    try:
        return Path(sys.executable).resolve() == VENV_PY.resolve()
    except OSError:
        return False


def ensure_venv() -> None:
    """Create the venv and install requirements if either is missing."""
    if not VENV_PY.exists():
        print(f"Creating virtual environment at {VENV.name} ...")
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV)
        install()
        return

    # Venv exists but may predate a requirements change; a cheap import probe
    # is faster and quieter than running pip install on every launch.
    probe = subprocess.run(
        [str(VENV_PY), "-c", "import fastapi, uvicorn, httpx, openpyxl, apscheduler"],
        capture_output=True,
    )
    if probe.returncode != 0:
        install()


def install() -> None:
    print("Installing dependencies ...")
    subprocess.run([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip", "-q"], check=True)
    subprocess.run([str(VENV_PY), "-m", "pip", "install", "-q", "-r", str(REQUIREMENTS)], check=True)
    print("Dependencies ready.")


def reexec(argv: list[str]) -> int:
    """Re-run this script inside the venv, passing the original arguments on."""
    return subprocess.call([str(VENV_PY), str(Path(__file__).resolve()), *argv])


def run_in_backend(args: list[str]) -> int:
    """Run a module from the backend directory, which is the import root."""
    return subprocess.call([sys.executable, *args], cwd=BACKEND)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_serve(args) -> int:
    print(f"Nepal Flood Watch -> http://{args.host}:{args.port}")
    print("First cycle starts immediately; the map fills within ~30s.  Ctrl-C to stop.")
    cmd = ["-m", "uvicorn", "app.main:app", "--host", args.host, "--port", str(args.port)]
    if args.reload:
        cmd.append("--reload")
    return run_in_backend(cmd)


def cmd_check(args) -> int:
    cmd = ["-m", "app.preflight"]
    if args.offline:
        cmd.append("--offline")
    return run_in_backend(cmd)


def cmd_cycle(args) -> int:
    return run_in_backend([
        "-c",
        "import asyncio; from app import logs, pipeline; logs.setup();"
        " r = asyncio.run(pipeline.run_cycle());"
        " print('\\nstations:', r['stations'], '| impoundment alerts:',"
        " r.get('impoundment_alerts')); print('sources:');"
        " [print('  %-12s' % k, v) for k, v in r['sources'].items()]",
    ])


def cmd_tiles(args) -> int:
    print(f"Warming the {args.style} tile cache for Nepal (z{args.min_zoom}-{args.max_zoom}).")
    print("One time only; the console then renders offline.")
    return run_in_backend([
        "-c",
        f"import asyncio; from app import tiles;"
        f" print(asyncio.run(tiles.prefetch('{args.style}', {args.min_zoom}, {args.max_zoom})))",
    ])


def cmd_setup(args) -> int:
    print(f"Environment ready: {VENV_PY}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="launch.py",
        description="Nepal Flood Watch launcher (Linux, macOS, Windows)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1].split("Bootstraps")[0].rstrip(),
    )
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="run the web console (default)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    s.set_defaults(func=cmd_serve)

    c = sub.add_parser("check", help="run the deployment preflight")
    c.add_argument("--offline", action="store_true", help="skip live source checks")
    c.set_defaults(func=cmd_check)

    y = sub.add_parser("cycle", help="run one collection cycle and exit")
    y.set_defaults(func=cmd_cycle)

    t = sub.add_parser("tiles", help="warm the offline map cache")
    t.add_argument("--style", default="dark", choices=["dark", "light", "osm"])
    t.add_argument("--min-zoom", type=int, default=5)
    t.add_argument("--max-zoom", type=int, default=12)
    t.set_defaults(func=cmd_tiles)

    u = sub.add_parser("setup", help="create the venv and install dependencies only")
    u.set_defaults(func=cmd_setup)

    return p


def main() -> int:
    if sys.version_info < (3, 11):
        sys.exit(f"Python 3.11+ required, found {sys.version.split()[0]}")

    parser = build_parser()
    # Bare `python launch.py` means serve; argparse subcommands do not default.
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["serve", *argv]
    args = parser.parse_args(argv)

    if not in_venv():
        ensure_venv()
        return reexec(argv)          # hand off to the venv interpreter

    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
