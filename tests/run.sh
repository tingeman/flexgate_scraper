#!/bin/bash
# Behaviour tests of the flexgate_scraper image. Run on the workstation with the docker CLI
# against a built image, from the repository root:
#
#   bash tests/run.sh flexgate_scraper:local
#
# Works under bash on Linux and WSL, and from Git Bash on Windows (Docker Desktop).
# Every container runs with --network none, except the download tests: those run the
# scraper against a local fake FlexGate server (tests/fake_flexgate.py) on a private
# --internal docker network, so nothing leaves the workstation. All containers have names
# starting with a unique prefix and are removed at the end, together with the network and
# the temporary directories (files that containers created there as root are deleted from
# inside a container). Nothing else is touched.
# Each check prints "PASS: <name>" or "FAIL: <name>: <reason>". The last line is
# "ALL <n> TESTS PASSED" (exit 0) or "<m> OF <n> TESTS FAILED" (exit 1).
# shellcheck disable=SC2329  # test functions are called indirectly through run_test
set -euo pipefail

IMAGE="${1:-}"
[ -n "$IMAGE" ] && [ $# -eq 1 ] || { echo "Usage: $0 <image>" >&2; exit 2; }
command -v docker >/dev/null || { echo "docker not found" >&2; exit 2; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image not found: $IMAGE" >&2; exit 2; }

TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$TESTS_DIR/fake_flexgate.py" ] || { echo "missing $TESTS_DIR/fake_flexgate.py" >&2; exit 2; }

# Git Bash: keep container paths (/app/...) as they are, and give docker Windows paths for
# the host side of bind mounts (see hp below).
export MSYS_NO_PATHCONV=1

PFX="flexgate-test-$$"
NET="$PFX-net"
T="$(mktemp -d "${TMPDIR:-/tmp}/flexgate-test.XXXXXX")"

# Host path for a bind mount: unchanged on Linux/WSL, C:/... form under Git Bash
hp() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s\n' "$1"; fi; }

cleanup() {
  local rc=$?
  trap - EXIT
  local ids
  ids="$(docker ps -aq --filter "name=${PFX}-" 2>/dev/null || true)"
  # shellcheck disable=SC2086  # word splitting of the id list is intended
  [ -z "$ids" ] || docker rm -f $ids >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  if [ -d "$T" ]; then
    # containers wrote as root: delete from inside a container, then remove what is left
    docker run --rm --network none --entrypoint sh -v "$(hp "$T"):/t" "$IMAGE" -c 'find /t -mindepth 1 -delete' >/dev/null 2>&1 || true
    rm -rf "$T" 2>/dev/null || echo "WARNING: could not remove $T" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ---- helpers -------------------------------------------------------------------------

TESTNAME=""
fail_t() { echo "$*"; exit 1; }                       # fail the current test (runs in a subshell)
expect() { [ "$2" = "$3" ] || fail_t "$1: expected [$3], got [$2]"; }
contains() { case "$2" in *"$3"*) ;; *) fail_t "$1: output does not contain [$3]: $2" ;; esac; }
lacks() { case "$2" in *"$3"*) fail_t "$1: output contains [$3]: $2" ;; esac; }
cname() { echo "$PFX-$TESTNAME${1:+-$1}"; }
sorted() { LC_ALL=C sort; }

# Plain container for image-content checks
plain() { docker run --rm --network none --entrypoint sh "$IMAGE" -c "$1"; }

# Entries under a directory as "<type> <path>", listed by a container (files written by
# containers are owned by root).
listing() {
  docker run --rm --network none --entrypoint find -v "$(hp "$1"):/t:ro" -w /t "$IMAGE" . -mindepth 1 -printf '%y %P\n' | sorted
}

wait_until() {   # wait_until <tries> <cmd...>: run cmd every 0.5 s until it succeeds
  local n=$1; shift
  while [ "$n" -gt 0 ]; do "$@" >/dev/null 2>&1 && return 0; sleep 0.5; n=$((n - 1)); done
  return 1
}

PASSED=0 FAILED=0
run_test() {   # run_test <name> <function> [args...]
  local name="$1" fn="$2"; shift 2
  local out rc=0
  TESTNAME="$(printf '%s' "$name" | tr -c 'A-Za-z0-9\n' '-' | cut -c1-40)"
  out="$($fn "$@" 2>&1)" || rc=$?
  if [ "$rc" -eq 0 ]; then
    PASSED=$((PASSED + 1)); echo "PASS: $name"
  else
    FAILED=$((FAILED + 1)); echo "FAIL: $name: $(printf '%s' "$out" | head -n 12 | sed '2,$s/^/    /')"
  fi
}

