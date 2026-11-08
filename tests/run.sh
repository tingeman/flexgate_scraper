#!/bin/bash
# Tests of the flexgate_scraper image. Run on the workstation with the docker CLI against a
# built image, from the repository root:
#
#   bash tests/run.sh flexgate_scraper:local
#
# Works under bash on Linux and WSL, and from Git Bash on Windows (Docker Desktop).
# Two layers:
#
# * unit tests (tests/unit/test_*.py, names "unit: <module>: <description>"): the scraper's
#   logic (settings, downloads, top-level files, failing and stalling sources, partial
#   downloads, modes, run lock, status file, healthcheck, decoding with the stub decoder
#   tests/stub_decode3.py), tested in-process with the standard library's unittest against
#   the fake FlexGate server (tests/fake_flexgate.py) on 127.0.0.1. All of them run in ONE
#   container of the image (--network none, as the scraper user) through
#   tests/unit/run_unit.py, which prints one PASS/FAIL line per unit test.
# * container tests (all other names): what needs a real container: the image contents, the
#   entrypoint, the root refusal, one end-to-end path as dockeruser against the fake server
#   in its own container on a private --internal docker network, the docker healthcheck and a
#   cron job. Checks that can share a container do.
#
# Nothing leaves the workstation; the real decoder is never used. All containers have names
# starting with a unique prefix and are removed at the end, together with the network, the
# docker volume and the temporary directory. Nothing else is touched. Scraper runs happen as
# the scraper user dockeruser (uid 30000), as in production; the image's process user stays
# root (cron). Each test prints "PASS: <name>" or "FAIL: <name>: <reason>". The last line is
# "ALL <n> TESTS PASSED" (exit 0) or "<m> OF <n> TESTS FAILED" (exit 1), where n counts the
# tests that ran, unit tests included.
#
# Selecting tests, for quicker runs while working on a change:
#
#   TEST_FILTER='^unit:' bash tests/run.sh flexgate_scraper:local       # only the unit tests
#   TEST_FILTER='^entrypoint' bash tests/run.sh flexgate_scraper:local  # names matching (grep -E)
#   bash tests/run.sh flexgate_scraper:local --quick                    # skip the slow tests
#
# TEST_FILTER is an extended regular expression matched against the test names (as printed
# after "PASS: "); only matching tests run. --quick (before or after the image) skips the
# tests marked slow (the cron job test, which waits for the next cron minute). Both can be
# combined. A setup (fake server, shared containers) runs only if a selected test needs it,
# and the unit test container only if a unit test is selected. Every test sets up what it
# needs, so any single test can be selected. Skipped tests are counted in a "SKIPPED: <k>
# test(s)" line before the last line; a selection without any test is an error (exit 2). The
# full suite (no TEST_FILTER, no --quick) is what the image build runs, and it must pass
# before a commit.
#
# One unit test module directly, while iterating (from the repository root; under Git Bash
# set MSYS_NO_PATHCONV=1 and use "$(cygpath -m "$PWD")" for "$PWD"):
#
#   docker run --rm --network none --user dockeruser -e PYTHONDONTWRITEBYTECODE=1 \
#     -v "$PWD/tests:/tests:ro" -w /tests/unit --entrypoint python flexgate_scraper:local \
#     -m unittest -v test_download            # or test_download.DownloadTest.test_stall
#
# Adding -e FLEXGATE_APP_DIR=/appsrc -v "$PWD/app:/appsrc:ro" tests the app code of the
# checkout instead of the image's, without rebuilding.
# shellcheck disable=SC2329  # test functions are called indirectly through run_test
set -euo pipefail

usage() { echo "Usage: [TEST_FILTER=<regex>] $0 [--quick] <image>" >&2; exit 2; }
IMAGE="" QUICK=0
for a in "$@"; do
  case "$a" in
    --quick) QUICK=1 ;;
    -*) usage ;;
    *) [ -z "$IMAGE" ] || usage; IMAGE="$a" ;;
  esac
