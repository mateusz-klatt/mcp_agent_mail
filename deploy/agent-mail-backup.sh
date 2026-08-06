#!/usr/bin/env bash
# Agent Mail backup + archive mirror.
#
# Two independent things are protected here, and they are NOT interchangeable:
#
#   1. storage.sqlite3 — the relational source of truth (messages, agents,
#      threads, reservations, viewer logins). The git archive does NOT contain
#      it: the server's commits are path-targeted, so the database file stays
#      untracked even though it sits inside the archive directory. Losing it
#      loses everything the UI and the MCP tools read.
#   2. The git archive — the human-auditable markdown/attachment history the
#      server commits to a repository under STORAGE_ROOT. The server never
#      configures a remote and never pushes (there is no push/remote/origin
#      anywhere in storage.py), so mirroring it off-box is an external job.
#
# Both destinations live OUTSIDE the mcp_agent_mail working tree on purpose.
# ./data is gitignored-and-untracked, which is precisely what `git clean -xdf`
# removes — an off-tree copy is what makes that mistake survivable.
#
# Division of labour, which is not arbitrary:
#   * Everything touching $BACKUP_DIR runs INSIDE the container. That directory
#     must be owned by the container's uid (10001) for the server to write to
#     it, which means this script's own user cannot even list it. Snapshotting,
#     compression and retention therefore all happen container-side.
#   * The git mirror runs on the HOST, because the live archive directory is
#     world-readable and the bare repo belongs to the host user.
#
# Configuration comes from the ENVIRONMENT (e.g. set once at the top of the
# crontab), never from defaults baked in here: this repository is public, so
# nothing in it may name a machine, a user, a checkout or a domain. Every
# required value is checked below and the script exits loudly if one is missing,
# so a forgotten variable surfaces as a failed run rather than a silently wrong
# one.
#
#   AGENT_MAIL_PROJECT_DIR   checkout containing compose.prod.yaml and ./data
#   AGENT_MAIL_BARE_REPO     bare mirror repository (git init --bare)
#   AGENT_MAIL_LOG_DIR       directory for the monthly log and the lock file
#   AGENT_MAIL_KEEP_DAYS     snapshot retention in days (optional, default 30)
#
# Cron (hourly), with the values living in the crontab, not here:
#   AGENT_MAIL_PROJECT_DIR=/path/to/mcp_agent_mail
#   AGENT_MAIL_BARE_REPO=/path/to/archive.git
#   AGENT_MAIL_LOG_DIR=/path/to/backups
#   47 * * * * $AGENT_MAIL_PROJECT_DIR/deploy/agent-mail-backup.sh
#
# The script writes its own monthly log (see below) rather than relying on a
# redirect in the crontab, matching the convention of the other jobs on this
# host: routine output never reaches cron mail, so anything that DOES arrive at
# MAILTO is a real failure the script could not report itself — a missing file,
# a failed exec, an OOM kill.

set -uo pipefail

require() {
    # Named-variable indirection so a missing value names itself in the error.
    local name="$1" value="${!1:-}"
    if [ -z "$value" ]; then
        printf '%s FATAL: %s is not set (see the header of this script)\n' "$(date -Is)" "$name" >&2
        exit 2
    fi
}
require AGENT_MAIL_PROJECT_DIR
require AGENT_MAIL_BARE_REPO
require AGENT_MAIL_LOG_DIR

PROJECT_DIR="${AGENT_MAIL_PROJECT_DIR}"
COMPOSE_FILE="${PROJECT_DIR}/compose.prod.yaml"
LIVE_ARCHIVE="${PROJECT_DIR}/data/mailbox"
BARE_REPO="${AGENT_MAIL_BARE_REPO}"
KEEP_DAYS="${AGENT_MAIL_KEEP_DAYS:-30}"
CONTAINER="mcp-agent-mail"

LOG_DIR="${AGENT_MAIL_LOG_DIR}"
mkdir -p "$LOG_DIR"
# ALWAYS append to the monthly log — a manual run is exactly the run you later
# want a record of. Additionally mirror to stdout when a human is watching;
# cron's stdout is not a terminal, so cron stays silent and its mail keeps
# meaning "something failed in a way the script could not report".
if [ -t 1 ]; then
    exec > >(tee -a "${LOG_DIR}/agent-mail-$(date +%Y%m).log") 2>&1
else
    exec >>"${LOG_DIR}/agent-mail-$(date +%Y%m).log" 2>&1
fi

# Only one run at a time. An hourly schedule plus a slow push (or a hung
# network) would otherwise stack runs that fight over the same bare repo refs.
exec 9>"${LOG_DIR}/.agent-mail-backup.lock"
if ! flock -n 9; then
    printf '%s %s\n' "$(date -Is)" "another run is still in progress; skipping"
    exit 0
fi

log() { printf '%s %s\n' "$(date -Is)" "$*"; }
fail=0

# --- 1. SQLite snapshot (container-side) ------------------------------------
# VACUUM INTO, not `cp`: the database runs in WAL mode, so a byte copy of the
# main file can miss committed transactions still living in the -wal sidecar and
# produce a corrupt or silently stale restore. VACUUM INTO takes a read lock and
# emits a single consistent, already-compacted file.
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    log "ERROR: container ${CONTAINER} is not running; skipping SQLite snapshot"
    fail=1
else
    snapshot_out="$(docker compose -f "$COMPOSE_FILE" exec -T "$CONTAINER" \
        /app/.venv/bin/python -c "
