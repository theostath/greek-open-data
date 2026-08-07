"""FastAPI application: routes, templates and the security posture (ADR-0008).

One process serves both the HTTP routes and the server-rendered templates. Handlers are
``def``, not ``async def``, on purpose: Starlette runs sync handlers in a threadpool, and an
``async def`` handler doing blocking SQLite and CPU inference would stall the event loop for
every other request.

A loopback bind is not an access control. Any page in any other browser tab can POST a
cross-origin form here, and CORS stops the *reading* of the response, not the request — so a
zero-auth endpoint that triggers LLM inference and outbound fetches checks ``Origin`` itself.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import asynccontextmanager, contextmanager, suppress
from typing import Any

from config import Settings, get_settings
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pythia.api import browse, metrics
from pythia.api.jobs import JobRejected, JobStatus, JobStore, Miss
from pythia.api.service import Pipeline
from pythia.api.view import to_view
from pythia.logging_setup import get_logger, log_event

LOGGER_NAME = "pythia.api"

#: Datasets per browse page. Πequivalent publishers run to 625 datasets, so this is load-bearing.
BROWSE_PAGE = 30

#: A catalogue id is a CKAN UUID. Bounding the shape keeps junk out of the pipeline, where a
#: miss would otherwise surface as "the publisher failed" — which would blame the wrong party.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

TEMPLATES = Jinja2Templates(directory="templates")
# Autoescape is the single control standing between a CKAN-supplied dataset title and script
# execution. Asserted rather than assumed, because a future Jinja2Templates change is silent.
assert TEMPLATES.env.autoescape, "Jinja2 autoescape must be on"


def _template_error(message: str) -> None:
    """Fail a render rather than emit it.

    ``Answer`` refuses to exist without provenance, but a template can happily omit the
    footer. This lets a template mirror the dataclass invariant instead of trusting it.
    """
    raise ValueError(message)


TEMPLATES.env.globals["fail"] = _template_error

#: Every asset is vendored locally, so a strict policy costs nothing. 'unsafe-inline' is absent
#: from script-src deliberately: the chart spec is delivered in a JSON script tag, not as code.
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def get_settings_dep() -> Settings:
    """Settings dependency, overridable in tests."""
    return get_settings()


def get_pipeline(request: Request) -> Pipeline:
    """The process-lifetime pipeline. Overridden with a fake in route tests."""
    pipeline: Pipeline = request.app.state.pipeline
    return pipeline


def get_jobs(request: Request) -> JobStore:
    """The process-lifetime job store. Overridden with a fake in route tests."""
    jobs: JobStore = request.app.state.jobs
    return jobs


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """Build the heavy resources once, and release them on shutdown."""
    settings = get_settings()
    pipeline = Pipeline.create(settings)
    pipeline.warm()  # pay the ~2.2 GB embedding load now, not on the first question

    def run(question: str, resource_id: str | None, on_stage: Any) -> Any:
        return pipeline.answer(question, resource_id=resource_id, on_stage=on_stage)

    app.state.settings = settings
    app.state.pipeline = pipeline
    app.state.jobs = JobStore(settings, run=run)
    try:
        yield
    finally:
        app.state.jobs.close()
        pipeline.close()


def create_app(lifespan_handler: Any = lifespan) -> FastAPI:
    """Build the application. ``debug=False`` explicitly: a traceback is never a response."""
    app = FastAPI(title="Pythia", debug=False, lifespan=lifespan_handler, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory="static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        """Set the security headers on every response, including error responses."""
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(500)
    async def on_error(request: Request, exc: Exception) -> Response:
        """Render our own error page; Starlette's default leaks framework detail."""
        return TEMPLATES.TemplateResponse(
            request, "partials/_error.html",
            {"blame": "pythia", "detail": None}, status_code=500,
        )

    _register_routes(app)
    return app