done
[ -n "$IMAGE" ] || usage
TEST_FILTER="${TEST_FILTER:-}"
# grep exits 2 on an invalid expression
rc=0; printf '' | grep -Eq -- "$TEST_FILTER" || rc=$?
[ "$rc" -le 1 ] || { echo "invalid TEST_FILTER: $TEST_FILTER" >&2; exit 2; }
command -v docker >/dev/null || { echo "docker not found" >&2; exit 2; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image not found: $IMAGE" >&2; exit 2; }

TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
for f in fake_flexgate.py stub_decode3.py unit/run_unit.py; do
  [ -f "$TESTS_DIR/$f" ] || { echo "missing $TESTS_DIR/$f" >&2; exit 2; }
done

# Git Bash: keep container paths (/app/...) as they are, and give docker Windows paths for
# the host side of bind mounts (see hp below).
export MSYS_NO_PATHCONV=1

PFX="flexgate-test-$$"
NET="$PFX-net"
REPVOL="$PFX-rep"
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
  docker volume rm "$REPVOL" >/dev/null 2>&1 || true
  # containers only read $T (read-only bind mounts): the files are the workstation user's
  [ ! -d "$T" ] || rm -rf "$T" 2>/dev/null || echo "WARNING: could not remove $T" >&2
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

wait_until() {   # wait_until <tries> <cmd...>: run cmd every 0.5 s until it succeeds
  local n=$1; shift
  while [ "$n" -gt 0 ]; do "$@" >/dev/null 2>&1 && return 0; sleep 0.5; n=$((n - 1)); done
  return 1
}

# Several checks share one container: its script runs commands with emit (EMIT), which
# prints "<key>.rc <exit code>" and "<key>.out <line>" for every line of the command's
# output. The setup stores them with facts <group> <output> in F: F[<group>.<key>.rc] is the
# exit code, F[<group>.<key>.out] the output lines; the tests read F directly (no subshells:
# forks are slow under Git Bash). F[<group>] holds the whole output, for error messages.
EMIT='emit() { k=$1; shift; o="$("$@" 2>&1)"; r=$?; echo "$k.rc $r"; [ -z "$o" ] || printf "%s\n" "$o" | sed "s/^/$k.out /"; }'
declare -A F=()
facts() {   # facts <group> <output>
  local line k v
  F[$1]="$2"
  while IFS= read -r line; do
    k="$1.${line%% *}" v="${line#* }"
    [ "$line" != "${line%% *}" ] || v=""
    case "$k" in
      *.out) F[$k]="${F[$k]:+${F[$k]}$'\n'}$v" ;;
      *) F[$k]="$v" ;;
    esac
  done <<< "$2"
}

PASSED=0 FAILED=0
run_test() {   # run_test <name> <function> [args...]
  local name="$1" fn="$2"; shift 2
  local out rc=0
  TESTNAME="${name//[^A-Za-z0-9]/-}" TESTNAME="${TESTNAME:0:40}"
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

# Start the entrypoint in the background: start_entry <name> [docker run arguments...];
# waits until the cron jobs are installed
start_entry() {
  local name="$1"
  shift
  docker run -d --name "$name" -v "$(hp "$E/settings.yml"):/app/settings.yml:ro" \
    -v "$(hp "$E/cron"):/app/cron_jobs:ro" "$@" "$IMAGE" >/dev/null || fail_t "could not start"
  wait_until 40 sh -c "docker logs '$name' 2>&1 | grep -q 'cron.d/ updated'" || fail_t "entrypoint did not finish: $(docker logs "$name" 2>&1 | tail -n 5)"
}

# ---- unit tests ----------------------------------------------------------------------

# Names of the unit tests, read from the test files: "unit: <module>: <docstring>", the
# names run_unit.py prints (it reports a test without a one-line docstring as failed)
unit_names() {
  local f m
  for f in "$TESTS_DIR"/unit/test_*.py; do
    m="${f##*/test_}" m="${m%.py}"
    sed -n '/^    def test_/{n;s/^ *"""\(.*\)"""$/unit: '"$m"': \1/p}' "$f"
  done
}

# The selected unit tests in one container; counts and prints their PASS/FAIL lines. A
# selected test without a result line counts as failed.
run_unit() {   # run_unit <name>...
  local out rc=0 line name nl=$'\n'
  out="$(docker run --rm --network none --user "$SU" --name "$PFX-unit" -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$(hp "$TESTS_DIR"):/tests:ro" --entrypoint python "$IMAGE" /tests/unit/run_unit.py "$@" 2>&1)" || rc=$?
  while IFS= read -r line; do
    case "$line" in
      "PASS: "*) PASSED=$((PASSED + 1)); echo "$line" ;;
      "FAIL: "*) FAILED=$((FAILED + 1)); echo "$line" ;;
      "    "*) echo "$line" ;;
    esac
  done <<< "$out"
  for name in "$@"; do
    case "$nl$out$nl" in
      *"${nl}PASS: $name$nl"* | *"${nl}FAIL: $name: "*) ;;
      *) FAILED=$((FAILED + 1))
         echo "FAIL: $name: no result from the unit test container (exit code $rc)"
         printf '%s\n' "$out" | tail -n 5 | sed 's/^/    /' ;;
    esac
  done
}

# ---- image contents ------------------------------------------------------------------
# One container (as the image's process user, root) collects everything; the tests check
# their part of its output (F[img.*]).

