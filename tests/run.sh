#!/bin/bash
# Behaviour tests of the flexgate_scraper image. Run on the workstation with the docker CLI
# against a built image, from the repository root:
#
#   bash tests/run.sh flexgate_scraper:local
#
# Works under bash on Linux and WSL, and from Git Bash on Windows (Docker Desktop).
# Every container runs with --network none, except the download and decode tests: those run
# the scraper against a local fake FlexGate server (tests/fake_flexgate.py) on a private
# --internal docker network, so nothing leaves the workstation. The decode tests use a stub
# decoder (tests/stub_decode3.py), never the real one. All containers have names starting
# with a unique prefix and are removed at the end, together with the network, three docker
# volumes and the temporary directories (files that containers created there are deleted from
# inside a container). Nothing else is touched. Scraper runs happen as the scraper user
# dockeruser (uid 30000), as in production; the image's process user stays root (cron).
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
DECVOL="$PFX-dec" STUBVOL="$PFX-stub" REPVOL="$PFX-rep"
SU=dockeruser   # the scraper user
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
  docker volume rm "$DECVOL" "$STUBVOL" "$REPVOL" >/dev/null 2>&1 || true
  if [ -d "$T" ]; then
    # containers wrote as root or uid 30000: delete from inside a container, then remove what is left
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
  printf '%s\n' 'SHELL=/bin/sh' '20 * * * * root /usr/local/bin/as-dockeruser /usr/local/bin/python /app/flexgate_scraper.py > /proc/1/fd/1 2>&1' > "$E/cron/cron_test"
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

t_user() {
  expect "id $SU" "$(plain "id $SU")" "uid=30000(dockeruser) gid=30000(dockeruser) groups=30000(dockeruser)"
  expect "folders" "$(plain 'stat -c "%u:%g %a %n" /app/downloads /var/lib/flexgate_scraper')" \
    "$(printf '%s\n' '30000:30000 755 /app/downloads' '30000:30000 755 /var/lib/flexgate_scraper')"
  expect "image process user (cron needs root)" "$(docker image inspect -f '{{.Config.User}}' "$IMAGE")" ""
  plain 'test ! -e /etc/sudoers && test ! -e /etc/sudoers.d' || fail_t "sudoers configuration present"
  expect "as-dockeruser" "$(plain '/usr/local/bin/as-dockeruser id')" "uid=30000(dockeruser) gid=30000(dockeruser) groups=30000(dockeruser)"
}

# ---- entrypoint ----------------------------------------------------------------------

# Start the entrypoint in the background: start_entry <name> [docker run arguments...];
# waits until the cron jobs are installed
start_entry() {
  local name="$1"
  shift
  docker run -d --name "$name" -v "$(hp "$E/settings.yml"):/app/settings.yml:ro" \
    -v "$(hp "$E/cron"):/app/cron_jobs:ro" "$@" "$IMAGE" >/dev/null || fail_t "could not start"
  wait_until 40 sh -c "docker logs '$name' 2>&1 | grep -q 'cron.d/ updated'" || fail_t "entrypoint did not finish: $(docker logs "$name" 2>&1 | tail -n 5)"
}

t_entry_ok() {
  mkenv entry_ok
  local name running mode cronfile out
  name="$(cname)"
  start_entry "$name" --network none
  sleep 2
  running="$(docker inspect -f '{{.State.Running}}' "$name")"
  mode="$(docker exec "$name" stat -c '%a %U' /etc/cron.d/cron_test)"
  cronfile="$(docker exec "$name" cat /etc/cron.d/cron_test)"
  out="$(docker logs "$name" 2>&1)"
  docker rm -f "$name" >/dev/null
  expect "container running" "$running" "true"
  expect "cron file mode and owner" "$mode" "644 root"
  contains "cron file" "$cronfile" "/app/flexgate_scraper.py"
  lacks "log" "$out" "WARNING"
  lacks "log" "$out" "owner of"
}

# Entrypoint that must refuse to start: exit non-zero with the message, no cron file
# installed. entry_must_fail <message> <docker run arguments...> (e.g. --entrypoint, image)
entry_must_fail() {
  local msg="$1" name out rc=0 changes
  shift
  name="$(cname)"
  out="$(docker run --network none --name "$name" -v "$(hp "$E/cron"):/app/cron_jobs:ro" "$@" 2>&1)" || rc=$?
  changes="$(docker diff "$name" || true)"
  docker rm -f "$name" >/dev/null
  [ "$rc" -ne 0 ] || fail_t "exit code 0, expected non-zero: $out"
  case "$changes" in *"/etc/cron.d/cron_test"*) fail_t "a cron file was installed" ;; esac
  contains "log" "$out" "$msg"
}

t_entry_empty_settings() {
  mkenv entry_empty
  : > "$E/settings.yml"
  entry_must_fail "/app/settings.yml is missing or empty" -v "$(hp "$E/settings.yml"):/app/settings.yml:ro" "$IMAGE"
}

# The image ships a template app/settings.yml, so "missing" is tested by removing it first
t_entry_missing_settings() {
  mkenv entry_missing
  entry_must_fail "/app/settings.yml is missing or empty" --entrypoint bash "$IMAGE" -c 'rm -f /app/settings.yml && exec /entrypoint.sh'
}

