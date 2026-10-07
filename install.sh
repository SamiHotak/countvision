#!/usr/bin/env bash
# CountVision edge agent - one-line installer for a Linux mini-PC (Ubuntu/Debian, x86-64 or ARM64).
#
#   curl -fsSL https://raw.githubusercontent.com/SamiHotak/countvision/main/install.sh | sudo bash
#
# What it does (run it again to update; your settings and numbers are kept):
#   1. installs Docker if it is missing (official script from get.docker.com)
#   2. downloads CountVision to /opt/countvision/app
#   3. asks for the camera addresses (or uses two test cameras) -> /opt/countvision/.env
#   4. builds the image and starts the counting service (starts again after a reboot)
#   5. installs the "countvision" command: countvision status | logs | setup | stop | start | update
#
# Options (environment variables): CV_DIR=/opt/countvision  CV_BRANCH=main  CV_TEST=1 (test cameras)
#   CV_SOURCE=<local checkout> (developers)
#   CV_GPU=auto|nvidia|cpu  CV_YES=1 (no questions)  DRY_RUN=1 (only print what would be done)
set -euo pipefail

REPO="${CV_REPO:-SamiHotak/countvision}"
BRANCH="${CV_BRANCH:-main}"
DIR="${CV_DIR:-/opt/countvision}"
GPU="${CV_GPU:-auto}"
DRY_RUN="${DRY_RUN:-0}"
CV_UID=10001
SAMPLES="https://raw.githubusercontent.com/intel-iot-devkit/sample-videos/master"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
run()  { echo "+ $*"; if [ "$DRY_RUN" != 1 ]; then "$@"; fi; }

ask() {  # ask "question" default -> answer (from the terminal even when piped from curl)
  local answer=""
  if [ "${CV_YES:-0}" = 1 ] || [ ! -r /dev/tty ]; then echo "$2"; return; fi
  printf '%s ' "$1" > /dev/tty
  read -r answer < /dev/tty || true
  echo "${answer:-$2}"
}

# ------------------------------------------------------------------ checks
[ "$(uname -s)" = Linux ] || die "This installer is for Linux. On Windows use install.ps1 (see README)."
if [ "$DRY_RUN" != 1 ] && [ "$(id -u)" -ne 0 ]; then
  die "Run it with sudo:  curl -fsSL https://raw.githubusercontent.com/$REPO/$BRANCH/install.sh | sudo bash"
fi
ARCH="$(uname -m)"
case "$ARCH" in x86_64|amd64|aarch64|arm64) ;; *) die "Unsupported CPU type: $ARCH (needs x86-64 or ARM64).";; esac
command -v curl >/dev/null || die "curl is missing: sudo apt-get install -y curl"

say "CountVision installer: $DIR (code: github.com/$REPO, branch $BRANCH)"

# ------------------------------------------------------------------ 1. Docker
if ! command -v docker >/dev/null 2>&1; then
  answer="$(ask "Docker is not installed. Install it now from get.docker.com? [Y/n]" y)"
  case "$answer" in n|N|no) die "CountVision needs Docker. Install it and run this again.";; esac
  run sh -c "curl -fsSL https://get.docker.com | sh"
fi
if [ "$DRY_RUN" != 1 ]; then
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing (docker compose version). Update Docker."
  run systemctl enable --now docker >/dev/null 2>&1 || true
fi

# GPU: NVIDIA card + NVIDIA Container Toolkit -> GPU image. Jetson: CPU image (GPU image untested).
SERVICE=countvision
PROFILE=""
if [ "$GPU" = auto ]; then
  if [ -f /etc/nv_tegra_release ]; then
    warn "NVIDIA Jetson found. The Jetson GPU image is not tested yet; using the CPU image."
    GPU=cpu
  elif command -v nvidia-smi >/dev/null 2>&1 && docker info 2>/dev/null | grep -qi nvidia; then
    GPU=nvidia
  else
    GPU=cpu
  fi
fi
if [ "$GPU" = nvidia ]; then SERVICE=countvision-gpu; PROFILE="nvidia"; fi
say "Detector runs on: $GPU (service $SERVICE)"

# ------------------------------------------------------------------ 2. code
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
say "Downloading CountVision ..."
if [ -n "${CV_SOURCE:-}" ]; then   # developers: install from a local checkout instead of GitHub
  run cp -a "$CV_SOURCE" "$TMP/countvision-src"
else
  run sh -c "curl -fsSL https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH | tar -xz -C '$TMP'"
fi
run mkdir -p "$DIR/config" "$DIR/data" "$DIR/videos"
if [ "$DRY_RUN" != 1 ]; then
  SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  [ -f "$SRC/docker/compose.yaml" ] || die "Download looks wrong (no docker/compose.yaml)."
  rm -rf "$DIR/app.old"
  if [ -d "$DIR/app" ]; then mv "$DIR/app" "$DIR/app.old"; fi
  mv "$SRC" "$DIR/app"
  rm -rf "$DIR/app.old"
fi

# ------------------------------------------------------------------ 3. settings (.env)
ENV_FILE="$DIR/.env"
USES_TEST=0
if grep -q "test-camera" "$ENV_FILE" 2>/dev/null; then USES_TEST=1; fi
if [ -f "$ENV_FILE" ] && [ "${CV_TEST:-0}" != 1 ]; then
  say "Keeping your settings in $ENV_FILE"