img_setup() {
  facts img "$(docker run --rm --network none --name "$PFX-img" --entrypoint sh "$IMAGE" -c "$EMIT"'
    for c in bash cron python sudo curl wget git nano unzip rsync; do emit "cmd_$c" command -v "$c"; done
    emit pipcheck python -m pip check
    emit imports python -c "import requests, bs4, pydantic, pydantic_settings, yaml"
    emit bash_n bash -n /entrypoint.sh
    emit entry_x test -x /entrypoint.sh
    for f in flexgate_scraper healthcheck decoding; do
      emit "py_$f" python -c "import ast, sys; ast.parse(open(sys.argv[1]).read())" "/app/$f.py"
    done
    emit id id dockeruser
    emit folders stat -c "%u:%g %a %n" /app/downloads /var/lib/flexgate_scraper
    emit sudoers sh -c "test ! -e /etc/sudoers && test ! -e /etc/sudoers.d"
    emit as_user /usr/local/bin/as-dockeruser id
    emit root_run python /app/flexgate_scraper.py
    emit root_check python /app/flexgate_scraper.py --check-settings
    emit root_written find /app/downloads /var/lib/flexgate_scraper -mindepth 1
  ' 2>&1)" || true
}
need_img() { [ -n "${F[img.id.rc]:-}" ] || fail_t "the image check container did not run: ${F[img]:-}"; }

t_has() { need_img; expect "command -v $1" "${F[img.cmd_$1.rc]:-}" "0"; }
t_lacks() { need_img; [ "${F[img.cmd_$1.rc]:-}" != 0 ] || fail_t "$1 is present: ${F[img.cmd_$1.out]:-}"; }
t_pip_check() { need_img; expect "pip check (${F[img.pipcheck.out]:-})" "${F[img.pipcheck.rc]:-}" "0"; }
t_python_imports() { need_img; expect "imports (${F[img.imports.out]:-})" "${F[img.imports.rc]:-}" "0"; }

t_syntax() {
  need_img
  expect "bash -n /entrypoint.sh (${F[img.bash_n.out]:-})" "${F[img.bash_n.rc]:-}" "0"
  expect "/entrypoint.sh executable" "${F[img.entry_x.rc]:-}" "0"
  local f
  for f in flexgate_scraper healthcheck decoding; do
    expect "syntax of $f.py (${F[img.py_$f.out]:-})" "${F[img.py_$f.rc]:-}" "0"
  done
}

t_user() {
  need_img
  expect "id $SU" "${F[img.id.out]:-}" "uid=30000(dockeruser) gid=30000(dockeruser) groups=30000(dockeruser)"
  expect "folders" "${F[img.folders.out]:-}" \
    "$(printf '%s\n' '30000:30000 755 /app/downloads' '30000:30000 755 /var/lib/flexgate_scraper')"
  expect "image process user (cron needs root)" "$(docker image inspect -f '{{.Config.User}}' "$IMAGE")" ""
  expect "sudoers configuration absent" "${F[img.sudoers.rc]:-}" "0"
  expect "as-dockeruser" "${F[img.as_user.out]:-}" "uid=30000(dockeruser) gid=30000(dockeruser) groups=30000(dockeruser)"
}

# A run as root exits 2 with the message and writes nothing; also --check-settings
t_root_refused() {
  need_img
  local k out
  for k in root_run root_check; do
    out="${F[img.$k.out]:-}"
    expect "exit code ($k)" "${F[img.$k.rc]:-}" "2"
    contains "log ($k)" "$out" "ERROR - Refusing to run as root: run the scraper as the scraper user dockeruser, e.g. docker exec -u dockeruser flexgate_scraper python /app/flexgate_scraper.py"
    lacks "log ($k)" "$out" "Downloading"
    lacks "log ($k)" "$out" "are valid"
  done
  expect "files written by the root runs" "${F[img.root_written.out]:-}" ""
}

# ---- entrypoint ----------------------------------------------------------------------

# One entrypoint container with valid settings and a docker healthcheck: it keeps running,
# installs the cron jobs and becomes healthy in the grace period (real start time of PID 1),
# also for docker exec as the scraper user; the settings check at the start writes no status
# file. Facts in F[ent.*].
entry_setup() {
  TESTNAME="entry"
  mkenv entry_ok
  local name
  name="$(cname)"
  facts ent "$( (
    start_entry "$name" --network none --health-cmd "python /app/healthcheck.py --max-age-minutes 130" \
      --health-interval 2s --health-retries 1
    wait_until 40 sh -c "docker inspect -f '{{.State.Health.Status}}' '$name' | grep -qx healthy" || true
    echo "running.out $(docker inspect -f '{{.State.Running}}' "$name")"
    echo "health.out $(docker inspect -f '{{.State.Health.Status}}' "$name")"
    docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' "$name" | sed 's/^/healthlog.out /'
    docker exec "$name" sh -c "$EMIT"'
      emit cronmode stat -c "%a %U" /etc/cron.d/cron_test
      emit cronfile cat /etc/cron.d/cron_test
      emit state ls -A /var/lib/flexgate_scraper'
    docker exec -u "$SU" "$name" sh -c "$EMIT"'; emit hc python /app/healthcheck.py'
    docker logs "$name" 2>&1 | sed 's/^/log.out /'
  ) 2>&1)"
  docker rm -f "$name" >/dev/null 2>&1 || true
}
need_ent() { [ -n "${F[ent.running.out]:-}" ] || fail_t "the entrypoint container did not start: ${F[ent]:-}"; }