t_entry_unreadable_settings() {
  mkenv entry_unreadable
  entry_must_fail "/app/settings.yml is not readable by the scraper user dockeruser" \
    --entrypoint bash "$IMAGE" -c 'chmod 0600 /app/settings.yml && exec /entrypoint.sh'
}

# A volume with files and folders from a version that ran as root: the first start hands
# them all to the scraper user and logs the count, a second start changes nothing
t_entry_repair() {
  mkenv entry_repair
  docker run --rm --network none -v "$REPVOL:/app/downloads" --entrypoint sh "$IMAGE" -c '
    cd /app/downloads && chown root:root . && mkdir -p X0001X/raw && echo a > X0001X/raw/a.f2p &&
    echo b > X0001X/log.txt && chown 30000:root X0001X/log.txt' || fail_t "could not fill the volume"
  local out1 out2 left
  start_entry "$(cname 1)" --network none -v "$REPVOL:/app/downloads"
  out1="$(docker logs "$(cname 1)" 2>&1)"
  docker rm -f "$(cname 1)" >/dev/null
  left="$(docker run --rm --network none -v "$REPVOL:/d:ro" --entrypoint find "$IMAGE" /d \( ! -user 30000 -o ! -group 30000 \) -print)"
  start_entry "$(cname 2)" --network none -v "$REPVOL:/app/downloads"
  out2="$(docker logs "$(cname 2)" 2>&1)"
  docker rm -f "$(cname 2)" >/dev/null
  contains "first start" "$out1" "/app/downloads: owner of 5 entries changed to dockeruser:dockeruser"
  expect "entries not owned by 30000:30000" "$left" ""
  lacks "second start" "$out2" "owner of"
  lacks "second start" "$out2" "WARNING"
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
  SOUT="$(docker run --rm --network "$NET" --name "$PFX-fg-run-$RANDOM" --user "$SU" \
    -v "$(hp "$DL_E/settings.yml"):/app/settings.yml:ro" -v "$(hp "$DL_E/downloads"):/app/downloads" \
    --entrypoint python "$IMAGE" /app/flexgate_scraper.py 2>&1)" || SRC=$?
}
n_downloads() { printf '%s\n' "$1" | grep -c 'Downloading ' || true; }

# A run as root exits 2 with the message and writes nothing; also --check-settings
t_root_refused() {
  [ "$FG_OK" -eq 1 ] || fail_t "the fake FlexGate server did not start"
  local d="$T/rootrun" out rc arg
  mkdir -p "$d"
  for arg in "" --check-settings; do
    rc=0
    # shellcheck disable=SC2086  # no argument at all when arg is empty
    out="$(docker run --rm --network "$NET" --name "$(cname)" \
      -v "$(hp "$DL_E/settings.yml"):/app/settings.yml:ro" -v "$(hp "$d"):/app/downloads" \
      --entrypoint python "$IMAGE" /app/flexgate_scraper.py $arg 2>&1)" || rc=$?
    expect "exit code ($arg)" "$rc" "2"
    contains "log ($arg)" "$out" "ERROR - Refusing to run as root: run the scraper as the scraper user dockeruser, e.g. docker exec -u dockeruser flexgate_scraper python /app/flexgate_scraper.py"
    lacks "log ($arg)" "$out" "Downloading"
    lacks "log ($arg)" "$out" "are valid"
  done
  expect "downloaded entries" "$(listing "$d")" ""
}

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

# ---- decoding (stub decoder) ---------------------------------------------------------
# The decode tests share the fake FlexGate server. Downloads go to a docker volume ($DECVOL,
# so file modes are real on every platform) and tests/stub_decode3.py is installed as
# /stub/decode3 in a second volume ($STUBVOL), together with the stub's call logs and the
# copies of the CSVs it wrote (/stub/keep). Settings use decoder_path /stub/decode3, which
# also hides any real decoder in the image. The tests run in order.

DEC_OK=0 DS=""

# Shell in a container with both volumes: downloads at /d, stub at /stub (as the scraper user)
vsh() { docker run --rm --network none --user "$SU" --entrypoint sh -v "$DECVOL:/d" -v "$STUBVOL:/stub" "$IMAGE" -c "$1"; }

# Settings for the decode tests:
#   dec_cfg <file> <decoder_path> <timeout_seconds> <comment_char|off> <source>...
# source is SERIAL or SERIAL:LOGGER_NAME; "off" disables post-processing.
dec_cfg() {
  local f="$1" dp="$2" to="$3" pp="$4" s
  shift 4
  {
    echo 'flexgate_browse_url: "http://flexgate:8080/fg2lite/sw/browse.php"'
    echo 'base_download_folder: "/app/downloads"'
    echo 'downloadable_extensions: [".f2p", ".f2b"]'
    echo 'sources:'
    for s in "$@"; do
      echo "  - serial: \"${s%%:*}\""
      [ "$s" = "${s%%:*}" ] || echo "    logger_name: \"${s#*:}\""
      echo '    folders: [raw, par]'
    done
    echo 'decoding:'
    echo '  enabled: true'
    echo "  decoder_path: $dp"
    echo '  output_folder: decoded'
    echo "  timeout_seconds: $to"
    echo '  postprocessing:'
    if [ "$pp" = off ]; then
      echo '    enabled: false'
      echo '    comment_char: "%"'
    else
      echo '    enabled: true'
      echo "    comment_char: \"$pp\""
    fi
    echo '    steps: [comment_metadata]'
  } > "$f"
}