# Fresh environment directory $E with a settings file and a cron job directory
mkenv() {
  E="$T/$1"
  mkdir -p "$E/cron"
  printf '%s\n' 'flexgate_browse_url: "http://flexgate:8080/fg2lite/sw/browse.php"' \
    'base_download_folder: "/app/downloads"' 'downloadable_extensions: [".f2p"]' \
    'sources:' '  - serial: "A0001A"' '    folders: raw' > "$E/settings.yml"
  printf '%s\n' 'SHELL=/bin/sh' '20 * * * * root /usr/local/bin/python /app/flexgate_scraper.py > /proc/1/fd/1 2>&1' > "$E/cron/cron_test"
}

# ---- image contents ------------------------------------------------------------------

t_has()   { plain "command -v $1" >/dev/null || fail_t "$1 is missing"; }
t_lacks() { ! plain "command -v $1" >/dev/null 2>&1 || fail_t "$1 is present"; }

t_pip_check() {
  docker run --rm --network none --entrypoint python "$IMAGE" -m pip check 2>&1 || fail_t "pip check failed"
}

t_python_imports() {
  docker run --rm --network none --entrypoint python "$IMAGE" -c \
    'import requests, bs4, pydantic, pydantic_settings, yaml' 2>&1 || fail_t "import failed"
}

t_syntax() {
  plain "bash -n /entrypoint.sh" 2>&1 || fail_t "syntax error in /entrypoint.sh"
  plain "test -x /entrypoint.sh" || fail_t "/entrypoint.sh is not executable"
  docker run --rm --network none --entrypoint python "$IMAGE" -c \
    'import ast; ast.parse(open("/app/flexgate_scraper.py").read())' 2>&1 || fail_t "syntax error in flexgate_scraper.py"
}

# ---- entrypoint ----------------------------------------------------------------------

t_entry_ok() {
  mkenv entry_ok
  local name running mode cronfile
  name="$(cname)"
  docker run -d --network none --name "$name" -v "$(hp "$E/settings.yml"):/app/settings.yml:ro" \
    -v "$(hp "$E/cron"):/app/cron_jobs:ro" "$IMAGE" >/dev/null || fail_t "could not start"
  wait_until 40 sh -c "docker logs '$name' 2>&1 | grep -q 'cron.d/ updated'" || fail_t "entrypoint did not finish: $(docker logs "$name" 2>&1 | tail -n 5)"
  sleep 2
  running="$(docker inspect -f '{{.State.Running}}' "$name")"
  mode="$(docker exec "$name" stat -c '%a %U' /etc/cron.d/cron_test)"
  cronfile="$(docker exec "$name" cat /etc/cron.d/cron_test)"
  docker rm -f "$name" >/dev/null
  expect "container running" "$running" "true"
  expect "cron file mode and owner" "$mode" "644 root"
  contains "cron file" "$cronfile" "/app/flexgate_scraper.py"
}

# Entrypoint that must refuse to start: exit non-zero with the settings error, no cron file
# installed. Extra arguments go to docker run before the image (e.g. --entrypoint).
entry_must_fail() {
  local name out rc=0 changes
  name="$(cname)"
  out="$(docker run --network none --name "$name" -v "$(hp "$E/cron"):/app/cron_jobs:ro" "$@" 2>&1)" || rc=$?
  changes="$(docker diff "$name" || true)"
  docker rm -f "$name" >/dev/null
  [ "$rc" -ne 0 ] || fail_t "exit code 0, expected non-zero: $out"
  case "$changes" in *"/etc/cron.d/cron_test"*) fail_t "a cron file was installed" ;; esac
  contains "log" "$out" "/app/settings.yml is missing or empty"
}

t_entry_empty_settings() {
  mkenv entry_empty
  : > "$E/settings.yml"
  entry_must_fail -v "$(hp "$E/settings.yml"):/app/settings.yml:ro" "$IMAGE"
}

# The image ships a template app/settings.yml, so "missing" is tested by removing it first
t_entry_missing_settings() {
  mkenv entry_missing
  entry_must_fail --entrypoint bash "$IMAGE" -c 'rm -f /app/settings.yml && exec /entrypoint.sh'
}

# ---- downloads against a fake FlexGate server ----------------------------------------
# One server and one download directory shared by the download tests, which run in order.

FG_OK=0 DL_E=""

