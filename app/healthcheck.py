"""Container healthcheck of the flexgate scraper, from the status file of the last run.

    python /app/healthcheck.py [--max-age-minutes N]       (default N: 130)

Exit 0 (healthy) when the last run finished less than N minutes ago and was ok (no failed
source, no decode failure, no unexpected error). Otherwise exit 1 (unhealthy). Either way one
line is printed, which ``docker inspect`` shows as the healthcheck output.

Grace after the container start: without a status file, the container is healthy for N
minutes after it started (the start time of PID 1) and unhealthy after that. A status file
from before the start (``docker restart`` keeps the container's files) is not counted as old
during those N minutes either; its ``ok`` still counts. So compose needs no ``start_period``.

The scraper (flexgate_scraper.py) writes the status file at the end of every run, see
``write_status``. Standard library only; runs as any user, also root (docker runs the
healthcheck as the image's process user). It does not import flexgate_scraper.py, so it
works without valid settings and without the scraper's dependencies.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

# Where the scraper keeps its state: in the container layer, not in the downloads volume, so
# backups never copy it.
STATUS_FILE = "/var/lib/flexgate_scraper/status.json"
# Test-only override of STATUS_FILE (used by both the scraper and this script)
STATUS_FILE_ENV = "FLEXGATE_SCRAPER_STATUS_FILE"
# Format version of the status file
STATUS_VERSION = 1
DEFAULT_MAX_AGE_MINUTES = 130
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def status_file_path() -> str:
    return os.environ.get(STATUS_FILE_ENV) or STATUS_FILE


def format_time(t: datetime) -> str:
    """UTC time as ISO 8601 with Z, e.g. 2026-11-07T12:20:03Z."""
    return t.astimezone(timezone.utc).strftime(TIME_FORMAT)


def parse_time(s: str) -> datetime:
    """Inverse of format_time; also accepts other ISO 8601 forms with a time zone."""
    if not isinstance(s, str):
        raise ValueError(f"not a time: {s!r}")
    t = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    if t.tzinfo is None:
        raise ValueError(f"time without a time zone: {s!r}")
    return t.astimezone(timezone.utc)


def container_start_time() -> datetime:
    """Start time of PID 1: its start in clock ticks after boot (/proc/1/stat field 22) plus
    the boot time (/proc/stat btime)."""
    with open("/proc/1/stat") as f:
        stat = f.read()
    # field 2 (comm) is in parentheses and may contain spaces: count fields after the last ")"
    ticks = int(stat[stat.rindex(")") + 2:].split()[19])
    with open("/proc/stat") as f:
        btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
    return datetime.fromtimestamp(btime + ticks / os.sysconf("SC_CLK_TCK"), timezone.utc)


def minutes(seconds: float) -> int:
    return int(max(seconds, 0) // 60)


def check(max_age: float, now: datetime, started: datetime, path: str):
    """Returns (healthy, message)."""
    since_start = (now - started).total_seconds()
    try:
        with open(path, "rb") as f:
            st = json.loads(f.read().decode("utf-8"))
    except FileNotFoundError:
        if since_start < max_age * 60:
            return True, (f"healthy: no run yet, container started {minutes(since_start)} min ago "
                          f"(grace {max_age:g} min)")
        return False, (f"unhealthy: no run since the container started {minutes(since_start)} min ago "
                       f"(max {max_age:g})")
    except (OSError, ValueError) as e:
        return False, f"unhealthy: status file unreadable: {path}: {e}"

    try:
        if not isinstance(st, dict) or st.get("version") != STATUS_VERSION:
            raise ValueError(f"not a status file of version {STATUS_VERSION}")
        finished = parse_time(st["finished"])
        ok = st["ok"]
        failed = list(st.get("sources_failed") or [])
        decode_failed = int(st.get("decode_failed") or 0)
    except (KeyError, TypeError, ValueError) as e:
        return False, f"unhealthy: status file unreadable: {path}: {type(e).__name__}: {e}"

    last = st["finished"]
    age = (now - finished).total_seconds()
    if age >= max_age * 60 and since_start >= max_age * 60:
        return False, f"unhealthy: last run finished {minutes(age)} min ago (max {max_age:g})"
    if st.get("error"):
        return False, f"unhealthy: last run {last} failed: {st['error']}"
    if failed:
        return False, f"unhealthy: last run {last} had {len(failed)} failed source(s): {', '.join(map(str, failed))}"
    if decode_failed:
        return False, f"unhealthy: {decode_failed} decode failure(s) in the last run ({last})"
    if st.get("decode_error"):
        return False, f"unhealthy: last run {last} could not decode: {st['decode_error']}"
    if ok is not True:
        return False, f"unhealthy: last run {last} was not ok"
    n_sources = int(st.get("sources_ok") or 0) + len(failed)
    return True, (f"healthy: last run {last}, {n_sources} sources, {st.get('files_downloaded', 0)} downloaded, "
                  f"{st.get('files_decoded', 0)} decoded")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Healthcheck of the flexgate scraper (status of the last run).")
    p.add_argument("--max-age-minutes", type=float, default=DEFAULT_MAX_AGE_MINUTES,
                   help=f"the last run must have finished less than this long ago (default {DEFAULT_MAX_AGE_MINUTES})")
    # Test-only: replace the clock and the container start time (ISO 8601, e.g. 2026-11-07T12:00:00Z)
    p.add_argument("--now", type=parse_time, help=argparse.SUPPRESS)
    p.add_argument("--started", type=parse_time, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.max_age_minutes <= 0:
        p.error("--max-age-minutes must be positive")

    now = args.now or datetime.now(timezone.utc)
    try:
        started = args.started or container_start_time()
    except (OSError, ValueError, IndexError, StopIteration) as e:
        print(f"unhealthy: cannot determine the container start time: {type(e).__name__}: {e}")
        return 1
    healthy, message = check(args.max_age_minutes, now, started, status_file_path())
    print(message)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