t_entry_ok() {
  need_ent
  expect "container running" "${F[ent.running.out]:-}" "true"
  expect "cron file mode and owner" "${F[ent.cronmode.out]:-}" "644 root"
  contains "cron file" "${F[ent.cronfile.out]:-}" "/app/flexgate_scraper.py"
  lacks "log" "${F[ent.log.out]:-}" "WARNING"
  lacks "log" "${F[ent.log.out]:-}" "owner of"
}

t_docker_health() {
  need_ent
  expect "health status" "${F[ent.health.out]:-}" "healthy"
  contains "health log" "${F[ent.healthlog.out]:-}" "healthy: no run yet, container started 0 min ago (grace 130 min)"
  expect "/var/lib/flexgate_scraper after the settings check" "${F[ent.state.out]:-}" ""
  expect "healthcheck by docker exec as $SU: exit code" "${F[ent.hc.rc]:-}" "0"
  contains "healthcheck by docker exec as $SU" "${F[ent.hc.out]:-}" "healthy: no run yet, container started 0 min ago"
}

# The entrypoint refusing to start, four times in one container (each time: exit non-zero
# with the message, no cron file installed): empty settings file, no settings file (the image
# ships a template, so it is removed), settings the scraper user cannot read, invalid
# decoding settings. Facts in F[ref.*].
refuse_setup() {
  mkenv refuse
  mkdir -p "$E/cfg"
  cp "$E/settings.yml" "$E/cfg/valid.yml"
  { cat "$E/settings.yml"
    printf '%s\n' 'decoding:' '  enabled: true' '  decoder_path: /stub/decode3' '  postprocessing:' \
      '    enabled: true' '    comment_char: "%"' '    steps: [frobnicate]'
  } > "$E/cfg/bad.yml"
  facts ref "$(docker run --rm --network none --name "$PFX-refuse" -v "$(hp "$E/cron"):/app/cron_jobs:ro" \
    -v "$(hp "$E/cfg"):/cfg:ro" --entrypoint bash "$IMAGE" -c '
    try() {
      timeout 30 /entrypoint.sh > /tmp/o 2>&1; echo "$1.rc $?"; sed "s/^/$1.out /" /tmp/o
      if [ -e /etc/cron.d/cron_test ]; then echo "$1.cron yes"; rm -f /etc/cron.d/cron_test; else echo "$1.cron no"; fi
    }
    : > /app/settings.yml; try empty
    rm -f /app/settings.yml; try missing
    cp /cfg/valid.yml /app/settings.yml; chmod 0600 /app/settings.yml; try unreadable
    cp /cfg/bad.yml /app/settings.yml; chmod 0644 /app/settings.yml; try baddec
  ' 2>&1)" || true
}
entry_refused() {   # entry_refused <key> <message>...
  local k="$1" m
  shift
  [ -n "${F[ref.$k.rc]:-}" ] || fail_t "no result from the entrypoint container: ${F[ref]:-}"
  [ "${F[ref.$k.rc]:-}" != 0 ] || fail_t "exit code 0, expected non-zero: ${F[ref.$k.out]:-}"
  expect "cron file installed" "${F[ref.$k.cron]:-}" "no"
  for m in "$@"; do contains "log" "${F[ref.$k.out]:-}" "$m"; done
}
t_entry_empty_settings() { entry_refused empty "/app/settings.yml is missing or empty"; }
t_entry_missing_settings() { entry_refused missing "/app/settings.yml is missing or empty"; }
t_entry_unreadable_settings() { entry_refused unreadable "/app/settings.yml is not readable by the scraper user dockeruser"; }
t_entry_bad_decoding() { entry_refused baddec "unknown post-processing step(s) ['frobnicate']" "/app/settings.yml is invalid"; }

