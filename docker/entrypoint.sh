#!/bin/sh
# CountVision container entry point.
#   run      count all cameras of /config/config.yaml (default)
#   app      local web app on port 8000 (draw lines; stop "run" first, see docker/README.md)
#   health   health check (used by Docker)
#   anything else is passed to countvision-edge, e.g. "demo", "report --db /data/countvision.db"
set -e
CONFIG="${CV_CONFIG:-/config/config.yaml}"

check_writable() {
  if ! touch "$1/.write-test" 2>/dev/null; then
    echo "ERROR: $1 is not writable for the container user (uid $(id -u))." >&2
    echo "On Linux run once:  sudo chown -R 10001 <your $1 folder>" >&2
    exit 78
  fi
  rm -f "$1/.write-test"
}

need_config() {
  if [ ! -f "$CONFIG" ]; then
    check_writable "$(dirname "$CONFIG")"
    cp /app/configs/site.example.yaml "$CONFIG"
    echo "No config found. Created $CONFIG from the example." >&2
    echo "Edit it (camera addresses in .env, lines) and restart the container." >&2
  fi
}

case "${1:-run}" in
  run)
    [ $# -gt 0 ] && shift
    need_config
    check_writable "${CV_DATA_DIR:-/data}"
    exec countvision-edge run --config "$CONFIG" "$@"
    ;;
  app)
    shift
    need_config
    check_writable "${CV_DATA_DIR:-/data}"
    exec countvision-edge app --config "$CONFIG" --host 0.0.0.0 --no-browser "$@"
    ;;
  health)
    exec countvision-edge health --data-dir "${CV_DATA_DIR:-/data}" --max-age 90
    ;;
  *)
    exec countvision-edge "$@"
    ;;
esac