dec_setup() {
  TESTNAME="decsetup"
  local d="$DL_E/srv/fg2lite/data" s
  for s in C0003C D0004D E0005E F0006F G0007G; do mkdir -p "$d/$s/raw" "$d/$s/par"; done
  # C0003C: a parameter set and two data files, one with metadata between the rows and a
  # repeated header. F0006F: the same data under another serial.
  printf 'NO;TIME;#1(oC);#2(oC);HK-BAT(V)\n' > "$d/C0003C/par/C0003C_P1.f2p"
  printf '%s\n' 'PARSET C0003C_P1.f2p' '<READOUT>' "<HK-INFO: 'Server OK'>" \
    '1;28.10.2026 16:00:10;-10,87;-9,25;6,400' '2;28.10.2026 17:00:10;-10,92;-9,3' \
    '<READOUT>' "<HK-INFO: 'Server Timeout'>" 'NO;TIME;#1(oC);#2(oC);HK-BAT(V)' \
    '3;28.10.2026 18:00:10;-11;-9,37' > "$d/C0003C/raw/C0003C_001.f2b"
  printf '%s\n' 'PARSET C0003C_P1.f2p' '4;28.10.2026 19:00:10;-11,1;-9,4;6,398' > "$d/C0003C/raw/C0003C_002.f2b"
  sed 's/C0003C/F0006F/g' "$d/C0003C/par/C0003C_P1.f2p" > "$d/F0006F/par/F0006F_P1.f2p"
  sed 's/C0003C/F0006F/g' "$d/C0003C/raw/C0003C_001.f2b" > "$d/F0006F/raw/F0006F_001.f2b"
  # D0004D: data file whose parameter set is not on the server (yet)
  printf '%s\n' 'PARSET D0004D_P1.f2p' '1;28.10.2026 16:00:10;-1,5;-2,5' > "$d/D0004D/raw/D0004D_001.f2b"
  # E0005E: exit 255, timeout, a good file, exit 0 without output
  printf 'NO;TIME;#1(oC)\n' > "$d/E0005E/par/E0005E_P1.f2p"
  printf '%s\n' 'STUB:exit255' 'PARSET E0005E_P1.f2p' '1;x;1' > "$d/E0005E/raw/E0005E_a.f2b"
  printf '%s\n' 'STUB:sleep 30' 'PARSET E0005E_P1.f2p' '1;x;1' > "$d/E0005E/raw/E0005E_b.f2b"
  printf '%s\n' 'PARSET E0005E_P1.f2p' '<READOUT>' '1;28.10.2026 16:00:10;-3,25' > "$d/E0005E/raw/E0005E_c.f2b"
  printf '%s\n' 'STUB:noout' 'PARSET E0005E_P1.f2p' '1;x;1' > "$d/E0005E/raw/E0005E_d.f2b"
  # G0007G: downloaded while the decoder is missing
  printf '%s\n' 'PARSET G0007G_P1.f2p' '1;x;1' > "$d/G0007G/raw/G0007G_001.f2b"

  DS="$DL_E/dec"
  mkdir -p "$DS"
  dec_cfg "$DS/s1.yml" /stub/decode3 60 % C0003C:SITE-03
  dec_cfg "$DS/s2.yml" /stub/decode3 60 % C0003C:SITE-03 D0004D
  dec_cfg "$DS/s3.yml" /stub/decode3 3 off E0005E
  dec_cfg "$DS/s4.yml" /stub/decode3 60 '#' F0006F
  dec_cfg "$DS/s5.yml" /opt/nothere/decode3 60 % G0007G

  [ "$FG_OK" -eq 1 ] || return 0
  docker volume create "$DECVOL" >/dev/null 2>&1 || return 0
  docker volume create "$STUBVOL" >/dev/null 2>&1 || return 0
  docker run --rm --network none --entrypoint sh -v "$STUBVOL:/stub" -v "$(hp "$TESTS_DIR"):/src:ro" "$IMAGE" \
    -c 'cp /src/stub_decode3.py /stub/decode3 && chmod 0755 /stub/decode3 && mkdir -p /stub/keep && chown -R 30000:30000 /stub' >/dev/null 2>&1 || return 0
  DEC_OK=1
}

# One scraper run with the decode volumes; sets SOUT, SRC and DCALLS (the inputs the stub
# was called with, one per line, in order). Fails if anything is left in /tmp after the run
# (the decoder work folder must be removed, also after errors; the run lock file stays).
#   dscrape <settings file> [docker run arguments...]
# -e SCRAPE_UMASK=<umask> runs the scraper with that umask (default 022).
dscrape() {
  [ "$DEC_OK" -eq 1 ] || fail_t "the decode test setup failed"
  local calls="/stub/calls-$TESTNAME-$RANDOM.log" settings="$1"
  shift
  SRC=0
  SOUT="$(docker run --rm --network "$NET" --name "$PFX-dec-run-$RANDOM" --user "$SU" \
    -v "$(hp "$settings"):/app/settings.yml:ro" -v "$DECVOL:/app/downloads" -v "$STUBVOL:/stub" \
    -e STUB_DECODE3_CALLS="$calls" -e STUB_DECODE3_KEEP=/stub/keep "$@" \
    --entrypoint sh "$IMAGE" -c 'umask "${SCRAPE_UMASK:-022}"; python /app/flexgate_scraper.py; rc=$?
      ls -A /tmp | grep -vx flexgate_scraper.lock | sed "s/^/TMPLEFT /"; exit $rc' 2>&1)" || SRC=$?
  lacks "work folder left in /tmp" "$SOUT" "TMPLEFT"
  DCALLS="$(vsh "cat $calls 2>/dev/null; true")"
}
dlisting() { vsh "cd /d/$1 && find . -mindepth 1 -printf '%y %P\n'" | sorted; }