import gzip, os, shutil, sqlite3, sys, time

BACKUP = '/backup'
KEEP_DAYS = ${KEEP_DAYS}
stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
raw = os.path.join(BACKUP, f'storage-{stamp}.sqlite3')
final = raw + '.gz'

con = sqlite3.connect('/data/mailbox/storage.sqlite3')
try:
    con.execute('VACUUM INTO ?', (raw,))
finally:
    con.close()

if not os.path.exists(raw) or os.path.getsize(raw) == 0:
    print('ERROR snapshot missing or empty')
    sys.exit(1)

with open(raw, 'rb') as src, gzip.open(final, 'wb', compresslevel=9) as dst:
    shutil.copyfileobj(src, dst)
os.unlink(raw)

# Integrity-check what we actually stored, not what we hoped to store. A backup
# that is never verified is a guess; this reads the compressed copy back.
tmp = final + '.verify'
try:
    with gzip.open(final, 'rb') as src, open(tmp, 'wb') as dst:
        shutil.copyfileobj(src, dst)
    v = sqlite3.connect(tmp)
    try:
        status = v.execute('PRAGMA integrity_check').fetchone()[0]
        rows = v.execute('select count(*) from messages').fetchone()[0]
    finally:
        v.close()
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)

if status != 'ok':
    print(f'ERROR integrity_check={status}')
    sys.exit(1)

cutoff = time.time() - KEEP_DAYS * 86400
pruned = 0
for name in os.listdir(BACKUP):
    # Only this script's own snapshots, matched by name, so nothing else that
    # happens to live here can be caught by the retention sweep.
    if name.startswith('storage-') and name.endswith('.sqlite3.gz'):
        p = os.path.join(BACKUP, name)
        if os.path.getmtime(p) < cutoff:
            os.unlink(p); pruned += 1

kept = sum(1 for n in os.listdir(BACKUP) if n.startswith('storage-') and n.endswith('.sqlite3.gz'))
print(f'OK {os.path.basename(final)} bytes={os.path.getsize(final)} messages={rows} pruned={pruned} kept={kept}')
" 2>&1)"

    if printf '%s' "$snapshot_out" | grep -q '^OK '; then
        log "sqlite ${snapshot_out#OK }"
    else
        log "ERROR: snapshot failed: ${snapshot_out}"
        fail=1
    fi
fi

# --- 2. Git archive mirror (host-side) --------------------------------------
# Pull INTO the bare repo rather than pushing FROM the live one. A fetch touches
# only the bare repo's refs and objects, so nothing here can take a lock in the
# live archive or race the server's commit queue. Never run gc/add/checkout in
# the live repo while the server is up.
if [ ! -d "${LIVE_ARCHIVE}/.git" ]; then
    log "archive: no git repo at ${LIVE_ARCHIVE} yet (nothing archived so far) — skipping mirror"
else
    # The live archive is owned by uid 10001 while this script runs as the host
    # user, and git refuses to read a repo owned by someone else unless told so.
    #
    # Register the CANONICAL path. Where the project directory is reached
    # through a symlink, git resolves it before matching safe.directory, so an
    # entry naming the symlinked path is silently never matched and every fetch
    # dies with "detected dubious ownership". Both spellings are registered.
    canonical_archive="$(readlink -f "$LIVE_ARCHIVE")"
    for candidate in "$canonical_archive" "$LIVE_ARCHIVE"; do
        git config --global --get-all safe.directory 2>/dev/null | grep -qxF "$candidate" \
            || git config --global --add safe.directory "$candidate"
    done

    if git --git-dir="$BARE_REPO" fetch --prune --quiet "$canonical_archive" \
            '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*' 2>&1; then
        # Keep the bare repo's HEAD pointing at whatever branch the server
        # actually commits to. The archive repo is created by the server, not by
        # us, so its default branch name is not ours to assume — a mismatch
        # leaves the mirror technically complete but unusable (`git clone` of it
        # checks out nothing, and every `rev-parse HEAD` fails).
        src_branch="$(git -C "$canonical_archive" symbolic-ref --short HEAD 2>/dev/null || echo '')"
        if [ -n "$src_branch" ] \
           && [ "$(git --git-dir="$BARE_REPO" symbolic-ref HEAD 2>/dev/null)" != "refs/heads/${src_branch}" ] \
           && git --git-dir="$BARE_REPO" show-ref --verify --quiet "refs/heads/${src_branch}"; then
            git --git-dir="$BARE_REPO" symbolic-ref HEAD "refs/heads/${src_branch}"
            log "archive: bare HEAD realigned to ${src_branch}"
        fi

        head_ref="$(git --git-dir="$BARE_REPO" rev-parse --short HEAD 2>/dev/null || echo none)"
        commits="$(git --git-dir="$BARE_REPO" rev-list --count HEAD 2>/dev/null || echo 0)"
        log "archive: mirrored into bare repo (HEAD ${head_ref}, ${commits} commits)"

        if git --git-dir="$BARE_REPO" remote get-url origin >/dev/null 2>&1; then
            if git --git-dir="$BARE_REPO" push --quiet --mirror origin 2>&1; then
                log "archive: pushed to origin"
            else
                log "ERROR: push to origin failed"
                fail=1
            fi
        else
            log "archive: no origin remote configured; local mirror only"
        fi
    else
        log "ERROR: fetch from live archive failed"
        fail=1
    fi
fi

log "done (failures: ${fail})"
exit "$fail"
