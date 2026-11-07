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
# with a unique prefix and are removed at the end, together with the network, two docker
# volumes and the temporary directories (files that containers created there as root are deleted from
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
DECVOL="$PFX-dec" STUBVOL="$PFX-stub"
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
  docker volume rm "$DECVOL" "$STUBVOL" >/dev/null 2>&1 || true
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

# ---- decoding (stub decoder) ---------------------------------------------------------
# The decode tests share the fake FlexGate server. Downloads go to a docker volume ($DECVOL,
# so file modes are real on every platform) and tests/stub_decode3.py is installed as
# /stub/decode3 in a second volume ($STUBVOL), together with the stub's call logs and the
# copies of the CSVs it wrote (/stub/keep). Settings use decoder_path /stub/decode3, which
# also hides any real decoder in the image. The tests run in order.

DEC_OK=0 DS=""

# Shell in a container with both volumes: downloads at /d, stub at /stub
vsh() { docker run --rm --network none --entrypoint sh -v "$DECVOL:/d" -v "$STUBVOL:/stub" "$IMAGE" -c "$1"; }

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
    -c 'cp /src/stub_decode3.py /stub/decode3 && chmod 0755 /stub/decode3 && mkdir -p /stub/keep' >/dev/null 2>&1 || return 0
  DEC_OK=1
}

# One scraper run with the decode volumes; sets SOUT, SRC and DCALLS (the inputs the stub
# was called with, one per line, in order). Fails if anything is left in /tmp after the run
# (the decoder work folder must be removed, also after errors).
dscrape() {
  [ "$DEC_OK" -eq 1 ] || fail_t "the decode test setup failed"
  local calls="/stub/calls-$TESTNAME-$RANDOM.log"
  SRC=0
  SOUT="$(docker run --rm --network "$NET" --name "$PFX-dec-run-$RANDOM" \
    -v "$(hp "$1"):/app/settings.yml:ro" -v "$DECVOL:/app/downloads" -v "$STUBVOL:/stub" \
    -e STUB_DECODE3_CALLS="$calls" -e STUB_DECODE3_KEEP=/stub/keep \
    --entrypoint sh "$IMAGE" -c 'python /app/flexgate_scraper.py; rc=$?; ls -A /tmp | sed "s/^/TMPLEFT /"; exit $rc' 2>&1)" || SRC=$?
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
  out="$(docker run --rm --network none -v "$(hp "$f"):/app/settings.yml:ro" \
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

TOTAL=$((PASSED + FAILED))
if [ "$FAILED" -eq 0 ]; then
  echo "ALL $TOTAL TESTS PASSED"
  exit 0
fi
echo "$FAILED OF $TOTAL TESTS FAILED"
exit 1