# Checks a published CSV against the stub's CSV after comment_metadata:
#   python -c "$CHECK_COMMENTED" <stub csv> <published csv> <comment_char>
CHECK_COMMENTED='
import sys
a = open(sys.argv[1], "rb").read().splitlines(keepends=True)
b = open(sys.argv[2], "rb").read().splitlines(keepends=True)
c = sys.argv[3].encode()
assert len(a) == len(b), f"line count {len(b)}, stub CSV has {len(a)}"
assert all(x.endswith(b"\r\n") for x in a), "stub CSV lines do not all end with CRLF"
seen, n = False, {"meta": 0, "header": 0, "data": 0}
for i, (x, y) in enumerate(zip(a, b), 1):
    if x.startswith(b"<"):
        exp, k = c + x, "meta"
    elif x.startswith((b"NO;TIME", b"NO,TIME")):
        exp, k = (c + x if seen else x), ("header" if seen else "data")
        seen = True
    else:
        exp, k = x, "data"
    n[k] += 1
    assert y == exp, f"line {i}: got {y!r}, expected {exp!r}"
assert n["meta"] >= 6 and n["header"] == 1 and n["data"] >= 4, n
print("ok", n)
'
check_commented() {   # check_commented <stub csv> <published csv> <comment_char>
  docker run --rm --network none --entrypoint python -v "$DECVOL:/d:ro" -v "$STUBVOL:/stub:ro" "$IMAGE" \
    -c "$CHECK_COMMENTED" "$@" 2>&1 || fail_t "comment_metadata output is wrong"
}

t_dec_absent() {
  local l
  l="$(listing "$DL_E/downloads")"
  lacks "downloaded entries" "$l" "decoded"
  lacks "downloaded entries" "$l" ".csv"
}

t_dec_first() {
  dscrape "$DS/s1.yml"
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC: $SOUT"
  expect "decoder calls" "$DCALLS" "$(printf '%s\n' C0003C_P1.f2p C0003C_001.f2b C0003C_002.f2b)"
  contains "log" "$SOUT" "Decoding C0003C_SITE-03: 2 decoded, 0 failed"
  lacks "log" "$SOUT" "ERROR"
  expect "logger folder" "$(dlisting C0003C_SITE-03)" "$(printf '%s\n' \
    'd raw' 'f raw/C0003C_001.f2b' 'f raw/C0003C_002.f2b' 'd par' 'f par/C0003C_P1.f2p' \
    'd decoded' 'f decoded/C0003C_001.csv' 'f decoded/C0003C_002.csv' | sorted)"
}

t_dec_header() {
  local csv
  csv="$(vsh 'cat /d/C0003C_SITE-03/decoded/C0003C_001.csv')"
  contains "decoded/C0003C_001.csv" "$csv" "$(printf 'C0003C_P1.f2p>\r\nNO;TIME;#1(oC);#2(oC);HK-BAT(V)\r\n%%<READOUT>\r\n')"
  lacks "decoded/C0003C_001.csv" "$csv" "unk."
  lacks "decoded/C0003C_001.csv" "$csv" "<ERROR"
}

t_dec_mode() {
  expect "modes" "$(vsh 'stat -c "%a %n" /d/C0003C_SITE-03/decoded/*')" "$(printf '%s\n' \
    '644 /d/C0003C_SITE-03/decoded/C0003C_001.csv' '644 /d/C0003C_SITE-03/decoded/C0003C_002.csv')"
  expect "temporary files" "$(vsh 'find /d -name "*.tmp"')" ""
}

t_dec_comment_pct() {
  local expected
  expected="$(printf "2;28.10.2026 17:00:10;-10,92;-9,3\r\n%%<READOUT>\r\n%%<HK-INFO: 'Server Timeout'>\r\n%%NO;TIME;")"
  check_commented /stub/keep/C0003C_001.csv /d/C0003C_SITE-03/decoded/C0003C_001.csv %
  contains "decoded/C0003C_001.csv" "$(vsh 'cat /d/C0003C_SITE-03/decoded/C0003C_001.csv')" "$expected"
}

t_dec_second() {
  local before
  before="$(dlisting C0003C_SITE-03)"
  dscrape "$DS/s1.yml"
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC: $SOUT"
  expect "decoder calls" "$DCALLS" ""
  lacks "log" "$SOUT" "Decoding"
  expect "logger folder" "$(dlisting C0003C_SITE-03)" "$before"
}

t_dec_missing_par() {
  dscrape "$DS/s2.yml"
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC: $SOUT"
  expect "decoder calls" "$DCALLS" "D0004D_001.f2b"
  contains "log" "$SOUT" "ERROR - Decoding D0004D/raw/D0004D_001.f2b failed"
  contains "log" "$SOUT" "PARAMETER SET NOT FOUND"
  contains "log" "$SOUT" "Decoding D0004D: 0 decoded, 1 failed"
  expect "logger folder" "$(dlisting D0004D)" "$(printf '%s\n' 'd raw' 'f raw/D0004D_001.f2b' 'd par' | sorted)"
}

