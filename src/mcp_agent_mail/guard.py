"""Pre-commit guard helpers for MCP Agent Mail."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

from .config import Settings
from .storage import ProjectArchive, ensure_archive

__all__ = [
    "install_guard",
    "install_prepush_guard",
    "render_precommit_script",
    "render_prepush_script",
    "uninstall_guard",
]

_HOOKS_DIRECTORY = "hooks.d"
_GUARD_PLUGIN_NAME = "50-agent-mail.py"
_CHAIN_RUNNER_MARKER = "mcp-agent-mail chain-runner"
_LEGACY_HOOK_SENTINELS = (
    "mcp-agent-mail guard hook",
    "AGENT_NAME environment variable is required.",
)

_SCRIPT_SHEBANG = "#!/usr/bin/env python3"
_SCRIPT_IMPORT_OS = "import os"
_SCRIPT_IMPORT_SYS = "import sys"
_SCRIPT_IMPORT_SUBPROCESS = "import subprocess"
_SCRIPT_IMPORT_PATH = "from pathlib import Path"
_SCRIPT_TRY = "    try:"
_SCRIPT_NESTED_TRY = "        try:"
_SCRIPT_EXCEPT = "except Exception:"
_SCRIPT_INDENTED_EXCEPT = "    except Exception:"
_SCRIPT_NESTED_EXCEPT = "        except Exception:"
_SCRIPT_RETURN_NONE = "        return None"
_SCRIPT_NESTED_RETURN_NONE = "            return None"
_SCRIPT_RETURN_FALSE = "        return False"
_SCRIPT_CONTINUE = "            continue"
_SCRIPT_NESTED_CONTINUE = "                continue"
_SCRIPT_EXIT_SUCCESS = "sys.exit(0)"
_SCRIPT_INDENTED_EXIT_SUCCESS = "    sys.exit(0)"
_SCRIPT_INDENTED_EXIT_FAILURE = "    sys.exit(1)"
_SCRIPT_NESTED_EXIT_FAILURE = "        sys.exit(1)"
_SCRIPT_RETURN_CODE_CHECK = "    if rc != 0:"
_SCRIPT_EXIT_RETURN_CODE = "        sys.exit(rc)"
_SCRIPT_ENFORCEMENT_CHECK = "    if EXECUTION_ENFORCEMENT == 'enforce':"
_SCRIPT_GIT_RUN_OPTIONS = "                            check=False,capture_output=True,text=True)"


def _render_chain_runner_script(hook_name: str) -> str:
    """
    Render a Python chain-runner for the given Git hook name.

    Behavior:
    - Runs executables in hooks.d/<hook_name>/* in lexical order.
    - For pre-push, reads STDIN once and forwards it to each child hook.
    - If a <hook_name>.orig exists and is executable, it is invoked last.
      Husky v9 stubs are sourced with the real hook name as ``argv[0]`` so
      Husky resolves the tracked hook instead of silently looking for an
      artificial ``<hook_name>.orig`` hook.
    - On Windows, where CreateProcess cannot honor shebangs, Python children
      use the running interpreter, native executables run directly, and shell
      children use ``sh`` from PATH or the Git-for-Windows installation.
    - Exits non-zero on the first non-zero child exit code.
    """
    lines: list[str] = [
        _SCRIPT_SHEBANG,
        f"# mcp-agent-mail chain-runner ({hook_name})",
        _SCRIPT_IMPORT_OS,
        "import shlex",
        "import shutil",
        _SCRIPT_IMPORT_SYS,
        "import stat",
        _SCRIPT_IMPORT_SUBPROCESS,
        _SCRIPT_IMPORT_PATH,
        "",
        "HOOK_DIR = Path(__file__).parent",
        f"RUN_DIR = HOOK_DIR / 'hooks.d' / '{hook_name}'",
        f"ORIG = HOOK_DIR / '{hook_name}.orig'",
        f"HOOK_NAME = '{hook_name}'",
        "HUSKY_H = HOOK_DIR / 'h'",
        "",
        "def _is_exec(p: Path) -> bool:",
        _SCRIPT_TRY,
        "        st = p.stat()",
        "        return bool(st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))",
        _SCRIPT_INDENTED_EXCEPT,
        _SCRIPT_RETURN_FALSE,
        "",
        "def _list_execs() -> list[Path]:",
        "    if not RUN_DIR.exists() or not RUN_DIR.is_dir():",
        "        return []",
        "    items = sorted([p for p in RUN_DIR.iterdir() if p.is_file()], key=lambda p: p.name)",
        "    # POSIX has a meaningful executable bit. Windows entries are",
        "    # validated by _windows_argv before anything is launched.",
        "    if os.name == 'posix':",
        _SCRIPT_NESTED_TRY,
        "            items = [p for p in items if _is_exec(p)]",
        _SCRIPT_NESTED_EXCEPT,
        "            pass",
        "    return items",
        "",
        "# Forward Git's hook arguments (e.g. pre-push <remote> <url>) to children.",
        "ARGV = sys.argv[1:]",
        "",
        "def _win_sh() -> str:",
        "    # A hook can be launched outside Git's own shell environment, so",
        "    # resolve sh explicitly and then inspect Git for Windows as fallback.",
        "    found = shutil.which('sh')",
        "    if found:",
        "        return found",
        _SCRIPT_TRY,
        "        cp = subprocess.run(",
        "            ['git', '--exec-path'],",
        "            capture_output=True,",
        "            text=True,",
        "            check=False,",
        "        )",
        "        exec_path = (cp.stdout or '').strip()",
        _SCRIPT_INDENTED_EXCEPT,
        "        exec_path = ''",
        "    if exec_path:",
        "        base = Path(exec_path)",
        "        for anchor in (base, *base.parents):",
        "            for rel in (('usr', 'bin', 'sh.exe'), ('bin', 'sh.exe')):",
        "                candidate = anchor.joinpath(*rel)",
        "                if candidate.is_file():",
        "                    return str(candidate)",
        "    return 'sh'",
        "",
        "def _read_shebang(path: Path) -> str:",
        _SCRIPT_TRY,
        "        with path.open('rb') as handle:",
        "            first = handle.readline(256).decode('utf-8', 'ignore').strip()",
        _SCRIPT_INDENTED_EXCEPT,
        "        return ''",
        "    return first[2:].strip() if first.startswith('#!') else ''",
        "",
        "def _shell_path(path: Path) -> str:",
        "    value = str(path)",
        "    return value.replace('\\\\', '/') if os.name != 'posix' else value",
        "",
        "def _windows_argv(path: Path):",
        "    # CreateProcess cannot honor shebangs, and Python may implicitly",
        "    # hand .bat/.cmd files to cmd.exe without safely quoting Git's",
        "    # remote arguments. Resolve an explicit interpreter or fail closed.",
        "    suffix = path.suffix.lower()",
        "    if suffix == '.py':",
        "        return [sys.executable, str(path), *ARGV]",
        "    if suffix in ('.exe', '.com'):",
        "        return [str(path), *ARGV]",
        "    if suffix in ('.bat', '.cmd'):",
        _SCRIPT_RETURN_NONE,
        "    if suffix == '.ps1':",
        "        powershell = shutil.which('pwsh') or shutil.which('powershell')",
        "        if not powershell:",
        _SCRIPT_NESTED_RETURN_NONE,
        "        return [powershell, '-NoProfile', '-NonInteractive', '-File', str(path), *ARGV]",
        "    shebang = _read_shebang(path)",
        "    if not shebang:",
        "        if suffix == '.sh':",
        "            return [_win_sh(), _shell_path(path), *ARGV]",
        _SCRIPT_RETURN_NONE,
        _SCRIPT_TRY,
        "        parts = shlex.split(shebang, posix=True)",
        "    except ValueError:",
        _SCRIPT_RETURN_NONE,
        "    if not parts:",
        _SCRIPT_RETURN_NONE,
        "    command = Path(parts[0].replace('\\\\', '/')).name.lower()",
        "    interpreter_args = parts[1:]",
        "    if command in ('env', 'env.exe'):",
        "        if interpreter_args[:1] == ['-S']:",
        "            interpreter_args = interpreter_args[1:]",
        "        if not interpreter_args or interpreter_args[0].startswith('-'):",
        _SCRIPT_NESTED_RETURN_NONE,
        "        command = Path(interpreter_args[0].replace('\\\\', '/')).name.lower()",
        "        interpreter_args = interpreter_args[1:]",
        "    if command in ('python', 'python3', 'python.exe', 'python3.exe'):",
        "        return [sys.executable, *interpreter_args, str(path), *ARGV]",
        "    if command in ('sh', 'sh.exe'):",
        "        return [_win_sh(), *interpreter_args, _shell_path(path), *ARGV]",
        "    if command in ('bash', 'bash.exe', 'dash', 'dash.exe'):",
        "        interpreter = shutil.which(command)",
        "        if not interpreter:",
        _SCRIPT_NESTED_RETURN_NONE,
        "        return [interpreter, *interpreter_args, _shell_path(path), *ARGV]",
        "    if command in ('cmd', 'cmd.exe'):",
        _SCRIPT_RETURN_NONE,
        "    interpreter = shutil.which(command)",
        "    if not interpreter:",
        _SCRIPT_RETURN_NONE,
        "    return [interpreter, *interpreter_args, str(path), *ARGV]",
        "",
        "def _run_child(path: Path, *, stdin_bytes=None):",
        "    argv = [str(path), *ARGV]",
        "    if os.name != 'posix':",
        "        argv = _windows_argv(path)",
        "        if argv is None:",
        "            sys.stderr.write(f'Unsupported Windows hook child: {path}\\n')",
        "            return 126",
        "    return subprocess.run(argv, input=stdin_bytes, check=False).returncode",
        "",
        "def _is_husky_stub(path: Path) -> bool:",
        "    # Husky v9 puts a tiny hook beside resolver 'h'. The resolver uses",
        "    # basename($0), so executing the renamed .orig would skip the",
        "    # repository's tracked .husky/<hook-name> hook.",
        "    if not HUSKY_H.is_file():",
        _SCRIPT_RETURN_FALSE,
        _SCRIPT_TRY,
        "        text = path.read_text(encoding='utf-8', errors='ignore')",
        _SCRIPT_INDENTED_EXCEPT,
        _SCRIPT_RETURN_FALSE,
        "    normalized = text.replace('\\\\', '/')",
        "    body = [line.strip() for line in normalized.splitlines()",
        "            if line.strip() and not line.lstrip().startswith('#')]",
        "    return body.count('. \"$(dirname \"$0\")/h\"') == 1",
        "",
        "def _run_orig(*, stdin_bytes=None):",
        "    if _is_husky_stub(ORIG):",
        "        # Source the preserved stub under the original hook name so all",
        "        # of its commands run and Husky's h resolver sees the right $0.",
        "        shell = '/bin/sh' if os.name == 'posix' else _win_sh()",
        "        argv0 = _shell_path(HOOK_DIR / HOOK_NAME)",
        "        snippet = 'orig=\"$1\"; shift; . \"$orig\"'",
        "        return subprocess.run(",
        "            [shell, '-c', snippet, argv0, _shell_path(ORIG), *ARGV],",
        "            input=stdin_bytes,",
        "            check=False,",
        "        ).returncode",
        "    return _run_child(ORIG, stdin_bytes=stdin_bytes)",
        "",
    ]
    if hook_name == "pre-push":
        lines += [
            "# Read STDIN once (Git passes ref tuples); forward to children",
            "stdin_bytes = sys.stdin.buffer.read()",
            "for exe in _list_execs():",
            "    rc = _run_child(exe, stdin_bytes=stdin_bytes)",
            _SCRIPT_RETURN_CODE_CHECK,
            _SCRIPT_EXIT_RETURN_CODE,
            "",
            "# Run the preserved original hook last (POSIX: only if it is executable).",
            "if ORIG.exists() and (os.name != 'posix' or _is_exec(ORIG)):",
            "    rc = _run_orig(stdin_bytes=stdin_bytes)",
            _SCRIPT_RETURN_CODE_CHECK,
            _SCRIPT_EXIT_RETURN_CODE,
            _SCRIPT_EXIT_SUCCESS,
        ]
    else:
        lines += [
            "for exe in _list_execs():",
            "    rc = _run_child(exe)",
            _SCRIPT_RETURN_CODE_CHECK,
            _SCRIPT_EXIT_RETURN_CODE,
            "",
            "# Run the preserved original hook last (POSIX: only if it is executable).",
            "if ORIG.exists() and (os.name != 'posix' or _is_exec(ORIG)):",
            "    rc = _run_orig()",
            _SCRIPT_RETURN_CODE_CHECK,
            _SCRIPT_EXIT_RETURN_CODE,
            _SCRIPT_EXIT_SUCCESS,
        ]
    return "\n".join(lines) + "\n"


async def _preserve_foreign_hook(chain_path: Path, marker: str) -> None:
    """Preserve a foreign hook without overwriting a different saved original."""

    if not chain_path.exists():
        return
    try:
        content = await asyncio.to_thread(chain_path.read_text, "utf-8")
    except Exception:
        content = ""
    if marker in content:
        return

    orig_path = chain_path.with_name(f"{chain_path.name}.orig")
    if orig_path.exists():
        current_bytes, original_bytes = await asyncio.gather(
            asyncio.to_thread(chain_path.read_bytes),
            asyncio.to_thread(orig_path.read_bytes),
        )
        if current_bytes != original_bytes:
            raise FileExistsError(
                "Refusing to replace a foreign Git hook because a different "
                f"saved hook already exists: {chain_path} and {orig_path}"
            )
        return
    await asyncio.to_thread(chain_path.replace, orig_path)


def _git(cwd: Path, *args: str) -> str | None:
    try:
        cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
        return cp.stdout.strip()
    except Exception:
        return None


def _resolve_hooks_dir(repo: Path) -> Path:
    # Prefer core.hooksPath if configured
    hooks_path = _git(repo, "config", "--get", "core.hooksPath")
    if hooks_path:
        # Expand user (e.g. ~/.githooks)
        p = Path(hooks_path).expanduser()
        if p.is_absolute():
            return p
        # Resolve relative to repo root
        root = _git(repo, "rev-parse", "--show-toplevel") or str(repo)
        return Path(root) / hooks_path

    # Fall back to git-dir/hooks
    git_dir = _git(repo, "rev-parse", "--git-dir")
    if git_dir:
        g = Path(git_dir)
        if not g.is_absolute():
            g = repo / g
        return g / "hooks"
    # Last resort: traditional path
    return repo / ".git" / "hooks"



def render_precommit_script(archive: ProjectArchive) -> str:
    """Return the pre-commit script content for the given archive.

    Construct with explicit lines at column 0 to avoid indentation errors.
    """

    file_reservations_dir = str((archive.root / "file_reservations").resolve()).replace("\\", "/")
    storage_root = str(archive.root.resolve()).replace("\\", "/")
    lines = [
        _SCRIPT_SHEBANG,
        "# mcp-agent-mail guard hook (pre-commit)",
        "import json",
        _SCRIPT_IMPORT_OS,
        _SCRIPT_IMPORT_SYS,
        _SCRIPT_IMPORT_SUBPROCESS,
        _SCRIPT_IMPORT_PATH,
        "import fnmatch as _fn",
        "from datetime import datetime, timezone",
        "",
        "# Optional Git pathspec support (preferred when available)",
        "try:",
        "    from pathspec import PathSpec as _PS  # type: ignore[import-not-found]",
        _SCRIPT_EXCEPT,
        "    _PS = None  # type: ignore[assignment]",
        "",
        f"FILE_RESERVATIONS_DIR = Path({json.dumps(file_reservations_dir)})",
        f"STORAGE_ROOT = Path({json.dumps(storage_root)})",
        "",
        "# Gate variables (presence) and mode",
        "TRUTHY = {\"1\",\"true\",\"t\",\"yes\",\"y\"}",
        "GATE_ENABLED = (",
        "    os.environ.get(\"WORKTREES_ENABLED\", \"0\").strip().lower() in TRUTHY",
        "    or os.environ.get(\"GIT_IDENTITY_ENABLED\", \"0\").strip().lower() in TRUTHY",
        ")",
        "",
        "# Exit early if gate is not enabled (WORKTREES_ENABLED=0 and GIT_IDENTITY_ENABLED=0)",
        "if not GATE_ENABLED:",
        _SCRIPT_INDENTED_EXIT_SUCCESS,
        "",
        "# Advisory/blocking mode: default to 'block' unless explicitly set to 'warn'.",
        "MODE = (os.environ.get(\"AGENT_MAIL_GUARD_MODE\",\"block\") or \"block\").strip().lower()",
        "ADVISORY = MODE in {\"warn\",\"advisory\",\"adv\"}",
        "",
        "# Emergency bypass",
        "if (os.environ.get(\"AGENT_MAIL_BYPASS\",\"0\") or \"0\").strip().lower() in {\"1\",\"true\",\"t\",\"yes\",\"y\"}:",
        "    sys.stderr.write(\"[pre-commit] bypass enabled via AGENT_MAIL_BYPASS=1\\n\")",
        _SCRIPT_INDENTED_EXIT_SUCCESS,
        "AGENT_NAME = os.environ.get(\"AGENT_NAME\")",
        "if not AGENT_NAME:",
        "    sys.stderr.write(\"[pre-commit] AGENT_NAME environment variable is required.\\n\")",
        _SCRIPT_INDENTED_EXIT_FAILURE,
        "def _current_execution_context():",
        "    explicit = (os.environ.get(\"AGENT_EXECUTION_ID\") or \"\").strip()",
        "    if explicit:",
        "        ancestors = {value.strip() for value in (os.environ.get(\"AGENT_EXECUTION_ANCESTOR_IDS\") or \"\").split(',') if value.strip()}",
        "        ancestors.discard(explicit)",
        "        return explicit, ancestors, None",
        _SCRIPT_TRY,
        "        cp = subprocess.run([\"git\",\"rev-parse\",\"--path-format=absolute\",\"--git-path\",\"agent-mail/execution-id\"],",
        _SCRIPT_GIT_RUN_OPTIONS,
        "        marker_text = cp.stdout.strip()",
        "        if not marker_text:",
        "            return \"\", set(), 'execution marker path is unavailable'",
        "        marker = Path(marker_text)",
        "        if not marker.is_absolute():",
        "            marker = Path.cwd() / marker",
        "        raw = marker.read_text(encoding=\"utf-8\").strip()",
        "        if not raw:",
        "            return \"\", set(), 'execution marker is empty'",
        _SCRIPT_NESTED_TRY,
        "            payload = json.loads(raw)",
        _SCRIPT_NESTED_EXCEPT,
        "            return \"\", set(), 'execution marker is not valid JSON'",
        "        if not isinstance(payload, dict):",
        "            return \"\", set(), 'execution marker must be a JSON object'",
        "        if payload.get(\"status\") != \"active\":",
        "            return \"\", set(), 'execution marker is not active'",
        "        execution_id = str(payload.get(\"execution_id\") or \"\").strip()",
        "        if not execution_id:",
        "            return \"\", set(), 'execution marker has no execution_id'",
        "        ancestor_values = payload.get(\"ancestor_execution_ids\", [])",
        "        if not isinstance(ancestor_values, list) or not all(isinstance(value, str) and value.strip() for value in ancestor_values):",
        "            return \"\", set(), 'execution marker has invalid ancestor_execution_ids'",
        "        ancestors = {value.strip() for value in ancestor_values}",
        "        ancestors.discard(execution_id)",
        "        heartbeat_raw = payload.get(\"heartbeat_ts\")",
        "        if not isinstance(heartbeat_raw, str) or not heartbeat_raw.strip():",
        "            return \"\", set(), 'execution marker has no heartbeat_ts'",
        "        heartbeat_text = heartbeat_raw.strip()",
        "        if heartbeat_text.endswith(\"Z\"):",
        "            heartbeat_text = heartbeat_text[:-1] + \"+00:00\"",
        "        heartbeat = datetime.fromisoformat(heartbeat_text)",
        "        if heartbeat.tzinfo is None or heartbeat.tzinfo.utcoffset(heartbeat) is None:",
        "            return \"\", set(), 'execution marker heartbeat is not timezone-aware'",
        "        heartbeat = heartbeat.astimezone(timezone.utc)",
        "        max_age_raw = os.environ.get(\"AGENT_EXECUTION_MARKER_MAX_AGE_SECONDS\", \"1800\")",
        _SCRIPT_NESTED_TRY,
        "            max_age = max(60, int(max_age_raw))",
        "        except (TypeError, ValueError):",
        "            return \"\", set(), 'AGENT_EXECUTION_MARKER_MAX_AGE_SECONDS is invalid'",
        "        age_seconds = (datetime.now(timezone.utc) - heartbeat).total_seconds()",
        "        if age_seconds < -300 or age_seconds > max_age:",
        "            return \"\", set(), 'execution marker heartbeat is stale or in the future'",
        "        return execution_id, ancestors, None",
        "    except FileNotFoundError:",
        "        return \"\", set(), 'execution marker is missing'",
        _SCRIPT_INDENTED_EXCEPT,
        "        return \"\", set(), 'execution marker cannot be read'",
        "EXECUTION_ID, ANCESTOR_EXECUTION_IDS, EXECUTION_CONTEXT_ERROR = _current_execution_context()",
        "COMPATIBLE_EXECUTION_IDS = ({EXECUTION_ID} | ANCESTOR_EXECUTION_IDS) if EXECUTION_ID else set()",
        "EXECUTION_ENFORCEMENT = (os.environ.get(\"AGENT_EXECUTION_ENFORCEMENT_MODE\", \"observe\") or \"observe\").strip().lower()",
        "if EXECUTION_CONTEXT_ERROR and EXECUTION_CONTEXT_ERROR != 'execution marker is missing':",
        _SCRIPT_ENFORCEMENT_CHECK,
        "        sys.stderr.write(f\"[pre-commit] blocked: {EXECUTION_CONTEXT_ERROR}. Start or resume an AgentExecution first.\\n\")",
        _SCRIPT_NESTED_EXIT_FAILURE,
        "    sys.stderr.write(f\"[pre-commit] observe: {EXECUTION_CONTEXT_ERROR}; continuing without self-suppression.\\n\")",
        "if not EXECUTION_ID:",
        _SCRIPT_ENFORCEMENT_CHECK,
        "        sys.stderr.write(\"[pre-commit] AGENT_EXECUTION_ENFORCEMENT_MODE=enforce requires an active execution marker.\\n\")",
        _SCRIPT_NESTED_EXIT_FAILURE,
        "    sys.stderr.write(\"[pre-commit] observe: no active AgentExecution marker; legacy unscoped mode.\\n\")",
        "",
        "# Collect staged paths (name-only) and expand renames/moves (old+new)",
        "paths = []",
        "try:",
        "    co = subprocess.run([\"git\",\"diff\",\"--cached\",\"--name-only\",\"-z\",\"--diff-filter=ACMRDTU\"],",
        "                        check=True,capture_output=True)",
        "    data = co.stdout.decode(\"utf-8\",\"ignore\")",
        "    for p in data.split(\"\\x00\"):",
        "        if p:",
        "            paths.append(p)",
        "    # Rename detection: capture both old and new names",
        "    cs = subprocess.run([\"git\",\"diff\",\"--cached\",\"--name-status\",\"-M\",\"-z\"],",
        "                        check=True,capture_output=True)",
        "    sdata = cs.stdout.decode(\"utf-8\",\"ignore\")",
        "    parts = [x for x in sdata.split(\"\\x00\") if x]",
        "    i = 0",
        "    while i < len(parts):",
        "        status = parts[i]",
        "        i += 1",
        "        if status.startswith(\"R\") and i + 1 < len(parts):",
        "            oldp = parts[i]; newp = parts[i+1]; i += 2",
        "            if oldp: paths.append(oldp)",
        "            if newp: paths.append(newp)",
        "        else:",
        "            # Status followed by one path",
        "            if i < len(parts):",
        "                pth = parts[i]; i += 1",
        "                if pth: paths.append(pth)",
        _SCRIPT_EXCEPT,
        "    pass",
        "",
        "if not paths:",
        _SCRIPT_INDENTED_EXIT_SUCCESS,
        "",
        "# Local conflict detection against FILE_RESERVATIONS_DIR",
        "def _now_utc():",
        "    return datetime.now(timezone.utc)",
        "def _parse_iso(value):",
        "    if not value:",
        _SCRIPT_RETURN_NONE,
        _SCRIPT_TRY,
        "        text = value",
        "        if text.endswith(\"Z\"):",
        "            text = text[:-1] + \"+00:00\"",
        "        dt = datetime.fromisoformat(text)",
        "        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:",
        "            dt = dt.replace(tzinfo=timezone.utc)",
        "        return dt.astimezone(timezone.utc)",
        _SCRIPT_INDENTED_EXCEPT,
        _SCRIPT_RETURN_NONE,
        "def _not_expired(expires_ts):",
        "    parsed = _parse_iso(expires_ts)",
        "    if parsed is None:",
        "        return True",
        "    return parsed > _now_utc()",
        "# Honor core.ignorecase: case-fold paths and patterns before matching (#194).",
        "def _detect_ignorecase():",
        _SCRIPT_TRY,
        "        cp = subprocess.run([\"git\",\"config\",\"--type=bool\",\"--get\",\"core.ignorecase\"],",
        _SCRIPT_GIT_RUN_OPTIONS,
        "        return cp.stdout.strip() == \"true\"",
        _SCRIPT_INDENTED_EXCEPT,
        _SCRIPT_RETURN_FALSE,
        "IGNORECASE = _detect_ignorecase()",
        "def _casefold(value):",
        "    return value.lower() if IGNORECASE else value",
        "def _compile_one(patt):",
        "    q = _casefold(patt.replace(\"\\\\\",\"/\"))",
        "    if _PS:",
        _SCRIPT_NESTED_TRY,
        "            return _PS.from_lines(\"gitignore\", [q])",
        _SCRIPT_NESTED_EXCEPT,
        _SCRIPT_NESTED_RETURN_NONE,
        "    return None",
        "",
        "# Phase 1: Pre-load and compile all reservation patterns ONCE",
        "compiled_patterns = []",
        "legacy_self_notices = []",
        "all_pattern_strings = []",
        "seen_ids = set()",
        "try:",
        "    for f in FILE_RESERVATIONS_DIR.iterdir():",
        "        if not f.name.endswith('.json'):",
        _SCRIPT_CONTINUE,
        _SCRIPT_NESTED_TRY,
        "            data = json.loads(f.read_text(encoding='utf-8'))",
        _SCRIPT_NESTED_EXCEPT,
        _SCRIPT_CONTINUE,
        "        recs = data if isinstance(data, list) else [data]",
        "        for r in recs:",
        "            if not isinstance(r, dict):",
        _SCRIPT_NESTED_CONTINUE,
        "            rid = r.get('id')",
        "            if rid is not None:",
        "                rid_key = str(rid)",
        "                if rid_key in seen_ids:",
        "                    continue",
        "                seen_ids.add(rid_key)",
        "            patt = (r.get('path_pattern') or '').strip()",
        "            if not patt:",
        _SCRIPT_NESTED_CONTINUE,
        "            # Skip virtual namespace reservations (tool://, resource://, service://) — bd-14z",
        "            if any(patt.startswith(pfx) for pfx in ('tool://', 'resource://', 'service://')):",
        _SCRIPT_NESTED_CONTINUE,
        "            holder = (r.get('agent') or '').strip()",
        "            holder_execution = (r.get('execution_id') or '').strip()",
        "            exclusive = r.get('exclusive', True)",
        "            released = (r.get('released_ts') or '').strip()",
        "            expires = (r.get('expires_ts') or '').strip()",
        "            if not exclusive:",
        _SCRIPT_NESTED_CONTINUE,
        "            if released:",
        _SCRIPT_NESTED_CONTINUE,
        "            if not _not_expired(expires):",
        _SCRIPT_NESTED_CONTINUE,
        "            # A durable Agent can have concurrent root/subagent executions.",
        "            # Exact/ancestor claims are compatible. During the observe",
        "            # migration window, an active legacy claim owned by this same",
        "            # durable Agent is reported honestly but does not masquerade as",
        "            # a sibling conflict. Enforce mode remains fail-closed.",
        "            if holder == AGENT_NAME and holder_execution in COMPATIBLE_EXECUTION_IDS:",
        _SCRIPT_NESTED_CONTINUE,
        "            if holder == AGENT_NAME and not holder_execution and EXECUTION_ENFORCEMENT != 'enforce':",
        "                legacy_self_notices.append((patt, expires))",
        _SCRIPT_NESTED_CONTINUE,
        "            # Pre-compile pattern ONCE (not per-path)",
        "            spec = _compile_one(patt)",
        "            patt_norm = _casefold(patt.replace('\\\\','/').lstrip('/'))",
        "            compiled_patterns.append((spec, patt, patt_norm, holder, holder_execution))",
        "            all_pattern_strings.append(patt_norm)",
        _SCRIPT_EXCEPT,
        "    compiled_patterns = []",
        "    all_pattern_strings = []",
        "    legacy_self_notices = []",
        "if legacy_self_notices:",
        "    sys.stderr.write('[pre-commit] observe: active legacy_unscoped claim(s) owned by this Agent; drain before enforce.\\n')",
        "    for patt, expires in legacy_self_notices[:10]:",
        "        sys.stderr.write(f'- legacy claim {patt} expires {expires or \"<unknown>\"}\\n')",
        "",
        "# Phase 2: Build union PathSpec for fast-path rejection",
        "union_spec = None",
        "if _PS and all_pattern_strings:",
        _SCRIPT_TRY,
        "        union_spec = _PS.from_lines(\"gitignore\", all_pattern_strings)",
        _SCRIPT_INDENTED_EXCEPT,
        "        union_spec = None",
        "",
        "# Phase 3: Check paths against compiled patterns",
        "conflicts = []",
        "if compiled_patterns:",
        "    for p in paths:",
        "        norm = _casefold(p.replace('\\\\','/').lstrip('/'))",
        "        # Fast-path: if union_spec exists and path doesn't match ANY pattern, skip",
        "        if union_spec is not None and not union_spec.match_file(norm):",
        _SCRIPT_CONTINUE,
        "        # Detailed matching for conflict attribution",
        "        for spec, patt, patt_norm, holder, holder_execution in compiled_patterns:",
        "            matched = spec.match_file(norm) if spec is not None else _fn.fnmatch(norm, patt_norm)",
        "            if matched:",
        "                conflicts.append((patt, p, holder, holder_execution))",
        "if conflicts:",
        "    sys.stderr.write(\"Exclusive file_reservation conflicts detected\\n\")",
        "    for patt, path, holder, holder_execution in conflicts[:10]:",
        "        execution_label = holder_execution or '<legacy-unscoped>'",
        "        sys.stderr.write(f\"- {path} matches {patt} (holder: {holder}, execution: {execution_label})\\n\")",
        "    if ADVISORY:",
        "        sys.exit(0)",
        _SCRIPT_INDENTED_EXIT_FAILURE,
        _SCRIPT_EXIT_SUCCESS,
    ]
    return "\n".join(lines) + "\n"


def render_prepush_script(archive: ProjectArchive) -> str:
    """Return the pre-push script content that checks conflicts across pushed commits.

    Python script to avoid external shell assumptions; NUL-safe and respects gate/advisory mode.
    """
    file_reservations_dir = str((archive.root / "file_reservations").resolve()).replace("\\", "/")
    lines = [
        _SCRIPT_SHEBANG,
        "# mcp-agent-mail guard hook (pre-push)",
        "import json",
        _SCRIPT_IMPORT_OS,
        _SCRIPT_IMPORT_SYS,
        _SCRIPT_IMPORT_SUBPROCESS,
        _SCRIPT_IMPORT_PATH,
        "import fnmatch as _fn",
        "from datetime import datetime, timezone",
        "",
        "# Optional Git pathspec support (preferred when available)",
        "try:",
        "    from pathspec import PathSpec as _PS  # type: ignore[import-not-found]",
        _SCRIPT_EXCEPT,
        "    _PS = None  # type: ignore[assignment]",
        "",
        f"FILE_RESERVATIONS_DIR = Path({json.dumps(file_reservations_dir)})",
        "",
        "# Gate variables (presence) and mode",
        "TRUTHY = {\"1\",\"true\",\"t\",\"yes\",\"y\"}",
        "GATE_ENABLED = (",
        "    os.environ.get(\"WORKTREES_ENABLED\", \"0\").strip().lower() in TRUTHY",
        "    or os.environ.get(\"GIT_IDENTITY_ENABLED\", \"0\").strip().lower() in TRUTHY",
        ")",
        "",
        "# Exit early if gate is not enabled (WORKTREES_ENABLED=0 and GIT_IDENTITY_ENABLED=0)",
        "if not GATE_ENABLED:",
        _SCRIPT_INDENTED_EXIT_SUCCESS,
        "",
        "MODE = (os.environ.get(\"AGENT_MAIL_GUARD_MODE\",\"block\") or \"block\").strip().lower()",
        "ADVISORY = MODE in {\"warn\",\"advisory\",\"adv\"}",
        "if (os.environ.get(\"AGENT_MAIL_BYPASS\",\"0\") or \"0\").strip().lower() in {\"1\",\"true\",\"t\",\"yes\",\"y\"}:",
        "    sys.stderr.write(\"[pre-push] bypass enabled via AGENT_MAIL_BYPASS=1\\n\")",
        _SCRIPT_INDENTED_EXIT_SUCCESS,
        "AGENT_NAME = os.environ.get(\"AGENT_NAME\")",
        "if not AGENT_NAME:",
        "    sys.stderr.write(\"[pre-push] AGENT_NAME environment variable is required.\\n\")",
        _SCRIPT_INDENTED_EXIT_FAILURE,
        "def _current_execution_context():",
        "    explicit = (os.environ.get(\"AGENT_EXECUTION_ID\") or \"\").strip()",
        "    if explicit:",
        "        ancestors = {value.strip() for value in (os.environ.get(\"AGENT_EXECUTION_ANCESTOR_IDS\") or \"\").split(',') if value.strip()}",
        "        ancestors.discard(explicit)",
        "        return explicit, ancestors, None",
        _SCRIPT_TRY,
        "        cp = subprocess.run([\"git\",\"rev-parse\",\"--path-format=absolute\",\"--git-path\",\"agent-mail/execution-id\"],",
        _SCRIPT_GIT_RUN_OPTIONS,
        "        marker_text = cp.stdout.strip()",
        "        if not marker_text:",
        "            return \"\", set(), 'execution marker path is unavailable'",
        "        marker = Path(marker_text)",
        "        if not marker.is_absolute():",
        "            marker = Path.cwd() / marker",
        "        raw = marker.read_text(encoding=\"utf-8\").strip()",
        "        if not raw:",
        "            return \"\", set(), 'execution marker is empty'",
        _SCRIPT_NESTED_TRY,
        "            payload = json.loads(raw)",
        _SCRIPT_NESTED_EXCEPT,
        "            return \"\", set(), 'execution marker is not valid JSON'",
        "        if not isinstance(payload, dict):",
        "            return \"\", set(), 'execution marker must be a JSON object'",
        "        if payload.get(\"status\") != \"active\":",
        "            return \"\", set(), 'execution marker is not active'",
        "        execution_id = str(payload.get(\"execution_id\") or \"\").strip()",
        "        if not execution_id:",
        "            return \"\", set(), 'execution marker has no execution_id'",
        "        ancestor_values = payload.get(\"ancestor_execution_ids\", [])",
        "        if not isinstance(ancestor_values, list) or not all(isinstance(value, str) and value.strip() for value in ancestor_values):",
        "            return \"\", set(), 'execution marker has invalid ancestor_execution_ids'",
        "        ancestors = {value.strip() for value in ancestor_values}",
        "        ancestors.discard(execution_id)",
        "        heartbeat_raw = payload.get(\"heartbeat_ts\")",
        "        if not isinstance(heartbeat_raw, str) or not heartbeat_raw.strip():",
        "            return \"\", set(), 'execution marker has no heartbeat_ts'",
        "        heartbeat_text = heartbeat_raw.strip()",
        "        if heartbeat_text.endswith(\"Z\"):",
        "            heartbeat_text = heartbeat_text[:-1] + \"+00:00\"",
        "        heartbeat = datetime.fromisoformat(heartbeat_text)",
        "        if heartbeat.tzinfo is None or heartbeat.tzinfo.utcoffset(heartbeat) is None:",
        "            return \"\", set(), 'execution marker heartbeat is not timezone-aware'",
        "        heartbeat = heartbeat.astimezone(timezone.utc)",
        "        max_age_raw = os.environ.get(\"AGENT_EXECUTION_MARKER_MAX_AGE_SECONDS\", \"1800\")",
        _SCRIPT_NESTED_TRY,
        "            max_age = max(60, int(max_age_raw))",
        "        except (TypeError, ValueError):",
        "            return \"\", set(), 'AGENT_EXECUTION_MARKER_MAX_AGE_SECONDS is invalid'",
        "        age_seconds = (datetime.now(timezone.utc) - heartbeat).total_seconds()",
        "        if age_seconds < -300 or age_seconds > max_age:",
        "            return \"\", set(), 'execution marker heartbeat is stale or in the future'",
        "        return execution_id, ancestors, None",
        "    except FileNotFoundError:",
        "        return \"\", set(), 'execution marker is missing'",
        _SCRIPT_INDENTED_EXCEPT,
        "        return \"\", set(), 'execution marker cannot be read'",
        "EXECUTION_ID, ANCESTOR_EXECUTION_IDS, EXECUTION_CONTEXT_ERROR = _current_execution_context()",
        "COMPATIBLE_EXECUTION_IDS = ({EXECUTION_ID} | ANCESTOR_EXECUTION_IDS) if EXECUTION_ID else set()",
        "EXECUTION_ENFORCEMENT = (os.environ.get(\"AGENT_EXECUTION_ENFORCEMENT_MODE\", \"observe\") or \"observe\").strip().lower()",
        "if EXECUTION_CONTEXT_ERROR and EXECUTION_CONTEXT_ERROR != 'execution marker is missing':",
        _SCRIPT_ENFORCEMENT_CHECK,
        "        sys.stderr.write(f\"[pre-push] blocked: {EXECUTION_CONTEXT_ERROR}. Start or resume an AgentExecution first.\\n\")",
        _SCRIPT_NESTED_EXIT_FAILURE,
        "    sys.stderr.write(f\"[pre-push] observe: {EXECUTION_CONTEXT_ERROR}; continuing without self-suppression.\\n\")",
        "if not EXECUTION_ID:",
        _SCRIPT_ENFORCEMENT_CHECK,
        "        sys.stderr.write(\"[pre-push] AGENT_EXECUTION_ENFORCEMENT_MODE=enforce requires an active execution marker.\\n\")",
        _SCRIPT_NESTED_EXIT_FAILURE,
        "    sys.stderr.write(\"[pre-push] observe: no active AgentExecution marker; legacy unscoped mode.\\n\")",
        "if not FILE_RESERVATIONS_DIR.exists():",
        _SCRIPT_INDENTED_EXIT_SUCCESS,
        "",
        "# Read tuples from STDIN: <local ref> <local sha> <remote ref> <remote sha>",
        "tuples = []",
        "for line in sys.stdin.read().splitlines():",
        "    parts = line.strip().split()",
        "    if len(parts) >= 4:",
        "        tuples.append((parts[0], parts[1], parts[2], parts[3]))",
        "",
        "changed = []",
        "commits = []",
        "for local_ref, local_sha, remote_ref, remote_sha in tuples:",
        "    if not local_sha:",
        "        continue",
        "    # Enumerate commits to be pushed using remote name from args (argv[1]) when available",
        "    remote = (sys.argv[1] if len(sys.argv) > 1 else \"origin\")",
        _SCRIPT_TRY,
        "        cp = subprocess.run([\"git\",\"rev-list\",\"--topo-order\",local_sha,\"--not\",f\"--remotes={remote}\"],",
        "                            check=True,capture_output=True,text=True)",
        "        for sha in cp.stdout.splitlines():",
        "            if sha:",
        "                commits.append(sha.strip())",
        _SCRIPT_INDENTED_EXCEPT,
        "        # Fallback: gather changed paths directly when range enumeration fails",
        "        rng = local_sha if (not remote_sha or set(remote_sha) == {\"0\"}) else f\"{remote_sha}..{local_sha}\"",
        _SCRIPT_NESTED_TRY,
        "            cp = subprocess.run([\"git\",\"diff\",\"--name-status\",\"-M\",\"-z\",rng],check=True,capture_output=True)",
        "            data = cp.stdout.decode(\"utf-8\",\"ignore\")",
        "            parts = [p for p in data.split(\"\\x00\") if p]",
        "            i = 0",
        "            while i < len(parts):",
        "                status = parts[i]",
        "                i += 1",
        "                if status.startswith(\"R\") and i + 1 < len(parts):",
        "                    oldp = parts[i]; newp = parts[i + 1]; i += 2",
        "                    if oldp: changed.append(oldp)",
        "                    if newp: changed.append(newp)",
        "                else:",
        "                    if i < len(parts):",
        "                        pth = parts[i]; i += 1",
        "                        if pth: changed.append(pth)",
        _SCRIPT_NESTED_EXCEPT,
        "            pass",
        "",
        "# changed already initialized above; add per-commit changed paths (capture renames)",
        "for c in commits:",
        _SCRIPT_TRY,
        "        cp = subprocess.run([\"git\",\"diff-tree\",\"-r\",\"--root\",\"--no-commit-id\",\"--name-status\",\"-M\",\"--no-ext-diff\",\"--diff-filter=ACMRDTU\",\"-z\",c],",
        "                            check=True,capture_output=True)",
        "        data = cp.stdout.decode(\"utf-8\",\"ignore\")",
        "        parts = [p for p in data.split(\"\\x00\") if p]",
        "        i = 0",
        "        while i < len(parts):",
        "            status = parts[i]",
        "            i += 1",
        "            if status.startswith(\"R\") and i + 1 < len(parts):",
        "                oldp = parts[i]; newp = parts[i + 1]; i += 2",
        "                if oldp: changed.append(oldp)",
        "                if newp: changed.append(newp)",
        "            else:",
        "                if i < len(parts):",
        "                    pth = parts[i]; i += 1",
        "                    if pth: changed.append(pth)",
        _SCRIPT_INDENTED_EXCEPT,
        "        continue",
        "",
        "# Local conflict detection against FILE_RESERVATIONS_DIR using changed paths",
        "if not changed:",
        _SCRIPT_INDENTED_EXIT_SUCCESS,
        "def _now_utc():",
        "    return datetime.now(timezone.utc)",
        "def _parse_iso(value):",
        "    if not value:",
        _SCRIPT_RETURN_NONE,
        _SCRIPT_TRY,
        "        text = value",
        "        if text.endswith(\"Z\"):",
        "            text = text[:-1] + \"+00:00\"",
        "        dt = datetime.fromisoformat(text)",
        "        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:",
        "            dt = dt.replace(tzinfo=timezone.utc)",
        "        return dt.astimezone(timezone.utc)",
        _SCRIPT_INDENTED_EXCEPT,
        _SCRIPT_RETURN_NONE,
        "def _not_expired(expires_ts):",
        "    parsed = _parse_iso(expires_ts)",
        "    if parsed is None:",
        "        return True",
        "    return parsed > _now_utc()",
        "# Honor core.ignorecase: case-fold paths and patterns before matching (#194).",
        "def _detect_ignorecase():",
        _SCRIPT_TRY,
        "        cp = subprocess.run([\"git\",\"config\",\"--type=bool\",\"--get\",\"core.ignorecase\"],",
        _SCRIPT_GIT_RUN_OPTIONS,
        "        return cp.stdout.strip() == \"true\"",
        _SCRIPT_INDENTED_EXCEPT,
        _SCRIPT_RETURN_FALSE,
        "IGNORECASE = _detect_ignorecase()",
        "def _casefold(value):",
        "    return value.lower() if IGNORECASE else value",
        "def _compile_one(patt):",
        "    q = _casefold(patt.replace(\"\\\\\",\"/\"))",
        "    if _PS:",
        _SCRIPT_NESTED_TRY,
        "            return _PS.from_lines(\"gitignore\", [q])",
        _SCRIPT_NESTED_EXCEPT,
        _SCRIPT_NESTED_RETURN_NONE,
        "    return None",
        "",
        "# Phase 1: Pre-load and compile all reservation patterns ONCE",
        "compiled_patterns = []",
        "legacy_self_notices = []",
        "all_pattern_strings = []",
        "seen_ids = set()",
        "try:",
        "    for f in FILE_RESERVATIONS_DIR.iterdir():",
        "        if not f.name.endswith('.json'):",
        _SCRIPT_CONTINUE,
        _SCRIPT_NESTED_TRY,
        "            data = json.loads(f.read_text(encoding='utf-8'))",
        _SCRIPT_NESTED_EXCEPT,
        _SCRIPT_CONTINUE,
        "        recs = data if isinstance(data, list) else [data]",
        "        for r in recs:",
        "            if not isinstance(r, dict):",
        _SCRIPT_NESTED_CONTINUE,
        "            rid = r.get('id')",
        "            if rid is not None:",
        "                rid_key = str(rid)",
        "                if rid_key in seen_ids:",
        "                    continue",
        "                seen_ids.add(rid_key)",
        "            patt = (r.get('path_pattern') or '').strip()",
        "            if not patt:",
        _SCRIPT_NESTED_CONTINUE,
        "            # Skip virtual namespace reservations (tool://, resource://, service://) — bd-14z",
        "            if any(patt.startswith(pfx) for pfx in ('tool://', 'resource://', 'service://')):",
        _SCRIPT_NESTED_CONTINUE,
        "            holder = (r.get('agent') or '').strip()",
        "            holder_execution = (r.get('execution_id') or '').strip()",
        "            exclusive = r.get('exclusive', True)",
        "            released = (r.get('released_ts') or '').strip()",
        "            expires = (r.get('expires_ts') or '').strip()",
        "            if not exclusive:",
        _SCRIPT_NESTED_CONTINUE,
        "            if released:",
        _SCRIPT_NESTED_CONTINUE,
        "            if not _not_expired(expires):",
        _SCRIPT_NESTED_CONTINUE,
        "            if holder == AGENT_NAME and holder_execution in COMPATIBLE_EXECUTION_IDS:",
        _SCRIPT_NESTED_CONTINUE,
        "            if holder == AGENT_NAME and not holder_execution and EXECUTION_ENFORCEMENT != 'enforce':",
        "                legacy_self_notices.append((patt, expires))",
        _SCRIPT_NESTED_CONTINUE,
        "            # Pre-compile pattern ONCE (not per-path)",
        "            spec = _compile_one(patt)",
        "            patt_norm = _casefold(patt.replace('\\\\','/').lstrip('/'))",
        "            compiled_patterns.append((spec, patt, patt_norm, holder, holder_execution))",
        "            all_pattern_strings.append(patt_norm)",
        _SCRIPT_EXCEPT,
        "    compiled_patterns = []",
        "    all_pattern_strings = []",
        "    legacy_self_notices = []",
        "if legacy_self_notices:",
        "    sys.stderr.write('[pre-push] observe: active legacy_unscoped claim(s) owned by this Agent; drain before enforce.\\n')",
        "    for patt, expires in legacy_self_notices[:10]:",
        "        sys.stderr.write(f'- legacy claim {patt} expires {expires or \"<unknown>\"}\\n')",
        "",
        "# Phase 2: Build union PathSpec for fast-path rejection",
        "union_spec = None",
        "if _PS and all_pattern_strings:",
        _SCRIPT_TRY,
        "        union_spec = _PS.from_lines(\"gitignore\", all_pattern_strings)",
        _SCRIPT_INDENTED_EXCEPT,
        "        union_spec = None",
        "",
        "# Phase 3: Check changed paths against compiled patterns",
        "conflicts = []",
        "if compiled_patterns:",
        "    for p in changed:",
        "        norm = _casefold(p.replace('\\\\','/').lstrip('/'))",
        "        # Fast-path: if union_spec exists and path doesn't match ANY pattern, skip",
        "        if union_spec is not None and not union_spec.match_file(norm):",
        _SCRIPT_CONTINUE,
        "        # Detailed matching for conflict attribution",
        "        for spec, patt, patt_norm, holder, holder_execution in compiled_patterns:",
        "            matched = spec.match_file(norm) if spec is not None else _fn.fnmatch(norm, patt_norm)",
        "            if matched:",
        "                conflicts.append((patt, p, holder, holder_execution))",
        "if conflicts:",
        "    sys.stderr.write(\"Exclusive file_reservation conflicts detected\\n\")",
        "    for patt, path, holder, holder_execution in conflicts[:10]:",
        "        execution_label = holder_execution or '<legacy-unscoped>'",
        "        sys.stderr.write(f\"- {path} matches {patt} (holder: {holder}, execution: {execution_label})\\n\")",
        "    if ADVISORY:",
        "        sys.exit(0)",
        _SCRIPT_INDENTED_EXIT_FAILURE,
        _SCRIPT_EXIT_SUCCESS,
    ]
    return "\n".join(lines) + "\n"


def _legacy_cmd_body(hook_name: str) -> bytes:
    """Return the exact historical Agent Mail cmd shim body with LF endings."""
    return (
        "@echo off\n"
        "setlocal\n"
        'set "DIR=%~dp0"\n'
        f'python "%DIR%{hook_name}" %*\n'
        "exit /b %ERRORLEVEL%\n"
    ).encode()


def _retired_cmd_body() -> bytes:
    """Return the inert marker used to retire an exact-owned legacy cmd shim."""
    return (
        "@echo off\n"
        "REM mcp-agent-mail disabled legacy cmd shim v1\n"
        "1>&2 echo [mcp-agent-mail] Legacy .cmd shim is disabled; use Git or the sibling PowerShell shim.\n"
        "exit /b 126\n"
    ).encode()


def _powershell_body(hook_name: str) -> bytes:
    """Return the owned PowerShell shim body with canonical LF endings."""
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$hook = Join-Path $PSScriptRoot '{hook_name}'\n"
        "python $hook @args\n"
        "exit $LASTEXITCODE\n"
    ).encode()


def _line_ending_variants(body: bytes, *, include_doubled_cr: bool = False) -> frozenset[bytes]:
    """Return exact whole-file variants produced by historical text writes."""
    variants = {body, body.replace(b"\n", b"\r\n")}
    if include_doubled_cr:
        variants.add(body.replace(b"\n", b"\r\r\n"))
    return frozenset(variants)


def _matches_exact_owned(path: Path, bodies: frozenset[bytes]) -> bool:
    """Return whether a regular, non-symlink file exactly matches an owned body."""
    if not bodies or path.is_symlink() or not path.is_file():
        return False
    owned_sizes = {len(body) for body in bodies}
    max_owned_size = max(owned_sizes)
    try:
        if path.stat().st_size not in owned_sizes:
            return False
        with path.open("rb") as handle:
            content = handle.read(max_owned_size + 1)
        return content in bodies
    except OSError:
        return False


def _retire_legacy_cmd(path: Path, hook_name: str) -> None:
    """Overwrite only an exact historical cmd shim with an inert marker."""
    legacy_bodies = _line_ending_variants(
        _legacy_cmd_body(hook_name),
        include_doubled_cr=True,
    )
    if _matches_exact_owned(path, legacy_bodies):
        retired_crlf = _retired_cmd_body().replace(b"\n", b"\r\n")
        path.write_bytes(retired_crlf)


async def install_guard(settings: Settings, project_slug: str, repo_path: Path) -> Path:
    """Install the pre-commit chain-runner and Agent Mail guard plugin."""

    archive = await ensure_archive(settings, project_slug)

    hooks_dir = _resolve_hooks_dir(repo_path)
    if not hooks_dir.exists():
        await asyncio.to_thread(hooks_dir.mkdir, parents=True, exist_ok=True)

    # Ensure hooks.d/pre-commit exists
    run_dir = hooks_dir / _HOOKS_DIRECTORY / "pre-commit"
    await asyncio.to_thread(run_dir.mkdir, parents=True, exist_ok=True)

    chain_path = hooks_dir / "pre-commit"
    # Preserve an existing non-chain hook, but never overwrite a different
    # original left by an earlier installation.
    await _preserve_foreign_hook(
        chain_path,
        "mcp-agent-mail chain-runner (pre-commit)",
    )
    # Write/overwrite chain-runner
    chain_script = _render_chain_runner_script("pre-commit")
    await asyncio.to_thread(chain_path.write_text, chain_script, "utf-8")
    await asyncio.to_thread(os.chmod, chain_path, 0o755)

    # Git invokes the extensionless chain-runner directly. Historical .cmd
    # wrappers expanded %* through cmd.exe and cannot preserve arbitrary Git
    # hook arguments safely, so exact-owned copies are retired in place.
    cmd_path = hooks_dir / "pre-commit.cmd"
    await asyncio.to_thread(_retire_legacy_cmd, cmd_path, "pre-commit")
    ps1_path = hooks_dir / "pre-commit.ps1"
    if not ps1_path.exists():
        await asyncio.to_thread(ps1_path.write_bytes, _powershell_body("pre-commit"))

    # Write our guard plugin
    plugin_path = run_dir / _GUARD_PLUGIN_NAME
    plugin_script = render_precommit_script(archive)
    await asyncio.to_thread(plugin_path.write_text, plugin_script, "utf-8")
    await asyncio.to_thread(os.chmod, plugin_path, 0o755)
    return chain_path


async def install_prepush_guard(settings: Settings, project_slug: str, repo_path: Path) -> Path:
    """Install the pre-push chain-runner and Agent Mail guard plugin."""
    archive = await ensure_archive(settings, project_slug)

    hooks_dir = _resolve_hooks_dir(repo_path)
    await asyncio.to_thread(hooks_dir.mkdir, parents=True, exist_ok=True)
    # Ensure hooks.d/pre-push exists
    run_dir = hooks_dir / _HOOKS_DIRECTORY / "pre-push"
    await asyncio.to_thread(run_dir.mkdir, parents=True, exist_ok=True)

    chain_path = hooks_dir / "pre-push"
    await _preserve_foreign_hook(
        chain_path,
        "mcp-agent-mail chain-runner (pre-push)",
    )
    chain_script = _render_chain_runner_script("pre-push")
    await asyncio.to_thread(chain_path.write_text, chain_script, "utf-8")
    await asyncio.to_thread(os.chmod, chain_path, 0o755)

    # See install_guard: never create a new cmd wrapper, and retire only the
    # exact historical Agent Mail template without touching foreign files.
    cmd_path = hooks_dir / "pre-push.cmd"
    await asyncio.to_thread(_retire_legacy_cmd, cmd_path, "pre-push")
    ps1_path = hooks_dir / "pre-push.ps1"
    if not ps1_path.exists():
        await asyncio.to_thread(ps1_path.write_bytes, _powershell_body("pre-push"))

    plugin_path = run_dir / _GUARD_PLUGIN_NAME
    plugin_script = render_prepush_script(archive)
    await asyncio.to_thread(plugin_path.write_text, plugin_script, "utf-8")
    await asyncio.to_thread(os.chmod, plugin_path, 0o755)
    return chain_path


def _has_other_plugins(run_dir: Path) -> bool:
    """Return whether a hook directory contains a plugin other than ours."""
    if not run_dir.exists() or not run_dir.is_dir():
        return False
    return any(item.is_file() and item.name != _GUARD_PLUGIN_NAME for item in run_dir.iterdir())


def _agent_mail_shims(hooks_dir: Path, hook_name: str) -> list[Path]:
    """Return exact-owned Agent Mail shim paths for a hook."""
    cmd_bodies = _line_ending_variants(
        _legacy_cmd_body(hook_name),
        include_doubled_cr=True,
    ) | _line_ending_variants(
        _retired_cmd_body(),
        include_doubled_cr=True,
    )
    ps1_bodies = _line_ending_variants(_powershell_body(hook_name))
    shim_bodies = {
        hooks_dir / f"{hook_name}.cmd": cmd_bodies,
        hooks_dir / f"{hook_name}.ps1": ps1_bodies,
    }
    matches: list[Path] = []
    for shim_path, owned_bodies in shim_bodies.items():
        if _matches_exact_owned(shim_path, owned_bodies):
            matches.append(shim_path)
    return matches


async def _remove_agent_mail_shims(hooks_dir: Path, hook_name: str) -> None:
    shim_paths = await asyncio.to_thread(_agent_mail_shims, hooks_dir, hook_name)
    for shim_path in shim_paths:
        await asyncio.to_thread(shim_path.unlink)


async def _remove_guard_plugin(hooks_dir: Path, hook_name: str) -> bool:
    plugin_path = hooks_dir / _HOOKS_DIRECTORY / hook_name / _GUARD_PLUGIN_NAME
    if not plugin_path.exists():
        return False
    await asyncio.to_thread(plugin_path.unlink)
    return True


async def _read_hook_text(hook_path: Path) -> str:
    try:
        return (await asyncio.to_thread(hook_path.read_text, "utf-8")).strip()
    except Exception:
        return ""


async def _remove_chain_runner(hooks_dir: Path, hook_name: str, hook_path: Path) -> bool:
    run_dir = hooks_dir / _HOOKS_DIRECTORY / hook_name
    orig_path = hooks_dir / f"{hook_name}.orig"
    if _has_other_plugins(run_dir):
        return False

    restore_original = orig_path.exists()
    await asyncio.to_thread(hook_path.unlink)
    if restore_original:
        await asyncio.to_thread(orig_path.replace, hook_path)
    await _remove_agent_mail_shims(hooks_dir, hook_name)
    return True


async def _remove_top_level_hook(hooks_dir: Path, hook_name: str) -> bool:
    hook_path = hooks_dir / hook_name
    if not hook_path.exists():
        return False

    content = await _read_hook_text(hook_path)
    if _CHAIN_RUNNER_MARKER in content:
        return await _remove_chain_runner(hooks_dir, hook_name, hook_path)
    if any(sentinel in content for sentinel in _LEGACY_HOOK_SENTINELS):
        await asyncio.to_thread(hook_path.unlink)
        await _remove_agent_mail_shims(hooks_dir, hook_name)
        return True
    return False


async def uninstall_guard(repo_path: Path) -> bool:
    """Remove Agent Mail guard plugin(s) from repo, returning True if any were removed.

    - Removes hooks.d/<hook>/50-agent-mail.py if present.
    - Legacy fallback: removes top-level pre-commit/pre-push only if they are old-style
      Agent Mail hooks (sentinel present) and not chain-runners.
    """

    hooks_dir = _resolve_hooks_dir(repo_path)
    removed = False

    for hook_name in ("pre-commit", "pre-push"):
        plugin_removed = await _remove_guard_plugin(hooks_dir, hook_name)
        removed = plugin_removed or removed

    for hook_name in ("pre-commit", "pre-push"):
        hook_removed = await _remove_top_level_hook(hooks_dir, hook_name)
        removed = hook_removed or removed

    return removed
