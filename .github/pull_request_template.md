## What this changes, and why

<!-- The diff shows what. This section is for why: the failure, the
     measurement, or the decision behind it. -->

## How it was verified

<!-- Commands and their results, not adjectives. If something was not run,
     say which and why — an honest gap is worth more than an implied pass. -->

```
```

## Checklist

- [ ] `make check` passes (ruff, ty, pytest)
- [ ] If `ui/` changed: `pnpm lint && pnpm typecheck && pnpm test && pnpm coverage`
- [ ] If an API response changed: the Pydantic model and the browser parser were edited in this commit, and the contract tests were updated deliberately rather than to make them pass
- [ ] If the schema changed: it is an Alembic revision, not new DDL in `db.py`
- [ ] If the Node pin moved: `hatch_build.py`, `.github/workflows/ci.yml` and `Dockerfile` moved together
- [ ] No credential appears in the diff, a test fixture, or a commit message