t_dec_par_appears() {
  printf 'NO;TIME;#1(oC);#2(oC)\n' > "$DL_E/srv/fg2lite/data/D0004D/par/D0004D_P1.f2p"
  dscrape "$DS/s2.yml"
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC: $SOUT"
  expect "decoder calls" "$DCALLS" "$(printf '%s\n' D0004D_P1.f2p D0004D_001.f2b)"
  contains "log" "$SOUT" "Decoding D0004D: 1 decoded, 0 failed"
  contains "decoded/D0004D_001.csv" "$(vsh 'cat /d/D0004D/decoded/D0004D_001.csv')" "$(printf 'NO;TIME;#1(oC);#2(oC)\r\n1;')"
}

t_dec_failures() {
  dscrape "$DS/s3.yml"
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC: $SOUT"
  expect "decoder calls" "$DCALLS" "$(printf '%s\n' E0005E_P1.f2p E0005E_a.f2b E0005E_b.f2b E0005E_c.f2b E0005E_d.f2b)"
  contains "log" "$SOUT" "ERROR - Decoding E0005E/raw/E0005E_a.f2b failed (retried next run): decoder exit code 255"
  contains "log" "$SOUT" "ERROR - Decoding E0005E/raw/E0005E_b.f2b failed (retried next run): decoder timed out after 3 s"
  contains "log" "$SOUT" "ERROR - Decoding E0005E/raw/E0005E_d.f2b failed (retried next run): decoder wrote no"
  contains "log" "$SOUT" "Decoding E0005E: 1 decoded, 3 failed"
  expect "published" "$(vsh 'ls -A /d/E0005E/decoded')" "E0005E_c.csv"
}

t_dec_pp_off() {
  docker run --rm --network none --entrypoint python -v "$DECVOL:/d:ro" -v "$STUBVOL:/stub:ro" "$IMAGE" -c \
    'import filecmp, sys; sys.exit(0 if filecmp.cmp(sys.argv[1], sys.argv[2], shallow=False) else 1)' \
    /stub/keep/E0005E_c.csv /d/E0005E/decoded/E0005E_c.csv || fail_t "published CSV differs from the stub's CSV"
}

t_dec_comment_hash() {
  dscrape "$DS/s4.yml"
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC: $SOUT"
  contains "log" "$SOUT" "Decoding F0006F: 1 decoded, 0 failed"
  check_commented /stub/keep/F0006F_001.csv /d/F0006F/decoded/F0006F_001.csv '#'
}

t_dec_no_decoder() {
  dscrape "$DS/s5.yml"
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC: $SOUT"
  expect "missing decoder errors" "$(printf '%s\n' "$SOUT" | grep -c 'ERROR - Decoder /opt/nothere/decode3 is missing or not executable' || true)" "1"
  expect "number of downloads" "$(n_downloads "$SOUT")" "1"
  expect "logger folder" "$(dlisting G0007G)" "$(printf '%s\n' 'd raw' 'f raw/G0007G_001.f2b' 'd par' | sorted)"
}

# Invalid decoding settings: the scraper exits non-zero with the message and downloads
# nothing. bad_settings <sed expression applied to s1.yml> <expected message>
bad_settings() {
  [ -n "$DS" ] || fail_t "the decode test setup did not run"
  local f="$DS/bad-$TESTNAME.yml" out rc=0
  sed "$1" "$DS/s1.yml" > "$f"
  ! cmp -s "$f" "$DS/s1.yml" || fail_t "the sed expression changed nothing"
  out="$(docker run --rm --network none --user "$SU" -v "$(hp "$f"):/app/settings.yml:ro" \
    --entrypoint python "$IMAGE" /app/flexgate_scraper.py 2>&1)" || rc=$?
  [ "$rc" -ne 0 ] || fail_t "exit code 0, expected non-zero: $out"
  contains "log" "$out" "Invalid settings in /app/settings.yml, not running"
  contains "log" "$out" "$2"
  lacks "log" "$out" "Downloading"
  lacks "log" "$out" "Traceback"
}

t_bad_step()   { bad_settings 's/steps: \[comment_metadata\]/steps: [comment_metadata, frobnicate]/' "unknown post-processing step(s) ['frobnicate']"; }
t_bad_char()   { bad_settings 's/comment_char: "%"/comment_char: "%%"/' "comment_char must be exactly one non-whitespace character, got '%%'"; }
t_bad_space()  { bad_settings 's/comment_char: "%"/comment_char: " "/' "comment_char must be exactly one non-whitespace character"; }
t_bad_key()    { bad_settings 's/^  enabled: true/  enabled: true\n  colour: blue/' "decoding.colour"; }
t_bad_pp_key() { bad_settings 's/^    enabled: true/    enabled: true\n    colour: blue/' "decoding.postprocessing.colour"; }

t_bad_entry() {
  [ -n "$DS" ] || fail_t "the decode test setup did not run"
  mkenv bad_entry
  sed 's/steps: \[comment_metadata\]/steps: [frobnicate]/' "$DS/s1.yml" > "$E/settings.yml"
  local name out rc=0 changes
  name="$(cname)"
  out="$(docker run --network none --name "$name" -v "$(hp "$E/settings.yml"):/app/settings.yml:ro" \
    -v "$(hp "$E/cron"):/app/cron_jobs:ro" "$IMAGE" 2>&1)" || rc=$?
  changes="$(docker diff "$name" || true)"
  docker rm -f "$name" >/dev/null
  [ "$rc" -ne 0 ] || fail_t "exit code 0, expected non-zero: $out"
  case "$changes" in *"/etc/cron.d/cron_test"*) fail_t "a cron file was installed" ;; esac
  contains "log" "$out" "unknown post-processing step(s) ['frobnicate']"
  contains "log" "$out" "/app/settings.yml is invalid"
}

