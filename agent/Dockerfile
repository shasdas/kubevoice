# Reference Dockerfile for the KubeVoice agent worker.
# Adjust paths to match your agent project layout (this assumes the
# agent-starter-python style: src/agent.py, uv for dependency management).

FROM python:3.12-slim AS base

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Give uv's managed Python installs a predictable, chown-able home
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN mkdir -p /opt/uv/python

# Install dependencies first (better layer caching)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ ./src/

# Pre-download model files (Silero VAD, turn detector) at build time so
# pods start fast and don't need to fetch models at runtime.
RUN uv run python src/agent.py download-files

# adding observability metrics support
ENV PROMETHEUS_MULTIPROC_DIR=/tmp/kubevoice_metrics
EXPOSE 9091

# Run as non-root
RUN mkdir -p /tmp/kubevoice_metrics \
    && useradd --create-home agent \
    && chown -R agent:agent /app /opt/uv/python /tmp/kubevoice_metrics
USER agent

ENTRYPOINT ["uv", "run", "python", "src/agent.py"]
CMD ["start"]