# A volume with files and folders from a version that ran as root: the first start hands
# them all to the scraper user and logs the count, a restart changes nothing
n_started() { docker logs "$1" 2>&1 | grep -c 'cron.d/ updated' || true; }
t_entry_repair() {
  mkenv entry_repair
  local name out left
  name="$(cname)"
  docker run -d --name "$name" --network none -v "$REPVOL:/app/downloads" -v "$(hp "$E/settings.yml"):/app/settings.yml:ro" \
    -v "$(hp "$E/cron"):/app/cron_jobs:ro" --entrypoint bash "$IMAGE" -c '
    [ -e /root/.filled ] || { cd /app/downloads && chown root:root . && mkdir -p X0001X/raw && echo a > X0001X/raw/a.f2p &&
      echo b > X0001X/log.txt && chown 30000:root X0001X/log.txt && touch /root/.filled; } || exit 9
    exec /entrypoint.sh' >/dev/null || fail_t "could not start"
  wait_until 40 sh -c "[ \"\$(docker logs '$name' 2>&1 | grep -c 'cron.d/ updated')\" -ge 1 ]" || fail_t "first start did not finish: $(docker logs "$name" 2>&1 | tail -n 5)"
  left="$(docker exec "$name" find /app/downloads \( ! -user 30000 -o ! -group 30000 \) -print)"
  docker restart -t 0 "$name" >/dev/null || fail_t "could not restart"
  wait_until 40 sh -c "[ \"\$(docker logs '$name' 2>&1 | grep -c 'cron.d/ updated')\" -ge 2 ]" || fail_t "restart did not finish: $(docker logs "$name" 2>&1 | tail -n 5)"
  out="$(docker logs "$name" 2>&1)"
  docker rm -f "$name" >/dev/null
  contains "first start" "$out" "/app/downloads: owner of 5 entries changed to dockeruser:dockeruser"
  expect "entries not owned by 30000:30000" "$left" ""
  expect "ownership changes logged (first start and restart)" "$(printf '%s\n' "$out" | grep -c 'owner of' || true)" "1"
  lacks "log" "$out" "WARNING"
}

# ---- fake FlexGate server on a docker network ----------------------------------------
# One server container, reachable as "flexgate" on a private --internal network, for the
# end-to-end runs and the cron job.

FG_OK=0 SRV="$T/srv"
fg_setup() {
  TESTNAME="fakefg"
  local d="$SRV/fg2lite/data"
  mkdir -p "$SRV/fg2lite/sw" "$d/A0001A/raw" "$d/A0001A/par" "$d/O0015O/raw" "$T/srv_code"
  cp "$TESTS_DIR/fake_flexgate.py" "$T/srv_code/"
  # A0001A: a top-level file, a file that is not listed in the settings, a parameter set and
  # a data file with metadata and a repeated header
  printf 'log\n' > "$d/A0001A/log.txt"
  printf 'not listed\n' > "$d/A0001A/other.txt"
  printf 'NO;TIME;#1(oC);#2(oC)\n' > "$d/A0001A/par/A0001A_P1.f2p"
  printf '%s\n' 'PARSET A0001A_P1.f2p' '<READOUT>' '1;28.10.2026 16:00:10;-10,87;-9,25' \
    'NO;TIME;#1(oC);#2(oC)' '2;28.10.2026 17:00:10;-10,92;-9,3' > "$d/A0001A/raw/A0001A_001.f2b"
  # O0015O: downloaded by the cron job
  printf 'o raw\n' > "$d/O0015O/raw/o1.f2p"
  docker network create --internal "$NET" >/dev/null 2>&1 || return 0
  docker run -d --network "$NET" --network-alias flexgate --name "$(cname srv)" \
    -v "$(hp "$SRV"):/srv:ro" -v "$(hp "$T/srv_code"):/code:ro" \
    --entrypoint python "$IMAGE" -u /code/fake_flexgate.py /srv 8080 >/dev/null 2>&1 || return 0
  wait_until 40 sh -c "docker logs '$(cname srv)' 2>&1 | grep -q 'listening'" && FG_OK=1
  return 0
}

# ---- end to end ----------------------------------------------------------------------
# One container as dockeruser with umask 077 and the stub decoder: two scraper runs (python
# directly, as the cron job does) against the fake server, then the healthcheck. Facts in
# F[e2e.*].

# Fields of the status file on one line (also checks the field set and the time formats)
ST_SUMMARY='
import json, re, sys
st = json.load(open(sys.argv[1]))
keys = ["version", "ok", "sources_ok", "sources_failed", "files_downloaded", "files_decoded",
        "decode_failed", "decode_waiting", "decode_skipped", "decode_error", "error"]