fg_setup() {
  TESTNAME="fakefg"
  DL_E="$T/download"
  local d="$DL_E/srv/fg2lite/data"
  mkdir -p "$DL_E/srv/fg2lite/sw" "$d/A0001A/raw" "$d/A0001A/par" "$d/B0002B/raw" "$DL_E/downloads" "$DL_E/srv_code"
  cp "$TESTS_DIR/fake_flexgate.py" "$DL_E/srv_code/"
  printf 'log\n' > "$d/A0001A/log.txt"
  printf 'state\n' > "$d/A0001A/state.dat"
  printf 'not listed\n' > "$d/A0001A/other.txt"
  printf 'raw one\n' > "$d/A0001A/raw/x1.f2p"
  printf 'raw two\n' > "$d/A0001A/raw/x2.f2b"
  printf 'a,b\n' > "$d/A0001A/raw/notes.csv"
  printf 'par\n' > "$d/A0001A/par/p.par"
  printf 'other logger\n' > "$d/B0002B/raw/y1.f2p"
  cat > "$DL_E/settings.yml" <<'YML'
flexgate_browse_url: "http://flexgate:8080/fg2lite/sw/browse.php"
base_download_folder: "/app/downloads"
downloadable_extensions:
  - ".f2p"
  - ".f2b"
  - ".par"
sources:
  - serial: "A0001A"
    logger_name: "SITE-01"
    folders:
      - raw
      - par
    files:
      - "log.txt"
      - "state.dat"
  - serial: "B0002B"
    folders: raw
YML
  docker network create --internal "$NET" >/dev/null 2>&1 || return 0
  docker run -d --network "$NET" --network-alias flexgate --name "$(cname srv)" \
    -v "$(hp "$DL_E/srv"):/srv:ro" -v "$(hp "$DL_E/srv_code"):/code:ro" \
    --entrypoint python "$IMAGE" -u /code/fake_flexgate.py /srv 8080 >/dev/null 2>&1 || return 0
  wait_until 40 sh -c "docker logs '$(cname srv)' 2>&1 | grep -q 'listening'" && FG_OK=1
  return 0
}

# One scraper run (python directly, as the cron job does); sets SOUT and SRC
scrape() {
  [ "$FG_OK" -eq 1 ] || fail_t "the fake FlexGate server did not start"
  SRC=0
  SOUT="$(docker run --rm --network "$NET" --name "$PFX-fg-run-$RANDOM" \
    -v "$(hp "$DL_E/settings.yml"):/app/settings.yml:ro" -v "$(hp "$DL_E/downloads"):/app/downloads" \
    --entrypoint python "$IMAGE" /app/flexgate_scraper.py 2>&1)" || SRC=$?
}
n_downloads() { printf '%s\n' "$1" | grep -c 'Downloading ' || true; }

t_dl_first() {
  scrape
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC: $SOUT"
  contains "output" "$SOUT" "All files downloaded successfully."
  expect "downloaded entries" "$(listing "$DL_E/downloads")" "$(printf '%s\n' \
    'd A0001A_SITE-01' 'f A0001A_SITE-01/log.txt' 'f A0001A_SITE-01/state.dat' \
    'd A0001A_SITE-01/raw' 'f A0001A_SITE-01/raw/x1.f2p' 'f A0001A_SITE-01/raw/x2.f2b' \
    'd A0001A_SITE-01/par' 'f A0001A_SITE-01/par/p.par' \
    'd B0002B' 'd B0002B/raw' 'f B0002B/raw/y1.f2p' | sorted)"
  expect "number of downloads" "$(n_downloads "$SOUT")" "6"
  expect "content of raw/x1.f2p" "$(cat "$DL_E/downloads/A0001A_SITE-01/raw/x1.f2p")" "raw one"
  expect "content of log.txt" "$(cat "$DL_E/downloads/A0001A_SITE-01/log.txt")" "log"
}

t_dl_second() {
  local before
  before="$(listing "$DL_E/downloads")"
  scrape
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC: $SOUT"
  expect "number of downloads" "$(n_downloads "$SOUT")" "0"
  lacks "output at INFO level" "$SOUT" "Skipping"
  expect "downloaded entries" "$(listing "$DL_E/downloads")" "$before"
}

t_dl_new_file() {
  printf 'raw three\n' > "$DL_E/srv/fg2lite/data/A0001A/raw/x3.f2p"
  scrape
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC: $SOUT"
  expect "number of downloads" "$(n_downloads "$SOUT")" "1"
  contains "output" "$SOUT" "data/A0001A/raw/x3.f2p"
  expect "content of raw/x3.f2p" "$(cat "$DL_E/downloads/A0001A_SITE-01/raw/x3.f2p")" "raw three"
}

# ---- run -----------------------------------------------------------------------------

for c in bash cron python; do run_test "image has $c" t_has "$c"; done
for c in sudo curl wget git nano unzip rsync; do run_test "image lacks $c" t_lacks "$c"; done
run_test "image pip check passes" t_pip_check
run_test "image python packages import" t_python_imports
run_test "image scripts have valid syntax and the entrypoint is executable" t_syntax

run_test "entrypoint with settings keeps running and installs cron jobs" t_entry_ok
run_test "entrypoint refuses to start with an empty settings file" t_entry_empty_settings
run_test "entrypoint refuses to start without a settings file" t_entry_missing_settings

fg_setup
run_test "scraper downloads listed files and folders into <serial>_<logger_name>/<folder>" t_dl_first
run_test "scraper second run downloads nothing and logs no skips at INFO" t_dl_second
run_test "scraper downloads a file that appeared on the server" t_dl_new_file

TOTAL=$((PASSED + FAILED))
if [ "$FAILED" -eq 0 ]; then
  echo "ALL $TOTAL TESTS PASSED"
  exit 0
fi
echo "$FAILED OF $TOTAL TESTS FAILED"
exit 1