else
  CAM1=""
  if [ "${CV_TEST:-0}" != 1 ]; then
    echo
    echo "Camera address (RTSP), e.g. rtsp://user:password@192.168.1.20:554/stream2"
    echo "Leave it empty to start with two TEST cameras (looping videos) and add real ones later."
    CAM1="$(ask "Camera 1:" "")"
  fi
  if [ -z "$CAM1" ]; then
    say "Using the test cameras."
    USES_TEST=1
    TEST_LINES=$'CV_CONFIG=/test/config.yaml\nCAM1_URL=rtsp://test-camera:8554/cam1\nCAM2_URL=rtsp://test-camera:8554/cam2'
  else
    CAM2="$(ask "Camera 2 (empty = only one camera):" "")"
    USES_TEST=0
    TEST_LINES="CAM1_URL=$CAM1"
    if [ -n "$CAM2" ]; then TEST_LINES="$TEST_LINES"$'\n'"CAM2_URL=$CAM2"; fi
  fi
  if [ "$DRY_RUN" != 1 ]; then
    umask 077
    cat > "$ENV_FILE" <<EOF
# CountVision settings. Camera passwords stay in this file (readable by root only).
# After a change: countvision restart
$TEST_LINES
TZ=${TZ:-Europe/Berlin}
EOF
    umask 022
  else
    echo "+ write $ENV_FILE"
  fi
fi
# Which extra services run: the test cameras (if used) and the GPU service.
PROFILES="$PROFILE"
if [ "$USES_TEST" = 1 ]; then PROFILES="${PROFILES:+$PROFILES,}test"; fi
# Paths for docker compose (not secrets): kept in a separate file so updates can rewrite them.
if [ "$DRY_RUN" != 1 ]; then
  cat > "$DIR/compose.env" <<EOF
COMPOSE_PROFILES=$PROFILES
CV_ENV_FILE=$ENV_FILE
CV_CONFIG_HOST=$DIR/config
CV_DATA_HOST=$DIR/data
CV_VIDEOS_HOST=$DIR/videos
EOF
fi

# Test videos for the test cameras (only if they are used)
if [ "$USES_TEST" = 1 ]; then
  for v in people-detection one-by-one-person-detection; do
    [ -f "$DIR/videos/$v.mp4" ] || run curl -fsSL -o "$DIR/videos/$v.mp4" "$SAMPLES/$v.mp4"
  done
fi
# A single real camera: the example config has two cameras, the second one is removed.
if [ -f "$ENV_FILE" ] && ! grep -q "CAM2_URL" "$ENV_FILE" && [ ! -f "$DIR/config/config.yaml" ] && [ "$DRY_RUN" != 1 ]; then
  sed '/^  - id: counter/,$d' "$DIR/app/edge/configs/site.example.yaml" > "$DIR/config/config.yaml"
fi
run chown -R "$CV_UID" "$DIR/config" "$DIR/data"

# ------------------------------------------------------------------ 4. the "countvision" command
CMD=/usr/local/bin/countvision
if [ "$DRY_RUN" != 1 ]; then
  cat > "$CMD" <<EOF
#!/usr/bin/env bash
# CountVision service command (made by install.sh).
set -e
DIR="$DIR"
SERVICE="$SERVICE"
dc() { docker compose --project-directory "\$DIR/app/docker" --env-file "\$DIR/compose.env" --env-file "\$DIR/.env" -f "\$DIR/app/docker/compose.yaml" "\$@"; }
case "\${1:-status}" in
  status)  dc ps; echo; dc exec -T "\$SERVICE" countvision-entrypoint health ;;
  logs)    dc logs -f --tail 100 "\$SERVICE" ;;
  report)  dc exec -T "\$SERVICE" countvision-edge report --db /data/countvision.db ;;
  start)   dc up -d "\$SERVICE" ;;
  stop)    dc stop "\$SERVICE" ;;
  restart) dc up -d --force-recreate "\$SERVICE" ;;
  setup)   shift; echo "Counting is paused while you draw lines (Ctrl+C when done)."
           dc stop "\$SERVICE"; dc run --rm --service-ports setup app "\$@" || true; dc up -d "\$SERVICE" ;;
  update)  curl -fsSL https://raw.githubusercontent.com/$REPO/$BRANCH/install.sh | CV_YES=1 bash ;;
  config)  \${EDITOR:-nano} "\$DIR/config/config.yaml" ;;
  env)     \${EDITOR:-nano} "\$DIR/.env" ;;
  *)       dc "\$@" ;;
esac
EOF
  chmod 755 "$CMD"
else
  echo "+ write $CMD"
fi

# ------------------------------------------------------------------ 5. build and start
say "Building the image (first time 3-10 minutes) and starting ..."
START="$SERVICE"
if [ "$USES_TEST" = 1 ]; then START="test-camera $SERVICE"; fi
if [ "$DRY_RUN" != 1 ]; then
  "$CMD" build "$SERVICE"
  # shellcheck disable=SC2086  # START is a list of service names
  "$CMD" up -d $START
else
  echo "+ countvision build $SERVICE && countvision up -d $START"
fi

echo
say "Done. CountVision is counting and starts again after a reboot."
cat <<EOF
Use sudo (or add your user to the "docker" group):
  countvision status     is it running? (healthy after 1-2 minutes)
  countvision logs       live log (Ctrl+C to leave)
  countvision setup      draw lines in the browser: http://<this PC's IP>:8000/?key=...
  countvision config     edit cameras, lines, zones  ($DIR/config/config.yaml)
  countvision env        edit camera addresses/passwords ($DIR/.env)
  countvision report     the numbers so far
  countvision update     newest version (settings and numbers are kept)
EOF