# ---- run robustness ------------------------------------------------------------------
# Failing and stalling sources, partial downloads, file modes and the run lock. Uses the
# fake FlexGate server (control files .fake_status/.fake_delay, see fake_flexgate.py) and
# the decode volumes; new serials only, so earlier folders are not touched.

RS=""

# Settings without decoding: rob_cfg <file> <source>...; source is SERIAL or SERIAL:FILE
# (FILE is downloaded from the serial page as a listed file)
rob_cfg() {
  local f="$1" s
  shift
  {
    echo 'flexgate_browse_url: "http://flexgate:8080/fg2lite/sw/browse.php"'
    echo 'base_download_folder: "/app/downloads"'
    echo 'downloadable_extensions: [".f2p", ".f2b"]'
    echo 'sources:'
    for s in "$@"; do
      echo "  - serial: \"${s%%:*}\""
      echo '    folders: [raw, par]'
      [ "$s" = "${s%%:*}" ] || echo "    files: [\"${s#*:}\"]"
    done
  } > "$f"
}

rob_setup() {
  TESTNAME="robsetup"
  local d="$DL_E/srv/fg2lite/data" s
  for s in H0008H I0009I J0010J K0011K L0012L M0013M N0014N O0015O; do mkdir -p "$d/$s/raw" "$d/$s/par"; done
  # H0008H: every listing answers HTTP 500. I0009I: every listing stalls for 20 s.
  printf 'h\n' > "$d/H0008H/raw/H0008H_001.f2b"
  printf '500\n' > "$d/H0008H/.fake_status"
  printf 'i\n' > "$d/I0009I/raw/I0009I_001.f2b"
  printf '20\n' > "$d/I0009I/.fake_delay"
  # J0010J: good logger with a parameter set and a data file (decoded after H0008H fails)
  printf 'NO;TIME;#1(oC)\n' > "$d/J0010J/par/J0010J_P1.f2p"
  printf '%s\n' 'PARSET J0010J_P1.f2p' '1;28.10.2026 16:00:10;-1,5' > "$d/J0010J/raw/J0010J_001.f2b"
  # K0011K: leftover partial downloads; L0012L, M0013M: plain good loggers
  printf 'k log\n' > "$d/K0011K/log.txt"
  printf 'k raw\n' > "$d/K0011K/raw/k1.f2p"
  printf 'l raw\n' > "$d/L0012L/raw/l1.f2p"
  printf 'm raw\n' > "$d/M0013M/raw/m1.f2p"
  # N0014N: owner and modes under umask 077; O0015O: downloaded by the cron job
  printf 'n log\n' > "$d/N0014N/log.txt"
  printf 'n raw\n' > "$d/N0014N/raw/n1.f2p"
  printf 'NO;TIME\n' > "$d/N0014N/par/N0014N_P1.f2p"
  printf 'o raw\n' > "$d/O0015O/raw/o1.f2p"

  RS="$DL_E/rob"
  mkdir -p "$RS"
  dec_cfg "$RS/r1.yml" /stub/decode3 60 % H0008H J0010J
  rob_cfg "$RS/r2.yml" I0009I L0012L
  rob_cfg "$RS/r3.yml" K0011K:log.txt
  rob_cfg "$RS/r4.yml" M0013M
  rob_cfg "$RS/r5.yml" N0014N:log.txt
  rob_cfg "$RS/r6.yml" O0015O
}

t_rob_http500() {
  dscrape "$RS/r1.yml"
  expect "scraper exit code" "$SRC" "1"
  contains "log" "$SOUT" "ERROR - Source H0008H failed, continuing with the next source: HTTPError: 500"
  expect "H0008H files" "$(vsh 'find /d/H0008H -type f')" ""
  expect "J0010J downloads" "$(dlisting J0010J)" "$(printf '%s\n' 'd raw' 'f raw/J0010J_001.f2b' 'd par' \
    'f par/J0010J_P1.f2p' 'd decoded' 'f decoded/J0010J_001.csv' | sorted)"
  expect "decoder calls" "$DCALLS" "$(printf '%s\n' J0010J_P1.f2p J0010J_001.f2b)"
  contains "log" "$SOUT" "Decoding J0010J: 1 decoded, 0 failed"
  contains "log" "$SOUT" "Downloads incomplete: 1 source(s) failed: H0008H"
  contains "log" "$SOUT" "Run finished: 1 source(s) ok, 1 failed, 2 file(s) downloaded."
  lacks "log" "$SOUT" "All files downloaded successfully."
}