assert sorted(st) == sorted(keys + ["started", "finished"]), sorted(st)
for k in ("started", "finished"):
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", st[k]), (k, st[k])
assert st["started"] <= st["finished"], (st["started"], st["finished"])
print(" ".join(k + "=" + json.dumps(st[k], separators=(",", ":")) for k in keys))
'
e2e_setup() {
  [ "$FG_OK" -eq 1 ] || return 0
  local f="$T/e2e.yml"
  printf '%s\n' 'flexgate_browse_url: "http://flexgate:8080/fg2lite/sw/browse.php"' \
    'base_download_folder: "/app/downloads"' 'downloadable_extensions: [".f2p", ".f2b"]' 'sources:' \
    '  - serial: "A0001A"' '    logger_name: "SITE-01"' '    folders: [raw, par]' '    files: [log.txt]' \
    'decoding:' '  enabled: true' '  decoder_path: /home/dockeruser/stub/decode3' '  output_folder: decoded' \
    '  postprocessing:' '    enabled: true' '    comment_char: "%"' '    steps: [comment_metadata]' > "$f"
  facts e2e "$(docker run --rm --network "$NET" --name "$PFX-e2e" --user "$SU" -e ST_SUMMARY="$ST_SUMMARY" \
    -v "$(hp "$f"):/app/settings.yml:ro" -v "$(hp "$TESTS_DIR"):/tests:ro" --entrypoint sh "$IMAGE" -c "$EMIT"'
    s=/home/dockeruser/stub st=/var/lib/flexgate_scraper/status.json
    mkdir -p $s && cp /tests/stub_decode3.py $s/decode3 && chmod 0755 $s/decode3
    umask 077
    for run in run1 run2; do
      export STUB_DECODE3_CALLS=$s/calls-$run
      emit $run python /app/flexgate_scraper.py
      emit ${run}_calls sh -c "cat $STUB_DECODE3_CALLS 2>/dev/null; true"
      emit ${run}_files sh -c "cd /app/downloads && find . -mindepth 1 -printf \"%y %U:%G %m %P\n\" | LC_ALL=C sort"
      emit ${run}_status python -c "$ST_SUMMARY" $st
    done
    emit csv sh -c "tr -d \"\r\" < /app/downloads/A0001A_SITE-01/decoded/A0001A_001.csv"
    emit tmpleft sh -c "ls -A /tmp | grep -vx flexgate_scraper.lock"
    emit state ls -A /var/lib/flexgate_scraper
    emit stmeta stat -c "%u:%g %a" $st
    emit hc python /app/healthcheck.py
  ' 2>&1)" || true
}
need_e2e() {
  [ "$FG_OK" -eq 1 ] || fail_t "the fake FlexGate server did not start"
  [ -n "${F[e2e.run1.rc]:-}" ] || fail_t "the end-to-end container did not run: ${F[e2e]:-}"
}

t_e2e_first() {
  need_e2e
  local log
  log="${F[e2e.run1.out]:-}"
  expect "scraper exit code ($log)" "${F[e2e.run1.rc]:-}" "0"
  contains "log" "$log" "All files downloaded successfully."
  expect "number of downloads" "$(printf '%s\n' "$log" | grep -c 'Downloading ' || true)" "3"
  expect "decoder calls" "${F[e2e.run1_calls.out]:-}" "$(printf '%s\n' A0001A_P1.f2p A0001A_001.f2b)"
  contains "log" "$log" "Decoding A0001A_SITE-01: 1 decoded, 0 failed"
  lacks "log" "$log" "ERROR"
  contains "decoded CSV (CRs removed)" "${F[e2e.csv.out]:-}" "$(printf 'NO;TIME;#1(oC);#2(oC)\n%%<READOUT>\n1;')"
  expect "work folder left in /tmp" "${F[e2e.tmpleft.out]:-}" ""
}

t_e2e_second() {
  need_e2e
  local log
  log="${F[e2e.run2.out]:-}"
  expect "scraper exit code ($log)" "${F[e2e.run2.rc]:-}" "0"
  expect "number of downloads" "$(printf '%s\n' "$log" | grep -c 'Downloading ' || true)" "0"
  expect "decoder calls" "${F[e2e.run2_calls.out]:-}" ""
  lacks "log" "$log" "Decoding"
  expect "downloads after the second run" "${F[e2e.run2_files.out]:-}" "${F[e2e.run1_files.out]:-}"
}

t_e2e_files() {
  need_e2e
  expect "downloads (type, owner, mode, path)" "${F[e2e.run1_files.out]:-}" "$(printf '%s\n' \
    'd 30000:30000 755 A0001A_SITE-01' 'f 30000:30000 644 A0001A_SITE-01/log.txt' \
    'd 30000:30000 755 A0001A_SITE-01/raw' 'f 30000:30000 644 A0001A_SITE-01/raw/A0001A_001.f2b' \
    'd 30000:30000 755 A0001A_SITE-01/par' 'f 30000:30000 644 A0001A_SITE-01/par/A0001A_P1.f2p' \
    'd 30000:30000 755 A0001A_SITE-01/decoded' 'f 30000:30000 644 A0001A_SITE-01/decoded/A0001A_001.csv' | sorted)"
}

