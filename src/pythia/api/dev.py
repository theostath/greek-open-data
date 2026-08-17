"""One-command development server: preflight, serve, open a browser.

``uvicorn pythia.api.app:app`` works fine, but it starts silently, spends 30-60 seconds
loading a 2.2 GB embedding model before it accepts anything, and reports a missing catalogue
or a stopped Ollama only when a question eventually fails. This wraps it so the three things
that actually go wrong are named **before** the wait rather than after it.

Nothing here blocks startup. A missing index is worth serving anyway — ``/healthz`` and the
landing page both report it, and that is a more useful state than a refusal to boot.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# `config.py` lives at the repo ROOT, not in `src/` (CLAUDE.md §4). A console script gets
# neither the cwd nor the root on sys.path — unlike `python -m`, which is why every other
# entrypoint here works without this — so put the root on the path before importing it.
# This is consequently a repo-local dev command: it needs the checkout, not just the wheel.
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from config import Settings, get_settings
except ModuleNotFoundError as exc:  # pragma: no cover - a broken checkout, not a code path
    raise SystemExit(
        f"cannot import config.py from {_ROOT}. Run this from the repository checkout."
    ) from exc


def preflight(settings: Settings) -> bool:
    """Print what is and is not ready. Returns whether questions can be answered at all."""
    from pythia.api.app import health

    facts = health(settings)
    datasets, dense, lexical = facts["datasets"], facts["dense_index"], facts["lexical_index"]
    llm = facts["llm_reachable"]

    def line(ok: bool, label: str, detail: str) -> None:
        # flush: Python block-buffers stdout when it is a pipe rather than a console, which
        # silently swallows the whole preflight when this is launched from a script or IDE.
        print(f"  {'OK  ' if ok else 'MISS'}  {label:<16} {detail}", flush=True)

    # ASCII only in this chrome. A Windows console defaults to cp1252, and an em dash there
    # renders as mojibake — which, in a project whose oldest recurring bug class is encoding,
    # is a bad first impression from the startup banner of all places.
    print("\nPythia - preflight\n", flush=True)
    line(bool(datasets), "catalogue", f"{datasets:,} datasets"
         if datasets else "empty - run `uv run python -m pythia.ingest.harvest`")
    line(bool(dense and lexical), "search index",
         f"{dense:,} dense / {lexical:,} lexical" if dense and lexical
         else "incomplete - run `uv run python -m pythia.retrieval.index`")
    line(bool(llm), "language model",
         f"{facts['llm_model']} at {settings.llm_base_url}" if llm
         else f"unreachable at {settings.llm_base_url} - is `ollama serve` running?")

    ready = bool(datasets and dense and lexical and llm)
    if not ready:
        print("\n  Starting anyway. The landing page and /healthz will report the gap;", flush=True)
        print("  questions will refuse until it is fixed.", flush=True)
    return ready


def _open_when_ready(host: str, port: int, timeout_s: float = 180.0) -> None:
    """Open a browser once the port accepts connections, not before.

    The model load happens inside the app's lifespan, so the port is not bound for the first
    half-minute. Opening on a timer would just show the browser an error page.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(1.0)
            if probe.connect_ex((host, port)) == 0:
                webbrowser.open(f"http://{host}:{port}/")
                return
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    """Start the development server (``uv run pythia-dev``)."""
    import argparse

    import uvicorn

    from pythia.logging_setup import configure_logging

    parser = argparse.ArgumentParser(description="Run the Pythia web app.")
    parser.add_argument("--host", default=None, help="default: api_host from settings")
    parser.add_argument("--port", type=int, default=None, help="default: api_port")
    parser.add_argument("--no-reload", action="store_true", help="disable autoreload")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--skip-preflight", action="store_true")
    args = parser.parse_args(argv)

    configure_logging()
    settings = get_settings()
    host = args.host or settings.api_host
    port = args.port or settings.api_port

    if not args.skip_preflight:
        preflight(settings)

    print(f"\n  Serving on http://{host}:{port}/", flush=True)
    print("  First start loads a ~2.2 GB embedding model - allow 30-60s.", flush=True)
    print("  Ctrl-C to stop.\n", flush=True)

    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(host, port), daemon=True).start()

    uvicorn.run(
        "pythia.api.app:app",
        host=host,
        port=port,
        reload=not args.no_reload,
        log_config=None,  # keep this project's structured JSON logging
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
