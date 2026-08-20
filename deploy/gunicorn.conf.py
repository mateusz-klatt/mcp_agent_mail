"""Sample gunicorn configuration for MCP Agent Mail."""

from pathlib import Path

# Bind to same interface/port as default settings; override via GUNICORN_CMD_ARGS if needed.
bind = "0.0.0.0:8765"

# EXACTLY ONE worker. Not a conservative default — a correctness requirement.
#
# Four pieces of state this server depends on live in the memory of the process
# that created them, and none of them is shared or reconstructible:
#
#   app.py:5063   session_agent_bindings — which agent a stateful MCP session is
#                 speaking as. Bound on one worker, absent on the next: the agent
#                 is told to register again, mid-session.
#   notify.py:1   the in-process fan-out behind /events. A hint published on the
#                 worker that received the message never reaches an SSE client
#                 parked on any other worker, so the watcher silently degrades to
#                 the polling it exists to replace.
#   http.py:835   the login brute-force throttle, whose own comment says it is
#                 correct "precisely because this server must run as a single
#                 process anyway — there is no second worker to share it with".
#                 With N workers the cap is 8 failures per window per worker.
#   http.py:582   the JWKS cache, refetched once per worker rather than once.
#
# The previous value here was multiprocessing.cpu_count() * 2 + 1, the generic
# gunicorn recipe for a stateless WSGI app. On a 16-core host that is 33 workers:
# a session binding survives roughly one request in thirty-three, and the login
# throttle admits 264 attempts per five minutes instead of 8. Both failures are
# intermittent and neither logs anything, which is the worst shape for a bug that
# only appears under the load that justified adding workers in the first place.
#
# README.md points operators at this file by name, so the number here is the one
# that ships. Scale by putting more instances behind the proxy, each with its own
# store — not by adding workers to one.
workers = 1
worker_class = "uvicorn_worker.UvicornWorker"

# Graceful timeouts to match long running tasks.
keepalive = 5
graceful_timeout = 60
timeout = 120

# Location for PID/log files (customize as desired).
pidfile = str(Path("/var/run/mcp-agent-mail/gunicorn.pid"))
errorlog = "-"  # stderr
# The default Gunicorn request line includes the full query string. OAuth
# callbacks carry a short-lived authorization code and state there, so rely on
# the application's redacted request logger instead.
accesslog = None
loglevel = "info"

# Optional: forward standard proxy headers.
forwarded_allow_ips = "*"
