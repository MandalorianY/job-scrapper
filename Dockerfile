# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON=3.14.10 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=manual
WORKDIR /build

# uv installs the dependencies, so the runtime does not need the bundled pip.
RUN uv python install --no-bin "$UV_PYTHON" \
    && rm -rf /opt/python/cpython-*/bin/pip* \
        /opt/python/cpython-*/lib/python*/ensurepip \
        /opt/python/cpython-*/lib/python*/site-packages/pip* \
    && mkdir -p /work/data /work/exports
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --no-install-project --no-build
COPY README.md LICENSE ./
COPY src/scrapper ./src/scrapper
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --no-editable

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:cb87acc574abe719b6e5c547384defa2f7b1f24af9605d3f618cf852495d8be2
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SCRAPPER_PATHS__DATABASE_PATH=/work/data/job_search.duckdb \
    SCRAPPER_PATHS__LOG_FILE=/work/data/job-scrapper.jsonl \
    SCRAPPER_PATHS__EXPORT_DIR=/work/exports
COPY --from=build /opt/python /opt/python
COPY --from=build /opt/venv /opt/venv
COPY --from=build --chown=65532:65532 /work /work
WORKDIR /work
USER 65532:65532
# Validate the packaged configuration at build time.
RUN --network=none ["/opt/venv/bin/job-scrapper", "--status"]
ENTRYPOINT ["/opt/venv/bin/job-scrapper"]
