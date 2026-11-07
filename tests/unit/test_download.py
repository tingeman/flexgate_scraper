"""Downloads against the fake FlexGate server: folders, top-level files, failing and stalling
sources, partial downloads, file modes and the run lock."""
import fcntl
import os
import stat
import time
from pathlib import Path
from unittest import mock

from support import ScraperTestCase, capture_logs, fs, listing, n_downloads


class DownloadTest(ScraperTestCase):

    # ---- folders and files of a source ------------------------------------------------

    def two_loggers(self):
        """A0001A (logger SITE-01: top-level files, raw and par) and B0002B (raw)."""
        self.server_file("A0001A/log.txt", "log\n")
        self.server_file("A0001A/state.dat", "state\n")
        self.server_file("A0001A/other.txt", "not listed\n")
        self.server_file("A0001A/raw/x1.f2p", "raw one\n")
        self.server_file("A0001A/raw/x2.f2b", "raw two\n")
        self.server_file("A0001A/raw/notes.csv", "a,b\n")
        self.server_file("A0001A/par/p.par", "par\n")
        self.server_file("B0002B/raw/y1.f2p", "other logger\n")
        self.use_settings([
            {"serial": "A0001A", "logger_name": "SITE-01", "folders": ["raw", "par"], "files": ["log.txt", "state.dat"]},
            {"serial": "B0002B", "folders": "raw"},
        ], extensions=[".f2p", ".f2b", ".par"])

    FIRST_RUN = sorted([
        "d A0001A_SITE-01", "f A0001A_SITE-01/log.txt", "f A0001A_SITE-01/state.dat",
        "d A0001A_SITE-01/raw", "f A0001A_SITE-01/raw/x1.f2p", "f A0001A_SITE-01/raw/x2.f2b",
        "d A0001A_SITE-01/par", "f A0001A_SITE-01/par/p.par",
        "d B0002B", "d B0002B/raw", "f B0002B/raw/y1.f2p"])

    def test_first_run(self):
        """first run downloads listed files and folders into <serial>_<logger_name>/<folder>"""
        self.two_loggers()
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertIn("All files downloaded successfully.", log)
        self.assertEqual(listing(self.downloads), self.FIRST_RUN)
        self.assertEqual(n_downloads(log), 6)
        self.assertEqual((self.downloads / "A0001A_SITE-01/raw/x1.f2p").read_text(), "raw one\n")
        self.assertEqual((self.downloads / "A0001A_SITE-01/log.txt").read_text(), "log\n")

    def test_second_run(self):
        """second run downloads nothing and logs no skips at INFO"""
        self.two_loggers()
        self.run_main()
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertEqual(n_downloads(log), 0)
        self.assertNotIn("Skipping", log)
        self.assertEqual(listing(self.downloads), self.FIRST_RUN)

    def test_new_file(self):
        """a file that appeared on the server is downloaded"""
        self.two_loggers()
        self.run_main()
        self.server_file("A0001A/raw/x3.f2p", "raw three\n")
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertEqual(n_downloads(log), 1)
        self.assertIn("data/A0001A/raw/x3.f2p", log)
        self.assertEqual((self.downloads / "A0001A_SITE-01/raw/x3.f2p").read_text(), "raw three\n")

    def test_no_decoding_block(self):
        """without a decoding block nothing is decoded"""
        self.two_loggers()
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        entries = "\n".join(listing(self.downloads))
        self.assertNotIn("decoded", entries)
        self.assertNotIn(".csv", entries)
        self.assertNotIn("Decod", log)

    # ---- top-level files ---------------------------------------------------------------

    OLD = 1577836800  # 2020-01-01 00:00:00 UTC

    def top_logger(self):
        """P0016P with top-level files log.txt and state.dat; first run done, mtimes set old."""
        self.server_file("P0016P/log.txt", "p log 1\n")
        self.server_file("P0016P/state.dat", "p state\n")
        self.server_file("P0016P/raw/p1.f2p", "p raw\n")
        self.server_dirs("P0016P/par")
        self.use_settings([{"serial": "P0016P", "folders": ["raw", "par"], "files": "log.txt,state.dat"}])
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertEqual(n_downloads(log), 3)
        for n in ("log.txt", "state.dat"):
            os.utime(self.downloads / "P0016P" / n, (self.OLD, self.OLD))

    def top(self, name):
        """(mtime, uid:gid, mode, content) of a local top-level file of P0016P."""
        p = self.downloads / "P0016P" / name
        s = p.stat()
        return s.st_mtime, f"{s.st_uid}:{s.st_gid}", f"{stat.S_IMODE(s.st_mode):o}", p.read_text()

    def assert_no_part(self):
        self.assertEqual([p for p in listing(self.downloads) if p.endswith(".part")], [])

    def test_top_changed(self):
        """top-level file with changed content replaces the file (0644), counted as downloaded"""
        self.top_logger()
        before = self.top("state.dat")
        self.server_file("P0016P/log.txt", "p log 2\n")
        rc, log = self.run_main(umask=0o077)
        self.assertEqual(rc, 0, log)
        self.assertEqual(n_downloads(log), 0)
        self.assertIn(f"INFO - Updated {self.downloads}/P0016P/log.txt", log)
        self.assertNotIn("Updated " + str(self.downloads / "P0016P/state.dat"), log)
        self.assertIn("Run finished: 1 source(s) ok, 0 failed, 1 file(s) downloaded.", log)
        self.assertEqual(self.status()["files_downloaded"], 1)
        mtime, owner, mode, content = self.top("log.txt")
        self.assertEqual((owner, mode, content), (self.own(), "644", "p log 2\n"))
        self.assertNotEqual(mtime, self.OLD)
        self.assertEqual(self.top("state.dat"), before)
        self.assert_no_part()

    def test_top_unchanged(self):
        """top-level file with unchanged content is fetched again but untouched (mtime)"""
        self.top_logger()
        before = self.top("log.txt"), self.top("state.dat")
        req = [self.requests_for(f"/fg2lite/data/P0016P/{n}") for n in ("log.txt", "state.dat")]
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertEqual(n_downloads(log), 0)
        self.assertNotIn("Updated", log)
        self.assertNotIn("Saved to", log)
        self.assertIn("Run finished: 1 source(s) ok, 0 failed, 0 file(s) downloaded.", log)
        self.assertEqual(self.status()["files_downloaded"], 0)
        self.assertEqual((self.top("log.txt"), self.top("state.dat")), before)
        self.assertEqual([self.requests_for(f"/fg2lite/data/P0016P/{n}") for n in ("log.txt", "state.dat")],
                         [req[0] + 1, req[1] + 1])

    def test_top_failed_refresh(self):
        """failed refresh of a top-level file (file or listing HTTP error) keeps the old file, exit 1"""
        self.top_logger()
        self.server_file("P0016P/log.txt", "p log 3\n")
        before = self.top("log.txt"), self.top("state.dat")
        for ctl, code in ((".fake_file_status", "503"), (".fake_status", "500")):
            with self.subTest(ctl=ctl):
                p = self.server_file(f"P0016P/{ctl}", code + "\n")
                rc, log = self.run_main()
                p.unlink()
                self.assertEqual(rc, 1, log)
                self.assertIn(f"ERROR - Source P0016P failed, continuing with the next source: HTTPError: {code}", log)
                self.assertNotIn("Updated", log)
                self.assertEqual((self.top("log.txt"), self.top("state.dat")), before)
                self.assert_no_part()

    def test_refresh_file_new(self):
        """refresh_file downloads a file that is not there yet"""
        url = self.start_server().replace("sw/browse.php", "data/R0001R/log.txt")
        self.server_file("R0001R/log.txt", "r\n")
        target = self.tmp / "log.txt"
        self.assertTrue(fs.refresh_file(url, target, (1, 1)))
        self.assertEqual(target.read_text(), "r\n")
        self.assertFalse(fs.refresh_file(url, target, (1, 1)))

    def test_write_file_error(self):
        """write_file: an error removes the .part file and keeps the existing file"""
        target = self.tmp / "data.f2b"
        target.write_text("old\n")
        with mock.patch.object(fs.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                fs.write_file(b"new\n", target)
        self.assertEqual(target.read_text(), "old\n")
        self.assertFalse(fs.part_path(target).exists())

    # ---- failing and stalling sources --------------------------------------------------

    def test_http500(self):
        """HTTP 500 on one logger, the next logger is downloaded and decoded, exit 1"""
        stub = self.stub_decoder()
        self.server_file("H0008H/raw/H0008H_001.f2b", "h\n")
        self.server_file("H0008H/.fake_status", "500\n")
        self.server_file("J0010J/par/J0010J_P1.f2p", "NO;TIME;#1(oC)\n")
        self.server_file("J0010J/raw/J0010J_001.f2b", "PARSET J0010J_P1.f2p\n1;28.10.2026 16:00:10;-1,5\n")
        self.use_settings([self.decode_source("H0008H"), self.decode_source("J0010J")],
                          decoding=self.decoding_block(stub))
        rc, log = self.run_main()
        self.assertEqual(rc, 1, log)
        self.assertIn("ERROR - Source H0008H failed, continuing with the next source: HTTPError: 500", log)
        self.assertEqual([e for e in listing(self.downloads / "H0008H") if e.startswith("f ")], [])
        self.assertEqual(listing(self.downloads / "J0010J"), sorted([
            "d raw", "f raw/J0010J_001.f2b", "d par", "f par/J0010J_P1.f2p", "d decoded", "f decoded/J0010J_001.csv"]))
        self.assertEqual(self.calls(), ["J0010J_P1.f2p", "J0010J_001.f2b"])
        self.assertIn("Decoding J0010J: 1 decoded, 0 failed", log)
        self.assertIn("Downloads incomplete: 1 source(s) failed: H0008H", log)
        self.assertIn("Run finished: 1 source(s) ok, 1 failed, 2 file(s) downloaded.", log)
        self.assertNotIn("All files downloaded successfully.", log)

    def test_stall(self):
        """stalled server fails only that source after the timeout, exit 1"""
        self.server_file("I0009I/raw/I0009I_001.f2b", "i\n")
        self.server_file("I0009I/.fake_delay", "3\n")
        self.server_file("L0012L/raw/l1.f2p", "l raw\n")
        self.server_dirs("L0012L/par")
        self.use_settings([{"serial": "I0009I", "folders": ["raw", "par"]},
                           {"serial": "L0012L", "folders": ["raw", "par"]}])
        os.environ[fs.TIMEOUT_ENV] = "0.3"
        start = time.monotonic()
        rc, log = self.run_main()
        elapsed = time.monotonic() - start
        self.assertEqual(rc, 1, log)
        self.assertIn("ERROR - Source I0009I failed, continuing with the next source: ReadTimeout", log)
        self.assertEqual([e for e in listing(self.downloads / "I0009I") if e.startswith("f ")], [])
        self.assertEqual(listing(self.downloads / "L0012L"), sorted(["d raw", "f raw/l1.f2p", "d par"]))
        self.assertIn("Run finished: 1 source(s) ok, 1 failed, 1 file(s) downloaded.", log)
        self.assertLess(elapsed, 2, "the run waited for the stalled server")

    def test_timeout_setting(self):
        """timeout override: seconds, connect,read, and invalid values"""
        for value, expected in (("", fs.DEFAULT_TIMEOUT), ("5", (5.0, 5.0)), ("2,30", (2.0, 30.0))):
            os.environ[fs.TIMEOUT_ENV] = value
            self.assertEqual(fs.request_timeout(), expected)
        for value in ("soon", "0", "1,2,3", "-1,5"):
            os.environ[fs.TIMEOUT_ENV] = value
            with self.assertRaises(SystemExit) as cm:
                fs.request_timeout()
            self.assertEqual(cm.exception.code, 2, value)

    def test_bad_timeout(self):
        """invalid timeout override is rejected with exit 2, nothing downloaded"""
        self.server_file("M0013M/raw/m1.f2p", "m raw\n")
        self.use_settings([{"serial": "M0013M", "folders": ["raw"]}])
        os.environ[fs.TIMEOUT_ENV] = "soon"
        with capture_logs() as buf, self.assertRaises(SystemExit) as cm:
            fs.main()
        log = buf.getvalue()
        self.assertEqual(cm.exception.code, 2, log)
        self.assertIn("ERROR - Invalid FLEXGATE_SCRAPER_TIMEOUT_SECONDS='soon'", log)
        self.assertNotIn("Downloading", log)
        self.assertFalse(self.downloads.exists())
        self.assertFalse(self.status_path.exists())

    # ---- partial downloads, modes ------------------------------------------------------

    def test_partial(self):
        """leftover .part files are removed and downloaded again, none left"""
        self.server_file("K0011K/log.txt", "k log\n")
        self.server_file("K0011K/raw/k1.f2p", "k raw\n")
        self.server_dirs("K0011K/par")
        (self.downloads / "K0011K/raw").mkdir(parents=True)
        (self.downloads / "K0011K/.log.txt.part").write_text("partial\n")
        (self.downloads / "K0011K/raw/.k1.f2p.part").write_text("partial\n")
        self.use_settings([{"serial": "K0011K", "folders": ["raw", "par"], "files": "log.txt"}])
        rc, log = self.run_main(umask=0o077)
        self.assertEqual(rc, 0, log)
        self.assertIn(f"Removing partial download {self.downloads}/K0011K/.log.txt.part", log)
        self.assertIn(f"Removing partial download {self.downloads}/K0011K/raw/.k1.f2p.part", log)
        self.assertEqual(n_downloads(log), 2)
        self.assertEqual(listing(self.downloads / "K0011K"), sorted(["f log.txt", "d raw", "f raw/k1.f2p", "d par"]))
        self.assertEqual((self.downloads / "K0011K/log.txt").read_text(), "k log\n")
        self.assertEqual((self.downloads / "K0011K/raw/k1.f2p").read_text(), "k raw\n")
        self.assert_no_part()

    def test_modes(self):
        """files the scraper creates are 0644 and folders 0755 (own user), also with umask 077"""
        self.server_file("N0014N/log.txt", "n log\n")
        self.server_file("N0014N/raw/n1.f2p", "n raw\n")
        self.server_file("N0014N/par/N0014N_P1.f2p", "NO;TIME\n")
        self.use_settings([{"serial": "N0014N", "folders": ["raw", "par"], "files": "log.txt"}])
        rc, log = self.run_main(umask=0o077)
        self.assertEqual(rc, 0, log)
        entries = []
        for e in listing(self.downloads):
            s = (self.downloads / e[2:]).stat()
            entries.append(f"{e[0]} {s.st_uid}:{s.st_gid} {stat.S_IMODE(s.st_mode):o} {e[2:]}")
        o = self.own()
        self.assertEqual(sorted(entries), sorted([
            f"d {o} 755 N0014N", f"f {o} 644 N0014N/log.txt", f"d {o} 755 N0014N/raw",
            f"f {o} 644 N0014N/raw/n1.f2p", f"d {o} 755 N0014N/par", f"f {o} 644 N0014N/par/N0014N_P1.f2p"]))

    # ---- run lock ----------------------------------------------------------------------

    def hold_lock(self):
        fd = os.open(fs.LOCK_PATH, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def test_lock(self):
        """a run while the lock is held exits 0 with a WARNING and downloads nothing"""
        self.server_file("M0013M/raw/m1.f2p", "m raw\n")
        self.use_settings([{"serial": "M0013M", "folders": ["raw"]}])
        fd = self.hold_lock()
        try:
            rc, log = self.run_main()
        finally:
            os.close(fd)
        self.assertEqual(rc, 0, log)
        self.assertIn(f"WARNING - Lock {fs.LOCK_PATH} is held: another run is in progress, skipping this run.", log)
        self.assertNotIn("Downloading", log)
        self.assertFalse((self.downloads / "M0013M").exists())
        # without the holder the same settings download the file
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertEqual(n_downloads(log), 1)

    def test_lock_read_only_file(self):
        """the run lock works on an existing read-only lock file"""
        Path(fs.LOCK_PATH).touch()
        os.chmod(fs.LOCK_PATH, 0o444)
        fd = fs.acquire_run_lock()
        self.assertIsNotNone(fd)
        self.assertIsNone(fs.acquire_run_lock())
        os.close(fd)