t_e2e_status() {
  need_e2e
  expect "status after the first run" "${F[e2e.run1_status.out]:-}" 'version=1 ok=true sources_ok=1 sources_failed=[] files_downloaded=3 files_decoded=1 decode_failed=0 decode_waiting=0 decode_skipped=0 decode_error=null error=null'
  expect "status after the second run" "${F[e2e.run2_status.out]:-}" 'version=1 ok=true sources_ok=1 sources_failed=[] files_downloaded=0 files_decoded=0 decode_failed=0 decode_waiting=0 decode_skipped=0 decode_error=null error=null'
  expect "status file owner and mode" "${F[e2e.stmeta.out]:-}" "30000:30000 644"
  expect "/var/lib/flexgate_scraper" "${F[e2e.state.out]:-}" "status.json"
  expect "healthcheck exit code (${F[e2e.hc.out]:-})" "${F[e2e.hc.rc]:-}" "0"
  contains "healthcheck" "${F[e2e.hc.out]:-}" "healthy: last run "
}

# ---- cron job ------------------------------------------------------------------------
# A cron job line in the production form (user field root, the root shell opens
# /proc/1/fd/1, the command runs through as-dockeruser) runs as uid 30000 and its output
# reaches the container log. A line without the privilege drop fails loudly in the log.
# The container starts before all other tests (an early setup) and the test waits for the
# next cron minute, until at most 130 s after the start.

CRON_NAME="$PFX-cron" CRON_START=0
cron_setup() {
  [ "$FG_OK" -eq 1 ] || return 0
  mkenv cron_job
  printf '%s\n' 'flexgate_browse_url: "http://flexgate:8080/fg2lite/sw/browse.php"' \
    'base_download_folder: "/app/downloads"' 'downloadable_extensions: [".f2p"]' \
    'sources:' '  - serial: "O0015O"' '    folders: raw' > "$E/settings.yml"
  printf '%s\n' 'SHELL=/bin/sh' \
    '* * * * * root /usr/local/bin/as-dockeruser sh -c '"'"'echo "cron job uid: $(id -u):$(id -g)"; exec /usr/local/bin/python /app/flexgate_scraper.py'"'"' > /proc/1/fd/1 2>&1' \
    '* * * * * root /usr/local/bin/python /app/flexgate_scraper.py --check-settings > /proc/1/fd/1 2>&1' \
    > "$E/cron/cron_test"
  CRON_START=$SECONDS
  docker run -d --name "$CRON_NAME" --network "$NET" -v "$(hp "$E/settings.yml"):/app/settings.yml:ro" \
    -v "$(hp "$E/cron"):/app/cron_jobs:ro" "$IMAGE" >/dev/null 2>&1 || true
}

t_cron_job() {
  [ "$FG_OK" -eq 1 ] || fail_t "the fake FlexGate server did not start"
  local out="" owner st hrc=0 hout
  while [ $((SECONDS - CRON_START)) -lt 130 ]; do
    out="$(docker logs "$CRON_NAME" 2>&1)" || fail_t "the cron container did not start: $out"
    case "$out" in *"Run finished"*) case "$out" in *"Refusing"*) break ;; esac ;; esac
    sleep 1
  done
  out="$(docker logs "$CRON_NAME" 2>&1)"
  owner="$(docker exec "$CRON_NAME" stat -c '%U:%G %a' /app/downloads/O0015O/raw/o1.f2p 2>&1 || true)"
  st="$(docker exec "$CRON_NAME" stat -c '%U:%G %a' /var/lib/flexgate_scraper/status.json 2>&1 || true)"
  hout="$(docker exec "$CRON_NAME" python /app/healthcheck.py 2>&1)" || hrc=$?
  docker rm -f "$CRON_NAME" >/dev/null
  contains "container log" "$out" "cron.d/ updated"
  expect "status file of the cron run" "$st" "dockeruser:dockeruser 644"
  expect "healthcheck exit code ($hout)" "$hrc" "0"
  contains "healthcheck" "$hout" "healthy: last run "
  contains "container log" "$out" "cron job uid: 30000:30000"
  contains "container log" "$out" "Run finished: 1 source(s) ok, 0 failed, 1 file(s) downloaded."
  contains "container log (line without the privilege drop)" "$out" "ERROR - Refusing to run as root"
  expect "downloaded file" "$owner" "dockeruser:dockeruser 644"
}

# ---- run -----------------------------------------------------------------------------
# The tests are registered first, then the selected ones run: the unit tests together in one
# container, then the container tests in this order.
#   needs <setup>              the tests registered after it need <setup>_setup ("" for none)
#   add_test [--slow] <name> <function> [args...]   (--quick skips --slow tests)
# A setup runs once, just before the first selected test that needs it, after the setups it
# depends on (setup_deps); an early setup (EARLY_SETUPS) runs before all tests when a selected
# test needs it. Setups that no selected test needs do not run.

setup_deps() {   # setup_deps <setup>: the setups that must run before it
  case "$1" in
    e2e|cron) echo "fg" ;;
  esac
}
EARLY_SETUPS="cron"   # the cron job test waits for the next cron minute: start it first