def _same_origin(request: Request, settings: Settings) -> bool:
    """Reject a cross-site form POST.

    Browsers reliably send ``Origin`` on cross-site POSTs. A missing header is a same-origin
    navigation or a non-browser client (curl), which is allowed: this guards against another
    *page* driving inference here, not against the user's own terminal.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return origin in {
        f"http://{settings.api_host}:{settings.api_port}",
        f"http://localhost:{settings.api_port}",
    }


def _register_routes(app: FastAPI) -> None:
    """Attach every route. Split out so ``create_app`` stays readable."""

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, settings: Settings = Depends(get_settings_dep)) -> Response:
        """The landing page and its teaching empty state."""
        return TEMPLATES.TemplateResponse(
            request, "index.html", {"question": "", "examples": EXAMPLES},
        )

    @app.get("/explore", response_class=HTMLResponse)
    def explore(request: Request, settings: Settings = Depends(get_settings_dep)) -> Response:
        """Browse entry: who publishes readable data, and about what.

        Deterministic SQL, no retrieval — so unlike a question, this cannot be wrong.
        """
        with _catalog_conn(settings) as conn:
            publishers = browse.list_publishers(conn)
            themes = browse.list_themes(conn)
        grouped: dict[str, list[browse.Publisher]] = {}
        for publisher in publishers:
            grouped.setdefault(publisher.kind.value, []).append(publisher)
        return TEMPLATES.TemplateResponse(
            request, "explore.html",
            {"grouped": grouped, "themes": themes, "total": len(publishers)},
        )

    @app.get("/explore/datasets", response_class=HTMLResponse)
    def explore_datasets(
        request: Request,
        publisher: str = "",
        theme: str = "",
        page: int = 1,
        settings: Settings = Depends(get_settings_dep),
    ) -> Response:
        """Datasets for one publisher or theme — every one of them readable.

        Query params rather than path segments: publisher names are Greek with spaces, and
        round-tripping those through a URL path invites exactly the encoding bugs §5 warns
        about.
        """
        page = max(1, page)
        offset = (page - 1) * BROWSE_PAGE
        with _catalog_conn(settings) as conn:
            datasets = browse.list_datasets(
                conn, publisher=publisher or None, theme=theme or None,
                limit=BROWSE_PAGE, offset=offset,
            )
            total = browse.count_datasets(
                conn, publisher=publisher or None, theme=theme or None
            )
            label = publisher or next(
                (t.label for t in browse.list_themes(conn) if t.code == theme), theme
            )
        return TEMPLATES.TemplateResponse(
            request, "explore_datasets.html",
            {"datasets": datasets, "total": total, "page": page, "pages": max(
                1, -(-total // BROWSE_PAGE)), "publisher": publisher, "theme": theme,
             "label": label, "question": ""},
        )

    @app.post("/ask", response_class=HTMLResponse)
    def ask(
        request: Request,
        question: str = Form(default=""),
        resource_id: str = Form(default=""),
        jobs: JobStore = Depends(get_jobs),
        settings: Settings = Depends(get_settings_dep),
    ) -> Response:
        """Accept a question and return the fragment that polls for its result.

        ``resource_id`` is the browse handoff: it pins the dataset and **bypasses retrieval
        entirely**, which is the whole point of #18 — retrieval is the measured ceiling.
        """
        logger = get_logger(LOGGER_NAME)
        if not _same_origin(request, settings):
            return TEMPLATES.TemplateResponse(
                request, "partials/_error.html",
                {"blame": "origin", "detail": None}, status_code=403,
            )

        pinned = resource_id.strip()
        if pinned and not _ID_RE.match(pinned):
            # Rejected here rather than in the worker: a bad id there surfaces as "the
            # publisher failed", which blames the wrong party for a malformed request.
            return _fragment(request, "partials/_error.html",
                             {"blame": "bad_resource", "detail": None}, status_code=400)

        text = question.strip()
        # The client is a browser, not a JSON consumer, so a validation failure renders a
        # message rather than FastAPI's 422 body.
        if not text:
            return _fragment(request, "partials/_error.html",
                             {"blame": "empty", "detail": None}, status_code=400)
        if len(text) > settings.api_max_question_chars:
            return _fragment(request, "partials/_error.html",
                             {"blame": "too_long",
                              "detail": settings.api_max_question_chars}, status_code=400)

        try:
            job_id = jobs.submit(text, pinned or None)
        except JobRejected as exc:
            return _fragment(request, "partials/_error.html",
                             {"blame": "busy", "detail": str(exc)}, status_code=429)

        # Length, never the text: the question is user content and must not reach INFO logs.
        log_event(logger, logging.INFO, "api.ask", job=job_id, question_chars=len(text),
                  pinned=bool(pinned))
        return _fragment(request, "partials/_progress.html",
                         {"job": jobs.get(job_id), "elapsed": 0})

    @app.get("/ask/{job_id}", response_class=HTMLResponse)
    def poll(request: Request, job_id: str, jobs: JobStore = Depends(get_jobs),
             settings: Settings = Depends(get_settings_dep)) -> Response:
        """Render the current stage, or the terminal fragment once there is one."""
        job = jobs.get(job_id)
        if job is None:
            return _fragment(request, "partials/_expired.html",
                             {"reason": jobs.miss_reason(job_id), "question": ""})
        if job.status is JobStatus.FAILED:
            return _fragment(request, "partials/_error.html",
                             {"blame": "pipeline", "detail": job.error})
        if job.status is not JobStatus.DONE:
            elapsed = _elapsed(job, jobs)
            return _fragment(request, "partials/_progress.html",
                             {"job": job, "elapsed": elapsed})
        return _fragment(request, "partials/_result.html",
                         {"view": to_view(job.bundle.answer, job.bundle.recovery,
                                          settings=settings),
                          "job_id": job_id})

    @app.get("/a/{job_id}", response_class=HTMLResponse)
    def permalink(request: Request, job_id: str, jobs: JobStore = Depends(get_jobs),
                  settings: Settings = Depends(get_settings_dep)) -> Response:
        """Full-page render of a finished job.

        Its own **full page** on a miss, not the fragment: this route is reached by direct
        navigation, so returning a bare partial would yield a document with no head.
        """
        job = jobs.get(job_id)
        if job is None or job.status is not JobStatus.DONE:
            reason = jobs.miss_reason(job_id) if job is None else Miss.UNKNOWN
            return TEMPLATES.TemplateResponse(
                request, "expired.html",
                {"reason": reason, "question": job.question if job else ""}, status_code=404,
            )
        return TEMPLATES.TemplateResponse(
            request, "answer_page.html",
            {"view": to_view(job.bundle.answer, job.bundle.recovery, settings=settings),
             "question": job.question, "job_id": job_id},
        )

    @app.get("/stats", response_class=HTMLResponse)
    def stats(request: Request, settings: Settings = Depends(get_settings_dep)) -> Response:
        """What the app has actually done, aggregated from the metrics store.

        The headline is the **refusal mix**, not latency: grounded-or-silent means the
        answered / unsupported / no_match ratio is the product's health signal.
        """
        conn = metrics.connect(settings.metrics_db_path)
        try:
            metrics.init_db(conn)
            result = metrics.summary(conn)
        finally:
            conn.close()
        return TEMPLATES.TemplateResponse(request, "stats.html", {"s": result})

    @app.get("/healthz")
    def healthz(settings: Settings = Depends(get_settings_dep)) -> JSONResponse:
        """Report readiness field by field.

        Never ``settings.model_dump()`` — that would publish ``data_gov_gr_token`` and
        ``anthropic_api_key``. Counts rows rather than checking file existence, because
        ``ingest.db.connect()`` *creates* a missing database: an existence check reports green
        on a fresh checkout while every question silently returns ``no_match``.
        """
        return JSONResponse(health(settings))


@contextmanager
def _catalog_conn(settings: Settings) -> Any:
    """A read-only catalogue connection for the life of one request.

    Read-only on purpose: ``ingest.db.connect()`` *creates* a missing file, so a browse route
    using it would silently manufacture an empty catalogue instead of failing visibly. Opened
    per request because handlers run in a threadpool and ``sqlite3`` defaults to
    ``check_same_thread=True``.
    """
    conn = sqlite3.connect(f"file:{settings.catalog_db_path}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


def _elapsed(job: Any, jobs: JobStore) -> int:
    """Whole seconds since the question was submitted."""
    return max(0, int(jobs.now() - job.submitted_at))


def _fragment(
    request: Request, name: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    """Render an HTMX fragment."""
    return TEMPLATES.TemplateResponse(request, name, context, status_code=status_code)


def health(settings: Settings) -> dict[str, Any]:
    """Collect the readiness facts ``/healthz`` publishes."""
    datasets = _count(settings.catalog_db_path, "SELECT count(*) FROM datasets")
    lexical = _count(settings.catalog_db_path, "SELECT count(*) FROM datasets_fts")
    dense = _dense_count(settings.chroma_path)
    return {
        "status": "ok" if datasets and lexical and dense else "degraded",
        "datasets": datasets,
        "lexical_index": lexical,
        "dense_index": dense,
        "llm_reachable": _llm_reachable(settings),
        "llm_model": settings.llm_model,
    }


def _count(db_path: str, sql: str) -> int:
    """Run a scalar count, returning 0 when the table or file is not usable."""
    with suppress(sqlite3.Error, OSError):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(sql).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    return 0


def _dense_count(chroma_path: str) -> int:
    """Count vectors in the Chroma collection, or 0 if it cannot be opened."""
    with suppress(Exception):
        from pythia.retrieval.embed import _collection

        return int(_collection(chroma_path, "datasets").count())
    return 0


def _llm_reachable(settings: Settings) -> bool:
    """One short probe of the Ollama endpoint, so the empty state can warn before typing."""
    with suppress(Exception):
        import httpx

        base = settings.llm_base_url.rstrip("/").removesuffix("/v1")
        return bool(httpx.get(f"{base}/api/tags", timeout=2.0).status_code == 200)
    return False


#: Empty-state examples. Only questions the pipeline currently ANSWERS belong here: suggesting
#: one that refuses is a worse first run than an empty box.
#:
#: **Each was verified live on 2026-08-06 and returned ANSWERED.** Re-verify after any change
#: to retrieval, since R@1 is what decides these (only 12/26 golden questions put the right
#: dataset first). Two rejected in that run and are kept here as a warning, not as candidates:
#: "Πόσοι εμβολιασμοί ανά περιφέρεια;" → no_match, and
#: "Πόσες ημέρες με ακραίο κίνδυνο δασικής πυρκαγιάς;" → unsupported.
#:
#: The vaccination example is deliberately the dataset's own title: it demonstrates the advice
#: the refusal screen gives — prefer the words a public body would use.
EXAMPLES: tuple[str, ...] = (
    "Πόσα αιτήματα ασύλου ανά υπηκοότητα;",
    "Στατιστικά εμβολιασμού για τον COVID-19",
    "How many road traffic accidents were recorded?",
)


app = create_app()
