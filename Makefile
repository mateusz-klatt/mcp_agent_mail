.PHONY: serve-http migrate lint lint-fix typecheck test smoke showcase check check-all \
	guard-install guard-uninstall claims

PY=uv run
CLI=$(PY) python -m mcp_agent_mail.cli
TY_VERSION=0.0.71

serve-http:
	$(CLI) serve-http

migrate:
	$(CLI) migrate

# ---------------------------------------------------------------------------
# Quality gates
#
# The recipes under `check` are the local backend subset of CI, in CI order.
# They deliberately do not claim to reproduce the frontend, browser, benchmark,
# or cross-OS jobs in .github/workflows/ci.yml.
#
# CI also performs `uv sync --dev --frozen`, installs its browser runtimes, and
# runs independent React, WebKit, benchmark, macOS, and Windows jobs. A green
# `make check-all` therefore means only "the local backend subset passes here",
# never "the remote build is green".
# ---------------------------------------------------------------------------

# Checks, never mutates. This recipe used to be `ruff check --fix
# --unsafe-fixes`, which is a different verb hiding behind the same name: it
# rewrote the tree and exited 0 whether or not the code was clean, so "make
# lint passes" meant nothing. Auto-fixing is `lint-fix`, below.
lint:
	$(PY) ruff check

# Read the diff. `--fix` is not automatically an improvement: enabling RUF100
# and running it once today took this repository from 22 lint errors to 91,
# while reporting "19 fixed, 0 remaining".
lint-fix:
	$(PY) ruff check --fix

# `uvx`, not `uv run` — this is what CI invokes. Keep TY_VERSION synchronized
# with ci.yml so local and hosted checks use the same isolated tool build.
typecheck:
	uvx --from "ty==$(TY_VERSION)" ty check --python-version 3.14 --python-platform all

test:
	$(PY) -m pytest -q

smoke:
	$(CLI) am-run ci-slot -- echo "ok"

showcase:
	$(PY) python scripts/integration_showcase.py

check: lint typecheck test showcase smoke

# Explicit spelling for the local backend subset; the authoritative complete
# gate is the multi-job GitHub Actions workflow.
check-all: check

guard-install:
	$(CLI) guard install $(PROJECT) $(REPO)

guard-uninstall:
	$(CLI) guard uninstall $(REPO)

claims:
	$(CLI) claims list --active-only $(ACTIVE) $(PROJECT)