T_NAMES=() T_CMDS=() T_NEEDS=() T_SLOW=() NEED=""
needs() { NEED="$1"; }
add_test() {
  local slow=0
  if [ "$1" = --slow ]; then slow=1; shift; fi
  T_NAMES+=("$1") T_CMDS+=("${*:2}") T_NEEDS+=("$NEED") T_SLOW+=("$slow")
}

SETUPS_DONE=" "
run_setup() {   # run_setup <setup>: once, after its dependencies
  local dep
  case "$SETUPS_DONE" in *" $1 "*) return 0 ;; esac
  for dep in $(setup_deps "$1"); do run_setup "$dep"; done
  "${1}_setup"
  SETUPS_DONE="$SETUPS_DONE$1 "
}

needs ""
while IFS= read -r n; do add_test "$n" UNIT; done < <(unit_names)

needs img
for c in bash cron python; do add_test "image has $c" t_has "$c"; done
for c in sudo curl wget git nano unzip rsync; do add_test "image lacks $c" t_lacks "$c"; done
add_test "image pip check passes" t_pip_check
add_test "image python packages import" t_python_imports
add_test "image scripts have valid syntax and the entrypoint is executable" t_syntax
add_test "image has user dockeruser 30000:30000 owning the data folders, process user root" t_user
add_test "scraper refuses to run as root, exit 2, nothing written; also --check-settings" t_root_refused

needs entry
add_test "entrypoint with settings keeps running and installs cron jobs" t_entry_ok
add_test "entrypoint: docker health of a fresh container is healthy (PID 1 start time)" t_docker_health
needs refuse
add_test "entrypoint refuses to start with an empty settings file" t_entry_empty_settings
add_test "entrypoint refuses to start without a settings file" t_entry_missing_settings
add_test "entrypoint refuses to start with settings the scraper user cannot read" t_entry_unreadable_settings
add_test "entrypoint refuses to start with invalid decoding settings" t_entry_bad_decoding
needs ""
add_test "entrypoint hands root-owned downloads to dockeruser once, logs the count" t_entry_repair

needs e2e
add_test "e2e: first run as dockeruser downloads and decodes (stub decoder), exit 0" t_e2e_first
add_test "e2e: second run downloads and decodes nothing" t_e2e_second
add_test "e2e: files 30000:30000 0644, folders 0755, also with umask 077" t_e2e_files
add_test "e2e: status file 30000:30000 0644 with the run's counts, healthcheck healthy" t_e2e_status

needs cron
add_test --slow "cron job (root, as-dockeruser, > /proc/1/fd/1) runs as 30000, output in the container log" t_cron_job

# Selection: one grep over all names (line numbers of the matching names)
SELECTED=() SKIPPED=0
MATCHED=" $(printf '%s\n' "${T_NAMES[@]}" | grep -nE -- "$TEST_FILTER" | cut -d: -f1 | tr '\n' ' ' || true) "
for i in "${!T_NAMES[@]}"; do
  if [[ "$MATCHED" != *" $((i + 1)) "* ]] || { [ "$QUICK" -eq 1 ] && [ "${T_SLOW[i]}" -eq 1 ]; }; then
    SKIPPED=$((SKIPPED + 1))
  else
    SELECTED+=("$i")
  fi
done
[ "${#SELECTED[@]}" -gt 0 ] || { echo "no test selected (TEST_FILTER='$TEST_FILTER', quick=$QUICK)" >&2; exit 2; }

for s in $EARLY_SETUPS; do
  for i in "${SELECTED[@]}"; do
    if [ "${T_NEEDS[i]}" = "$s" ]; then run_setup "$s"; break; fi
  done
done

UNITS=()
for i in "${SELECTED[@]}"; do [ "${T_CMDS[i]}" != UNIT ] || UNITS+=("${T_NAMES[i]}"); done
[ "${#UNITS[@]}" -eq 0 ] || run_unit "${UNITS[@]}"

for i in "${SELECTED[@]}"; do
  [ "${T_CMDS[i]}" != UNIT ] || continue
  [ -z "${T_NEEDS[i]}" ] || run_setup "${T_NEEDS[i]}"
  read -ra cmd <<< "${T_CMDS[i]}"   # function and arguments; the arguments have no spaces
  run_test "${T_NAMES[i]}" "${cmd[@]}"
done

[ "$SKIPPED" -eq 0 ] || echo "SKIPPED: $SKIPPED test(s) (filter/quick)"
TOTAL=$((PASSED + FAILED))
if [ "$FAILED" -eq 0 ]; then
  echo "ALL $TOTAL TESTS PASSED"
  exit 0
fi
echo "$FAILED OF $TOTAL TESTS FAILED"
exit 1
