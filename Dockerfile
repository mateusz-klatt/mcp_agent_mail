# syntax=docker/dockerfile:1.7

# --------------------------------------------------------------------------
# Stage 1: build the toon_rust encoder (`tru`).
#
# The Python runtime can shell out to a `tru` binary to encode payloads in
# TOON format (`format='toon'` on any tool call). Without `tru` on $PATH the
# code path silently falls back to JSON. The image used to ship without a
# TOON encoder at all, so every `format='toon'` request from a container
# deployment was silently downgraded — see issue #163.
#
# We build the encoder from source pinned to a specific commit by default
# so the container's TOON output matches a known toon_rust commit, then copy
# the single binary into the runtime stage. The crate name on cargo install
# is `tru` but the [[bin]] target name is `toon`, so we rename on copy.
# (Renaming the target upstream is tracked separately; this Dockerfile is
# tolerant of either name today.)
# --------------------------------------------------------------------------
#
# toon_rust pins nightly via rust-toolchain.toml. Install rustup into a
# stable Debian base, let the toolchain file drive channel selection — that
# way the upstream repository controls its own compiler requirements without
# making the source revision itself mutable.
FROM debian:bookworm-slim AS tru-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential git ca-certificates pkg-config && \
    rm -rf /var/lib/apt/lists/*

# Install rustup with a minimal profile; the project's rust-toolchain.toml
# will pull the right channel + components on first `cargo` invocation.
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain none --profile minimal --no-modify-path

ARG TOON_RUST_REPO=https://github.com/Dicklesworthstone/toon_rust.git
ARG TOON_RUST_REF=cb256dcf73ab78c248a14f65840a3fa722ec8682

# Resolve ${TOON_RUST_REF} as a branch name, tag, or full 40-char commit
# SHA. We can't use `git clone --depth 1 --branch <ref>` because `--branch`
# refuses bare commit SHAs ("Remote branch <sha> not found in upstream
# origin"), which would prevent pinning the encoder to a specific upstream
# commit via `--build-arg TOON_RUST_REF=<sha>`. Instead: init an empty
# repo, fetch *just* the requested ref with depth 1, then check it out.
#
# Caveat: GitHub's smart-http upload-pack
# (uploadpack.allowReachableSHA1InWant) only resolves *full* 40-char SHAs
# in the want list — abbreviated SHAs error out with "couldn't find remote
# ref". Pass the full SHA, a branch, or a tag.
RUN git init -q /build/toon_rust && \
    cd /build/toon_rust && \
    git remote add origin "${TOON_RUST_REPO}" && \
    git fetch --depth 1 origin "${TOON_RUST_REF}" && \
    git checkout -q FETCH_HEAD && \
    cargo build --release && \
    # The [[bin]] target is currently named "toon" but mcp_agent_mail expects
    # the binary on $PATH as `tru`. Copy under the expected name. Fall back
    # to whichever target file exists so this stage stays valid if/when the
    # upstream [[bin]] target is renamed to `tru`.
    install -m 0755 \
        "$(test -f target/release/toon && echo target/release/toon || echo target/release/tru)" \
        /tru && \
    strip /tru

# --------------------------------------------------------------------------
# Stage 2: build the exact validated browser artifact used by Python packages.
#
# Node and npm are intentionally confined to this builder. The custom Hatch
# hook compiles in an isolated temporary directory, writes a per-file hash
# manifest, validates every entry-point reference, and embeds that same tree in
# a wheel. The extraction below copies only the validated ui_dist members.
# --------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.11.2 AS uv-bin
# Must match the toolchain hatch_build.py validates (_NODE_VERSION /
# _NPM_VERSION) and the node-version in ci.yml: the wheel is built here, so a
# divergence fails the image build rather than any test.
#
# trixie, not bookworm: only the bare node binary is copied into the
# python:3.14-slim builder (also trixie), and the bookworm build needs
# libatomic.so.1 / libstdc++.so.6 from its own base — absent there, so
# `node --version` exits 127 and the UI build fails with a bare (127).
FROM node:26.7.0-trixie-slim AS node-runtime
FROM python:3.14-slim AS ui-builder

COPY --from=uv-bin /uv /uvx /usr/local/bin/
# Node 26 links against libatomic (Node 22 did not) and only the bare binary
# is copied in, so the builder has to provide that library itself. Build
# stage only — it never reaches the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && \
    rm -rf /var/lib/apt/lists/*
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm

ENV SOURCE_DATE_EPOCH=0
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE hatch_build.py ./
COPY ui ./ui
COPY src/mcp_agent_mail/__init__.py ./src/mcp_agent_mail/__init__.py
COPY src/mcp_agent_mail/templates ./src/mcp_agent_mail/templates
COPY src/mcp_agent_mail/viewer_assets/index.html ./src/mcp_agent_mail/viewer_assets/index.html

RUN uv build --wheel --no-progress --no-create-gitignore --out-dir /artifacts && \
    python - <<'PY'
from pathlib import Path, PurePosixPath
from shutil import copyfileobj
from zipfile import ZipFile

wheels = list(Path("/artifacts").glob("*.whl"))
if len(wheels) != 1:
    raise RuntimeError(f"Expected one Iris wheel, found {len(wheels)}")
prefix = PurePosixPath("mcp_agent_mail/ui_dist")
output = Path("/ui/dist")
with ZipFile(wheels[0]) as archive:
    members = [
        info
        for info in archive.infolist()
        if PurePosixPath(info.filename).is_relative_to(prefix) and not info.is_dir()
    ]
    if not members:
        raise RuntimeError("The Iris wheel does not contain ui_dist")
    for info in members:
        relative = PurePosixPath(info.filename).relative_to(prefix)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"Unsafe ui_dist wheel member: {info.filename}")
        target = output.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, target.open("xb") as destination:
            copyfileobj(source, destination)
if not (output / "index.html").is_file() or not (output / ".hermes-ui-build.json").is_file():
    raise RuntimeError("Validated Iris index or build manifest is missing")
PY

# --------------------------------------------------------------------------
# Stage 3: Python application runtime.
# --------------------------------------------------------------------------
FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    PYTHONPATH=/app/src

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install uv to a shared path so it remains available after USER switch
COPY --from=uv-bin /uv /uvx /usr/local/bin/

# Install the TOON encoder built in stage 1 so `format='toon'` requests are
# served by the real toon_rust encoder rather than silently falling back to
# JSON. /usr/local/bin is on $PATH for all users including the unprivileged
# appuser below.
COPY --from=tru-builder /tru /usr/local/bin/tru

WORKDIR /app

# Copy locked project metadata and sync deps first for better caching.
# README.md is required by hatchling since pyproject.toml references it.
COPY pyproject.toml uv.lock README.md LICENSE hatch_build.py ./
# Install runtime deps only — the project itself (hatchling wheel from
# src/mcp_agent_mail) can't be built yet because src/ isn't present, so defer
# its install with --no-install-project to keep this dependency layer cached.
RUN uv sync --frozen --no-dev --no-install-project

# Copy source, then install the project itself now that src/ exists.
COPY src ./src
RUN uv sync --frozen --no-dev

# Copy the frontend only after the Python project has been installed, so no
# source-copy or project-install step can replace the production assets.
RUN test ! -e ./src/mcp_agent_mail/ui_dist
COPY --from=ui-builder /ui/dist ./src/mcp_agent_mail/ui_dist

# Defaults suitable for container.
#
# DATABASE_URL is set here for the same reason as STORAGE_ROOT: the application
# default is `sqlite+aiosqlite:///./storage.sqlite3`, which resolves against the
# working directory (/app) and therefore lands in the container's writable
# layer rather than on the mounted volume. `docker run -v iris-data:/data` would
# then appear to work and lose the whole mailbox on `docker rm`. Pointing it at
# /data/mailbox makes the documented one-command run durable by default; both
# variables remain overridable, and docker-compose.yml sets its own.
ENV HTTP_HOST=0.0.0.0 \
    STORAGE_ROOT=/data/mailbox \
    DATABASE_URL=sqlite+aiosqlite:////data/mailbox/iris.sqlite3

# The commit this image was built from, surfaced by health_check so a deploy can
# be verified from outside the host. Left empty unless the build passes it
# (`--build-arg MCP_AGENT_MAIL_BUILD_COMMIT=$(git rev-parse HEAD)`), because an
# absent commit must read as "this build did not record one" rather than as a
# confident wrong answer -- reading the version off a running server is exactly
# what went wrong when the only public number available was FastMCP's.
ARG MCP_AGENT_MAIL_BUILD_COMMIT=""
ENV MCP_AGENT_MAIL_BUILD_COMMIT=${MCP_AGENT_MAIL_BUILD_COMMIT}

# Registries display this field, and the default assumption for a repository
# like this one would be plain MIT -- which is wrong. LICENSE is "MIT License
# (with OpenAI/Anthropic Rider)"; the rider withholds all rights from named
# parties and must travel unmodified with every distribution, which is why the
# file itself is copied into /app above. There is no SPDX identifier for it, so
# the label points at the file rather than asserting a licence that does not
# apply.
LABEL org.opencontainers.image.licenses="SEE LICENSE IN /app/LICENSE" \
      org.opencontainers.image.title="Iris" \
      org.opencontainers.image.description="Private coordination for concurrent coding agents, with a human in the loop."

EXPOSE 8765
VOLUME ["/data"]

# Create non-root user and set ownership on data dir
RUN adduser --disabled-password --gecos "" --uid 10001 appuser && \
    mkdir -p /data/mailbox && chown -R appuser:appuser /data /app
USER appuser

# Mark the mounted mailbox directory as a git safe.directory so git does not
# refuse to operate when the host volume is owned by a different uid than
# appuser (uid 10001) — a common Docker-on-Linux scenario. Without this, git
# treats /data/mailbox (and every per-project repo created underneath it) as
# "dubious ownership" and falls back to a compat mode that fails with
# "Unknown parameter: --cached" on diff/status operations.
#
# git safe.directory entries must be absolute paths (no glob patterns other
# than the special catch-all '*'). Since per-project repos live at
# /data/mailbox/<slug>, we need the catch-all to cover the container's
# dynamically-created subdirectories. This is safe here because the user has
# explicitly mounted the volume into this dedicated container.
# See: https://github.com/Dicklesworthstone/mcp_agent_mail/issues/143
RUN git config --global --add safe.directory /data/mailbox && \
    git config --global --add safe.directory '*'

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8765/health/liveness || exit 1

# Run the HTTP server via the prebuilt venv (avoids uv overhead at startup)
CMD ["/app/.venv/bin/python", "-m", "mcp_agent_mail.cli", "serve-http"]
