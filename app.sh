#!/usr/bin/env bash
# Control script for the Stock Options Dashboard.
# Usage: ./app.sh <start|stop|restart|status|logs> [public|bots]
#
# Two apps live here: `public` (app.py, what gets deployed) and `bots`
# (bots_app.py, local only). They get their own port, pidfile and log so both
# can run at once — useful for checking that a change looks right in the app
# that strangers see, not just the one with the bot tab.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${2:-public}"
case "$APP" in
  public) ENTRY="app.py";      DEFAULT_PORT=8555 ;;
  bots)   ENTRY="bots_app.py"; DEFAULT_PORT=8556 ;;
  *) echo "Unknown app '$APP' — expected 'public' or 'bots'." >&2; exit 1 ;;
esac
PORT="${PORT:-$DEFAULT_PORT}"
PIDFILE="$DIR/.app-$APP.pid"
LOGFILE="$DIR/app-$APP.log"
STREAMLIT="$DIR/.venv/bin/streamlit"

running() {
  [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

start() {
  if running; then
    echo "Already running $APP (PID $(cat "$PIDFILE")) → http://localhost:$PORT"
    return 0
  fi
  [[ -x "$STREAMLIT" ]] || { echo "streamlit not found at $STREAMLIT — is .venv set up?" >&2; exit 1; }

  cd "$DIR"
  nohup "$STREAMLIT" run "$ENTRY" \
    --server.headless true \
    --server.port "$PORT" \
    --browser.gatherUsageStats false \
    > "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"

  sleep 2
  if running; then
    echo "Started $APP ($ENTRY, PID $(cat "$PIDFILE")) → http://localhost:$PORT"
    echo "Logs: $LOGFILE"
  else
    echo "Failed to start. Last lines of $LOGFILE:" >&2
    tail -20 "$LOGFILE" >&2
    rm -f "$PIDFILE"
    exit 1
  fi
}

stop() {
  if running; then
    PID="$(cat "$PIDFILE")"
    kill "$PID"
    for _ in {1..10}; do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.5
    done
    kill -9 "$PID" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "Stopped (PID $PID)"
  else
    rm -f "$PIDFILE"
    # Fall back to anything still holding the port.
    STRAY="$(pgrep -f "streamlit run $ENTRY.*--server.port $PORT" || true)"
    if [[ -n "$STRAY" ]]; then
      echo "$STRAY" | xargs kill
      echo "Stopped stray process(es): $STRAY"
    else
      echo "Not running ($APP)"
    fi
  fi
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)
    if running; then
      echo "Running $APP ($ENTRY, PID $(cat "$PIDFILE")) → http://localhost:$PORT"
    else
      echo "Not running ($APP)"
    fi
    ;;
  logs)    tail -f "$LOGFILE" ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs} [public|bots]" >&2
    exit 1
    ;;
esac
