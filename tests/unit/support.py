"""Shared helpers of the unit tests (tests/unit/test_*.py).

The unit tests run inside the built image, as the scraper user, in one container (see
run_unit.py and tests/run.sh); the app is imported from /app (FLEXGATE_APP_DIR overrides
it). Every test gets its own temporary folder with its own downloads, status file, run lock,
decoder work folder (tempfile.tempdir) and, when it asks for one, its own fake FlexGate
server (tests/fake_flexgate.py) on 127.0.0.1 in a thread. No test depends on another.
"""
import contextlib
import io
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(os.environ.get("FLEXGATE_APP_DIR", "/app"))
for p in (str(TESTS_DIR), str(APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml  # noqa: E402

import decoding  # noqa: E402,F401
import fake_flexgate  # noqa: E402
import flexgate_scraper as fs  # noqa: E402
import healthcheck  # noqa: E402

# The format of the scraper's log lines without the time (production: "<time> - " first)
LOG_FORMAT = "%(levelname)s - %(message)s"
STATUS_KEYS = ["version", "ok", "sources_ok", "sources_failed", "files_downloaded", "files_decoded",
               "decode_failed", "decode_waiting", "decode_skipped", "decode_error", "error"]


@contextlib.contextmanager
def capture_logs():
    """Log lines of the scraper ("<LEVEL> - <message>") at the production level (INFO), as a
    StringIO; no other log handler gets them meanwhile."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    try:
        yield buf
    finally:
        root.handlers, level = saved
        root.setLevel(level)


def listing(folder: Path):
    """Entries under folder as "<d|f> <relative path>", sorted."""
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            out.append(f"{'d' if p.is_dir() else 'f'} {p.relative_to(folder).as_posix()}")
    return sorted(out)


def n_downloads(log: str) -> int:
    return log.count("Downloading ")


class ScraperTestCase(unittest.TestCase):
    """Base class: an isolated environment per test (see the module docstring)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flexgate_unit_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.downloads = self.tmp / "downloads"
        self.state = self.tmp / "state"          # stands in for /var/lib/flexgate_scraper
        self.status_path = self.state / "status.json"
        self.work_tmp = self.tmp / "worktmp"      # the decoder's work folders go here
        self.srv = self.tmp / "srv"               # root of the fake FlexGate server
        for d in (self.state, self.work_tmp, self.srv / "fg2lite" / "sw"):
            d.mkdir(parents=True)
        umask = os.umask(0o022)
        self.addCleanup(os.umask, umask)
        env = {k: v for k, v in os.environ.items()
               if k not in (fs.TIMEOUT_ENV, "STUB_DECODE3_CALLS", "STUB_DECODE3_KEEP")}
        env[healthcheck.STATUS_FILE_ENV] = str(self.status_path)
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(fs, "LOCK_PATH", str(self.tmp / "flexgate_scraper.lock")),
                        mock.patch.object(fs, "settings", None),
                        mock.patch.object(tempfile, "tempdir", str(self.work_tmp))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = None
        self.calls_file = self.tmp / "stub_calls.log"
        self.keep = self.tmp / "keep"

    # ---- fake FlexGate server --------------------------------------------------------

    def start_server(self):
        if self.server is None:
            self.server = fake_flexgate.make_server(self.srv, 0, "127.0.0.1")
            t = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            t.start()
            self.addCleanup(self.server.server_close)
            self.addCleanup(self.server.shutdown)
        return f"http://127.0.0.1:{self.server.server_port}/fg2lite/sw/browse.php"

    def server_file(self, rel: str, content: str = "x\n"):
        """Write <server root>/fg2lite/data/<rel>."""
        p = self.srv / "fg2lite" / "data" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def server_dirs(self, *rels: str):
        for rel in rels:
            (self.srv / "fg2lite" / "data" / rel).mkdir(parents=True, exist_ok=True)

    def requests_for(self, path: str) -> int:
        return self.server.requests.count(path)

    # ---- settings and runs -----------------------------------------------------------

    def settings_dict(self, sources, extensions=(".f2p", ".f2b"), decoding=None) -> dict:
        d = {"flexgate_browse_url": self.start_server(), "base_download_folder": str(self.downloads),
             "downloadable_extensions": list(extensions), "sources": sources}
        if decoding is not None:
            d["decoding"] = decoding
        return d

    def write_settings(self, d: dict, name: str = "settings.yml") -> Path:
        p = self.tmp / name
        p.write_text(yaml.safe_dump(d, sort_keys=False))
        return p

    def use_settings(self, sources, **kw):
        """Write settings (sources: list of source dicts), load them with load_settings and
        make them the scraper's settings."""
        fs.settings = fs.load_settings(self.write_settings(self.settings_dict(sources, **kw)))

    def run_main(self, umask=None):
        """One scraper run (main()); returns (exit code, log). Also checks that the decoder
        work folder was removed and that only the status file is in the state folder."""
        if umask is not None:
            os.umask(umask)
        with capture_logs() as buf:
            rc = fs.main()
        log = buf.getvalue()
        self.assertEqual(sorted(os.listdir(self.work_tmp)), [], f"work folder left: {log}")
        self.assertEqual([n for n in os.listdir(self.state) if n != "status.json"], [], "files left in the state folder")
        return rc, log

    # ---- stub decoder ----------------------------------------------------------------

    def stub_decoder(self) -> str:
        """Install tests/stub_decode3.py as an executable decoder; its calls are logged to
        self.calls_file (see calls()) and its CSVs kept in self.keep."""
        d = self.tmp / "stub"
        d.mkdir(exist_ok=True)
        dst = d / "decode3"
        shutil.copyfile(TESTS_DIR / "stub_decode3.py", dst)
        dst.chmod(0o755)
        os.environ["STUB_DECODE3_CALLS"] = str(self.calls_file)
        os.environ["STUB_DECODE3_KEEP"] = str(self.keep)
        return str(dst)

    def calls(self):
        """The inputs the stub decoder was called with since the last calls(), in order."""
        if not self.calls_file.exists():
            return []
        names = self.calls_file.read_text().split()
        self.calls_file.unlink()
        return names

    @staticmethod
    def decoding_block(decoder_path: str, timeout=60, comment_char="%") -> dict:
        """comment_char None: post-processing disabled."""
        pp = ({"enabled": False, "comment_char": "%", "steps": ["comment_metadata"]} if comment_char is None
              else {"enabled": True, "comment_char": comment_char, "steps": ["comment_metadata"]})
        return {"enabled": True, "decoder_path": decoder_path, "output_folder": "decoded",
                "timeout_seconds": timeout, "postprocessing": pp}

    @staticmethod
    def decode_source(serial: str, logger_name: str = None) -> dict:
        s = {"serial": serial, "folders": ["raw", "par"]}
        if logger_name:
            s["logger_name"] = logger_name
        return s

    # ---- status file and healthcheck -------------------------------------------------

    def status(self) -> dict:
        return json.loads(self.status_path.read_text())

    def status_summary(self) -> str:
        """Fields of the status file on one line (also checks the field set and the time
        formats), then "meta=<uid>:<gid> <mode>" of the file."""
        st = self.status()
        self.assertEqual(sorted(st), sorted(STATUS_KEYS + ["started", "finished"]))
        for k in ("started", "finished"):
            self.assertRegex(st[k], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertLessEqual(st["started"], st["finished"])
        s = os.stat(self.status_path)
        return " ".join(k + "=" + json.dumps(st[k], separators=(",", ":")) for k in STATUS_KEYS) + \
            f" meta={s.st_uid}:{s.st_gid} {stat.S_IMODE(s.st_mode):o}"

    def own(self) -> str:
        return f"{os.getuid()}:{os.getgid()}"

    def healthcheck(self, *args):
        """healthcheck.main(args); returns (exit code, output), output must be one line."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = healthcheck.main(list(args))
        text = out.getvalue()
        self.assertEqual(text.count("\n"), 1, f"healthcheck output lines: {text!r}")
        return rc, text.strip()

    def healthcheck_rel(self, now_min: float, started_min: float):
        """Healthcheck (max age 130 min) with --now and --started in minutes after the status
        file's "finished" (negative: before)."""
        finished = healthcheck.parse_time(self.status()["finished"])
        t = lambda m: healthcheck.format_time(finished + timedelta(minutes=m))  # noqa: E731
        return self.healthcheck("--max-age-minutes", "130", "--now", t(now_min), "--started", t(started_min))

    def healthcheck_now(self):
        """Healthcheck one minute after the last run, container started long before."""
        return self.healthcheck_rel(1, -300)

