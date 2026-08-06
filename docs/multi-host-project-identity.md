# Project identity across multiple hosts

This is the one configuration mistake that silently disables everything else.

## The failure

A project's identity is a slug of the `project_key` string an agent passes to
`ensure_project` / `register_agent` / `send_message` / `file_reservation_paths`.
With the default `PROJECT_IDENTITY_MODE=dir`, the slug is literally
`slugify(human_key)` (`src/mcp_agent_mail/app.py:2007`), and the identity-mode
branches below it are unreachable unless `WORKTREES_ENABLED` is set
(`app.py:2005-2008`).

So if agents pass their local checkout path — the obvious thing to do — then:

| Host | `project_key` passed | Resulting project |
|---|---|---|
| Linux | `/home/me/projects/app` | `home-me-projects-app` |
| macOS | `/Users/me/dev/app` | `users-me-dev-app` |
| Windows | `C:\src\app` | `c-src-app` |

Three separate projects. Each agent has its own mailbox and its own
reservations, none of them visible to the others. There is no warning: every
call succeeds, the inbox is simply always empty and no reservation ever
conflicts. The coordination layer appears to work perfectly while coordinating
nothing.

## Why `PROJECT_IDENTITY_MODE=git-remote` does not fix it here

That mode looks like the right answer — it derives the slug from the normalised
git remote URL, which is identical on every host (`app.py:2020-2046`). But it
resolves the remote by opening a git repository **at `human_key` on the server**
(`_git_repo(human_key)`). In a containerised or otherwise remote deployment the
server has no copy of the client's checkout, that path does not exist, and the
code falls through to `return slugify(human_key)` — the dir behaviour, silently.

Verified on this deployment: the container cannot stat the host checkout path.
The identity modes are only useful when the server runs on the same machine, in
the same filesystem namespace, as the agents.

## The fix: one canonical key, agreed out of band

Every host must pass the **same literal string**. It does not need to be a path,
and it is better if it is not one. Use the repository's remote URL — globally
unique, identical everywhere, and self-documenting:

```
project_key = "git@github.com:owner/repo.git"
```

Set it once per host so no agent has to guess:

```
AGENT_MAIL_PROJECT_KEY=git@github.com:owner/repo.git
```

and state it in the project's `CLAUDE.md` / `AGENTS.md` so every agent uses it
verbatim rather than substituting its own working directory.

## Verifying

List the projects the server actually knows about:

```
docker compose -f compose.prod.yaml exec -T mcp-agent-mail \
  /app/.venv/bin/python -c "import sqlite3; \
  print(list(sqlite3.connect('/data/mailbox/storage.sqlite3') \
  .execute('select id, slug, human_key from projects')))"
```

More than one row for what is conceptually one repository means the hosts are
partitioned. Check this before diagnosing anything else: an empty inbox and a
never-conflicting reservation look identical to a correctly-idle system.
