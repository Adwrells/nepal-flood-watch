#!/usr/bin/env python3
"""Cross-platform launcher. Same commands on Linux, macOS and Windows.

    python launch.py                 set up, serve, and open a browser
    python launch.py serve --port 9000 --host 0.0.0.0
    python launch.py serve --no-browser
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
import socket
import subprocess
import sys
import threading
import time
import venv
import webbrowser
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


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host if host != "0.0.0.0" else "", port))
            return True
        except OSError:
            return False


def pick_port(host: str, preferred: int, tries: int = 20) -> int:
    """Return the preferred port, or the next free one above it.

    Failing with "address already in use" is a poor greeting for someone whose
    only crime is having left an old copy running, so we move up instead. The
    chosen port is printed, because a silently different port is worse than an
    error.
    """
    for offset in range(tries):
        candidate = preferred + offset
        if port_is_free(host, candidate):
            if offset:
                print(f"Port {preferred} is in use; using {candidate} instead.", flush=True)
            return candidate
    raise SystemExit(f"No free port between {preferred} and {preferred + tries - 1}.")


def open_when_ready(url: str, host: str, port: int, timeout: float = 90.0) -> None:
    """Open a browser once the server actually answers.

    Opening immediately shows a connection error, because the first cycle and
    the scheduler start before uvicorn binds. Polling the port and waiting for
    the API to respond means the tab opens on a working console.
    """
    def wait():
        deadline = time.time() + timeout
        probe = host if host not in ("0.0.0.0", "") else "127.0.0.1"
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                if sock.connect_ex((probe, port)) == 0:
                    time.sleep(1.0)          # let the app finish starting up
                    webbrowser.open(url)
                    return
            time.sleep(0.5)

    threading.Thread(target=wait, daemon=True).start()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_serve(args) -> int:
    port = args.port if args.strict_port else pick_port(args.host, args.port)
    shown = args.host if args.host not in ("0.0.0.0", "") else "127.0.0.1"
    url = f"http://{shown}:{port}"

    # flush=True matters: uvicorn logs to stderr, which is unbuffered, so a
    # buffered stdout banner arrives after the server output when piped -- and
    # the URL is the one line the user actually needs to see first.
    print(f"Nepal Flood Watch -> {url}", flush=True)
    print("First cycle starts immediately; the map fills within ~30s.  Ctrl-C to stop.",
          flush=True)

    # The backend serves the frontend itself (StaticFiles mounted at /), so
    # there is no second process to start and nothing to build.
    if not args.no_browser:
        open_when_ready(url, args.host, port)

    cmd = ["-m", "uvicorn", "app.main:app", "--host", args.host, "--port", str(port)]
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
    s.add_argument("--no-browser", action="store_true", help="do not open a browser")
    s.add_argument("--strict-port", action="store_true",
                   help="fail if the port is taken instead of moving to the next free one")
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
