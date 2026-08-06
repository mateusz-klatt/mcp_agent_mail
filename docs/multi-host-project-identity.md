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

Every host must pass the **same literal string**, and that string must be
*path-shaped*: `ensure_project` rejects anything for which
`Path(human_key).is_absolute()` is false (`app.py:5890`). A git remote URL —
the otherwise obvious choice for a machine-independent identifier — is refused
outright.

It does not, however, have to be a path that exists. The code is explicit
(`app.py:5889`): *"It need not exist on disk — it is an opaque project KEY, not
a filesystem probe."* So use a **synthetic** absolute path, deliberately one
that is not anybody's real checkout, so that nobody helpfully "corrects" it to
their own working directory:

```
AGENT_MAIL_PROJECT_KEY=/owner/repo
```

Mirroring the `owner/repo` of the remote keeps it unique and self-explanatory
while remaining identical on Linux, macOS and Windows. Note the check runs on
the server, so a POSIX-style leading slash is correct even for Windows clients.

State it in the project's `CLAUDE.md` / `AGENTS.md` as well, so an agent reading
its instructions uses it verbatim rather than substituting its own cwd.

## Agent names have a matching trap

Names are unique per project and an existing identity can only be re-registered
by presenting the `registration_token` from its first registration — so the
token is a durable credential worth persisting, not session state.

Less obviously: a requested name that looks like a program or model name is
**silently replaced** with a random one rather than rejected. The check is
`_looks_like_program_name(...) or _looks_like_model_name(...)` (`app.py:3272`),
and the default enforcement mode is `coerce`, which falls through to
auto-generation instead of raising. `claude-<host>` therefore does not do what
it appears to do; `<host>-1` does.

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