t_rob_stall() {
  local start=$SECONDS
  dscrape "$RS/r2.yml" -e FLEXGATE_SCRAPER_TIMEOUT_SECONDS=2
  expect "scraper exit code" "$SRC" "1"
  contains "log" "$SOUT" "ERROR - Source I0009I failed, continuing with the next source: ReadTimeout"
  expect "I0009I files" "$(vsh 'find /d/I0009I -type f')" ""
  expect "L0012L downloads" "$(dlisting L0012L)" "$(printf '%s\n' 'd raw' 'f raw/l1.f2p' 'd par' | sorted)"
  contains "log" "$SOUT" "Run finished: 1 source(s) ok, 1 failed, 1 file(s) downloaded."
  [ $((SECONDS - start)) -lt 15 ] || fail_t "the run waited for the stalled server ($((SECONDS - start)) s)"
}

t_rob_bad_timeout() {
  dscrape "$RS/r4.yml" -e FLEXGATE_SCRAPER_TIMEOUT_SECONDS=soon
  expect "scraper exit code" "$SRC" "2"
  contains "log" "$SOUT" "Invalid FLEXGATE_SCRAPER_TIMEOUT_SECONDS='soon'"
  lacks "log" "$SOUT" "Downloading"
}

t_rob_partial() {
  [ -n "$RS" ] || fail_t "the robustness setup did not run"
  vsh 'mkdir -p /d/K0011K/raw && echo partial > /d/K0011K/.log.txt.part && echo partial > /d/K0011K/raw/.k1.f2p.part' \
    || fail_t "could not create the partial files"
  dscrape "$RS/r3.yml" -e SCRAPE_UMASK=077
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC: $SOUT"
  contains "log" "$SOUT" "Removing partial download /app/downloads/K0011K/.log.txt.part"
  contains "log" "$SOUT" "Removing partial download /app/downloads/K0011K/raw/.k1.f2p.part"
  expect "number of downloads" "$(n_downloads "$SOUT")" "2"
  expect "K0011K downloads" "$(dlisting K0011K)" "$(printf '%s\n' 'f log.txt' 'd raw' 'f raw/k1.f2p' 'd par' | sorted)"
  expect "content of log.txt" "$(vsh 'cat /d/K0011K/log.txt')" "k log"
  expect "content of raw/k1.f2p" "$(vsh 'cat /d/K0011K/raw/k1.f2p')" "k raw"
  expect ".part files left" "$(vsh 'find /d -name "*.part"')" ""
}

# Everything the scraper creates belongs to 30000:30000, folders 0755 and files 0644, also
# when it is started with umask 077
t_rob_owner() {
  [ -n "$RS" ] || fail_t "the robustness setup did not run"
  dscrape "$RS/r5.yml" -e SCRAPE_UMASK=077
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC: $SOUT"
  expect "N0014N entries" "$(vsh 'cd /d && find N0014N -printf "%y %U:%G %m %p\n"' | sorted)" "$(printf '%s\n' \
    'd 30000:30000 755 N0014N' 'f 30000:30000 644 N0014N/log.txt' 'd 30000:30000 755 N0014N/raw' \
    'f 30000:30000 644 N0014N/raw/n1.f2p' 'd 30000:30000 755 N0014N/par' 'f 30000:30000 644 N0014N/par/N0014N_P1.f2p' | sorted)"
}

# A cron job line in the production form (user field root, the root shell opens
# /proc/1/fd/1, the command runs through as-dockeruser) runs as uid 30000 and its output
# reaches the container log. A line without the privilege drop fails loudly in the log.
# Waits for the next cron minute, at most about 75 s.
t_cron_job() {
  [ -n "$RS" ] || fail_t "the robustness setup did not run"
  mkenv cron_job
  cp "$RS/r6.yml" "$E/settings.yml"
  printf '%s\n' 'SHELL=/bin/sh' \
    '* * * * * root /usr/local/bin/as-dockeruser sh -c '"'"'echo "cron job uid: $(id -u):$(id -g)"; exec /usr/local/bin/python /app/flexgate_scraper.py'"'"' > /proc/1/fd/1 2>&1' \
    '* * * * * root /usr/local/bin/python /app/flexgate_scraper.py --check-settings > /proc/1/fd/1 2>&1' \
    > "$E/cron/cron_test"
  local name out owner
  name="$(cname)"
  start_entry "$name" --network "$NET" -v "$REPVOL:/app/downloads"
  wait_until 150 sh -c "docker logs '$name' 2>&1 | grep -q 'Run finished' && docker logs '$name' 2>&1 | grep -q 'Refusing'" || true
  out="$(docker logs "$name" 2>&1)"
  owner="$(docker exec "$name" stat -c '%U:%G %a' /app/downloads/O0015O/raw/o1.f2p 2>&1 || true)"
  docker rm -f "$name" >/dev/null
  contains "container log" "$out" "cron job uid: 30000:30000"
  contains "container log" "$out" "Run finished: 1 source(s) ok, 0 failed, 1 file(s) downloaded."
  contains "container log (line without the privilege drop)" "$out" "ERROR - Refusing to run as root"
  expect "downloaded file" "$owner" "dockeruser:dockeruser 644"
}

t_rob_mode() {
  expect "modes" "$(vsh 'stat -c "%a %n" /d/K0011K/log.txt /d/K0011K/raw/k1.f2p /d/L0012L/raw/l1.f2p /d/J0010J/raw/J0010J_001.f2b')" \
    "$(printf '%s\n' '644 /d/K0011K/log.txt' '644 /d/K0011K/raw/k1.f2p' '644 /d/L0012L/raw/l1.f2p' '644 /d/J0010J/raw/J0010J_001.f2b')"
}

