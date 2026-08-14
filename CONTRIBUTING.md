# Contributing

## Before you start

This is a fork of [Dicklesworthstone/mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail)
and it is developed for one operator's fleet. Small fixes are welcome. For
anything larger, open an issue first — the answer may be that it does not fit,
and that is cheaper to hear before you write it.

Read `LICENSE` before contributing. It is MIT with a rider that withholds all
rights from a named set of parties and must travel unmodified with any
redistribution, so it is not an OSI-approved open-source licence.

## Working on it

```bash
uv sync --dev
make check        # ruff, ty, pytest, showcase, smoke
```

The frontend lives in `ui/`:

```bash
cd ui && pnpm install
pnpm lint && pnpm typecheck && pnpm test && pnpm coverage
```

Coverage thresholds are 100% on statements, branches, functions and lines.
That is deliberate. It has already caught a test that kept passing after it had
silently stopped exercising its own subject.

A distribution build needs exactly the pinned Node and npm; `hatch_build.py`
validates the pair and refuses anything else. The same version is pinned in
`.github/workflows/ci.yml` and in the `Dockerfile`. Those three must move
together.

## What a good change looks like

- **Say why, not what.** The diff shows what changed. A comment or commit
  message earns its place by recording the reason, the measurement, or the
  failure that made the change necessary.
- **Measure before claiming.** "This fixes it" is worth less than the command
  you ran and its output.
- **Do not widen a contract silently.** The typed API responses use
  `extra="forbid"` and the browser client rejects unknown keys, so adding one
  field means editing both sides in the same commit.
- **Schema changes go through Alembic**, not new hand-written DDL in `db.py`.

## Reporting

Open an issue. For a vulnerability, use the private route in `SECURITY.md`.
