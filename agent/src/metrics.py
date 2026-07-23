"""
Prometheus metrics for KubeVoice — multiprocess-aware.

LiveKit's worker runs job code (where our tools execute) in a separate
spawned OS process from the top-level worker process. A plain
prometheus_client Counter/Histogram only lives in the memory of whichever
process incremented it — so the top-level process's /metrics endpoint
would always read zero, even while tools were genuinely being called in
the job process.

prometheus_client's multiprocess mode solves this the same way it's used
for multi-worker Gunicorn apps: each process writes its metric deltas to
files in PROMETHEUS_MULTIPROC_DIR, and the /metrics endpoint aggregates
across all of them on each scrape.

IMPORTANT: PROMETHEUS_MULTIPROC_DIR must be set as an environment variable
BEFORE prometheus_client is imported anywhere in the app (it's read at
import time). Set it in .env / Dockerfile / shell, not in Python code.
"""
import logging
import os
import shutil
from wsgiref.simple_server import make_server

logger = logging.getLogger("kubevoice.metrics")

METRICS_PORT = 9091
MULTIPROC_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "/tmp/kubevoice_metrics")

# NOTE: deliberately NOT calling os.makedirs() here at module level.
# This file gets imported as a side effect of importing agent.py — including
# during `docker build`'s `download-files` step, which runs as root before
# the image switches to a non-root user. Creating the directory here would
# bake it into the image owned by root, making it unwritable by the
# non-root runtime user (the same class of bug as the uv/.venv permissions
# issue). Directory creation is deferred to start_metrics_server(), which
# only ever runs at real container/process runtime, as whichever user is
# actually running the worker.

from prometheus_client import Counter, Histogram, CollectorRegistry, multiprocess, make_wsgi_app  # noqa: E402

TOOL_CALLS = Counter(
    "kubevoice_tool_calls_total",
    "Number of times a tool was invoked",
    ["tool_name", "outcome"],  # outcome: success | error
)

TOOL_LATENCY = Histogram(
    "kubevoice_tool_latency_seconds",
    "Time spent executing a tool call, including the Kubernetes API round trip",
    ["tool_name"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

K8S_API_ERRORS = Counter(
    "kubevoice_k8s_api_errors_total",
    "Number of Kubernetes API calls that raised an exception",
    ["tool_name"],
)


def _wipe_multiproc_dir():
    """Call ONCE, only from the top-level worker process, before any job
    processes are spawned — never from a job/child process. Creates the
    directory fresh (as whichever user is actually running the process)
    if it doesn't already exist, and clears stale files if it does."""
    os.makedirs(MULTIPROC_DIR, exist_ok=True)
    for f in os.listdir(MULTIPROC_DIR):
        path = os.path.join(MULTIPROC_DIR, f)
        try:
            os.remove(path) if os.path.isfile(path) else shutil.rmtree(path)
        except OSError:
            pass


def start_metrics_server() -> None:
    """Call once, in the top-level worker process, before serving jobs."""
    _wipe_multiproc_dir()

    def app(environ, start_response):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        wsgi_app = make_wsgi_app(registry)
        return wsgi_app(environ, start_response)

    httpd = make_server("", METRICS_PORT, app)
    import threading
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    logger.info(
        "Prometheus metrics server listening on :%d/metrics (multiprocess dir: %s)",
        METRICS_PORT, MULTIPROC_DIR,
    )