# A process in the same container holds the lock while the scraper starts
LOCK_HOLDER='
import fcntl, os, time
fd = os.open("/tmp/flexgate_scraper.lock", os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX)
open("/tmp/held", "w").close()
time.sleep(60)
'
t_rob_lock() {
  [ "$DEC_OK" -eq 1 ] || fail_t "the decode test setup failed"
  local out rc=0
  out="$(docker run --rm --network "$NET" --name "$(cname)" --user "$SU" \
    -v "$(hp "$RS/r4.yml"):/app/settings.yml:ro" -v "$DECVOL:/app/downloads" -e LOCK_HOLDER="$LOCK_HOLDER" \
    --entrypoint sh "$IMAGE" -c 'python -c "$LOCK_HOLDER" & pid=$!; i=0
      while [ ! -e /tmp/held ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
      [ -e /tmp/held ] || { echo "lock holder did not start"; exit 9; }
      python /app/flexgate_scraper.py; rc=$?; kill $pid; exit $rc' 2>&1)" || rc=$?
  expect "scraper exit code" "$rc" "0"
  contains "log" "$out" "WARNING - Lock /tmp/flexgate_scraper.lock is held: another run is in progress, skipping this run."
  lacks "log" "$out" "Downloading"
  expect "M0013M folder" "$(vsh 'ls -A /d | grep -x M0013M; true')" ""
  # without the holder the same settings download the file
  dscrape "$RS/r4.yml"
  [ "$SRC" -eq 0 ] || fail_t "scraper exit code $SRC without the lock holder: $SOUT"
  expect "number of downloads" "$(n_downloads "$SOUT")" "1"
}

# ---- run -----------------------------------------------------------------------------

for c in bash cron python; do run_test "image has $c" t_has "$c"; done
for c in sudo curl wget git nano unzip rsync; do run_test "image lacks $c" t_lacks "$c"; done
run_test "image pip check passes" t_pip_check
run_test "image python packages import" t_python_imports
run_test "image scripts have valid syntax and the entrypoint is executable" t_syntax
run_test "image has user dockeruser 30000:30000 owning the data folders, process user root" t_user

run_test "entrypoint with settings keeps running and installs cron jobs" t_entry_ok
run_test "entrypoint refuses to start with an empty settings file" t_entry_empty_settings
run_test "entrypoint refuses to start without a settings file" t_entry_missing_settings
run_test "entrypoint refuses to start with settings the scraper user cannot read" t_entry_unreadable_settings
run_test "entrypoint hands root-owned downloads to dockeruser once, logs the count" t_entry_repair

fg_setup
run_test "scraper refuses to run as root, exit 2, nothing downloaded" t_root_refused
run_test "scraper downloads listed files and folders into <serial>_<logger_name>/<folder>" t_dl_first
run_test "scraper second run downloads nothing and logs no skips at INFO" t_dl_second
run_test "scraper downloads a file that appeared on the server" t_dl_new_file
run_test "decode: without a decoding block nothing is decoded" t_dec_absent

dec_setup
run_test "decode: first run decodes parameter sets first, then each .f2b, adds only decoded/" t_dec_first
run_test "decode: CSV has the parameter set's header" t_dec_header
run_test "decode: published files have mode 0644, no .tmp files left" t_dec_mode
run_test "decode: comment_metadata with % comments metadata and repeated headers only" t_dec_comment_pct
run_test "decode: second run calls the decoder for nothing" t_dec_second
run_test "decode: missing parameter set gives an ERROR and no CSV" t_dec_missing_par
run_test "decode: published after the parameter set appears" t_dec_par_appears
run_test "decode: exit 255, timeout and no output are ERRORs, other files decoded" t_dec_failures
run_test "decode: post-processing disabled publishes the decoder output unchanged" t_dec_pp_off
run_test "decode: comment_metadata with another comment character" t_dec_comment_hash
run_test "decode: missing decoder gives one ERROR, downloads still happen" t_dec_no_decoder
run_test "settings: unknown post-processing step is rejected" t_bad_step
run_test "settings: two-character comment_char is rejected" t_bad_char
run_test "settings: whitespace comment_char is rejected" t_bad_space
run_test "settings: unknown key in decoding is rejected" t_bad_key
run_test "settings: unknown key in postprocessing is rejected" t_bad_pp_key
run_test "entrypoint refuses to start with invalid decoding settings" t_bad_entry

rob_setup
run_test "robust: HTTP 500 on one logger, next logger downloaded and decoded, exit 1" t_rob_http500
run_test "robust: stalled server fails only that source after the timeout, exit 1" t_rob_stall
run_test "robust: invalid timeout override is rejected, nothing downloaded" t_rob_bad_timeout
run_test "robust: leftover .part files removed and downloaded again, none left" t_rob_partial
run_test "robust: downloaded files have mode 0644 (also with umask 077)" t_rob_mode
run_test "robust: second run while the lock is held exits 0 with a WARNING" t_rob_lock
run_test "user: files 30000:30000 0644, folders 0755, also with umask 077" t_rob_owner
run_test "user: cron job (root, as-dockeruser, > /proc/1/fd/1) runs as 30000, output in the container log" t_cron_job

TOTAL=$((PASSED + FAILED))
if [ "$FAILED" -eq 0 ]; then
  echo "ALL $TOTAL TESTS PASSED"
  exit 0
fi
echo "$FAILED OF $TOTAL TESTS FAILED"
exit 1
