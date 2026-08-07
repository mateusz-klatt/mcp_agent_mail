.PHONY: serve-http migrate lint lint-fix typecheck test smoke check \
	guard-install guard-uninstall claims

PY=uv run
CLI=$(PY) python -m mcp_agent_mail.cli

serve-http:
	$(CLI) serve-http

migrate:
	$(CLI) migrate

# ---------------------------------------------------------------------------
# Quality gates
#
# The four recipes under `check` are the CI gate copied verbatim from
# .github/workflows/ci.yml, in the order CI runs them, so `make check` stops at
# the same step CI would. That ordering is the point: for most of a day the
# type gate held and pytest never ran once in CI, while four machines were
# running pytest locally and reporting numbers CI could not have produced.
#
# What this CANNOT reproduce: CI runs a matrix (three operating systems, several
# Python versions) and `uv sync --dev` first. A green `make check` says "the
# gate passes here", not "the build is green" — `ty` in particular returns
# different counts on different hosts, because diagnostics like the missing
# `resource` module on Windows are properties of the machine running the
# checker, not of the repository.
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

# `uvx`, not `uv run` — this is what CI invokes, and it resolves its own pinned
# copy rather than the project's. The two disagree: the version in uv.lock has
# ASYNC240 behind --preview, a newer one has it stable, and that difference
# alone accounted for one machine reporting 78 errors against another's 22.
typecheck:
	uvx ty check

test:
	$(PY) -m pytest -q

smoke:
	$(CLI) am-run ci-slot -- echo "ok"

check: lint typecheck test smoke

guard-install:
	$(CLI) guard install $(PROJECT) $(REPO)

guard-uninstall:
	$(CLI) guard uninstall $(REPO)

claims:
	$(CLI) claims list --active-only $(ACTIVE) $(PROJECT)
