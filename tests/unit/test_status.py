"""The status file a run writes, and the healthcheck's verdict on it."""
import fcntl
import os
import time
from unittest import mock

from support import ScraperTestCase, capture_logs, fs, listing


class StatusTest(ScraperTestCase):

    def c_logger(self):
        """C0003C (logger SITE-03): a parameter set and two data files, stub decoder."""
        self.server_file("C0003C/par/C0003C_P1.f2p", "NO;TIME;#1(oC)\n")
        self.server_file("C0003C/raw/C0003C_001.f2b", "PARSET C0003C_P1.f2p\n1;28.10.2026 16:00:10;-10,87\n")
        self.server_file("C0003C/raw/C0003C_002.f2b", "PARSET C0003C_P1.f2p\n2;28.10.2026 17:00:10;-10,92\n")
        self.use_settings([self.decode_source("C0003C", "SITE-03")], decoding=self.decoding_block(self.stub_decoder()))

    def test_clean(self):
        """clean run writes the status file: ok true, counts, own user, 0644; healthy"""
        self.c_logger()
        rc, log = self.run_main(umask=0o077)
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.status_summary(), "version=1 ok=true sources_ok=1 sources_failed=[] files_downloaded=3 "
                         "files_decoded=2 decode_failed=0 decode_waiting=0 decode_skipped=0 decode_error=null "
                         f"error=null meta={self.own()} 644")
        self.assertEqual([e for e in listing(self.downloads) if "status" in e or e.endswith(".json")], [])
        rc, out = self.healthcheck_now()
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith("healthy: last run "), out)
        self.assertIn(", 1 sources, 3 downloaded, 2 decoded", out)

    def test_failed_source(self):
        """failed source gives ok false with its name; unhealthy with the reason"""
        self.server_file("H0008H/raw/H0008H_001.f2b", "h\n")
        self.server_file("H0008H/.fake_status", "500\n")
        self.server_file("J0010J/par/J0010J_P1.f2p", "NO;TIME;#1(oC)\n")
        self.server_file("J0010J/raw/J0010J_001.f2b", "PARSET J0010J_P1.f2p\n1;28.10.2026 16:00:10;-1,5\n")
        self.use_settings([self.decode_source("H0008H"), self.decode_source("J0010J")],
                          decoding=self.decoding_block(self.stub_decoder()))
        rc, log = self.run_main()
        self.assertEqual(rc, 1, log)
        self.assertEqual(self.status_summary(), 'version=1 ok=false sources_ok=1 sources_failed=["H0008H"] '
                         'files_downloaded=2 files_decoded=1 decode_failed=0 decode_waiting=0 decode_skipped=0 '
                         f'decode_error=null error=null meta={self.own()} 644')
        rc, out = self.healthcheck_now()
        self.assertEqual(rc, 1, out)
        self.assertIn("had 1 failed source(s): H0008H", out)
        self.assertTrue(out.startswith("unhealthy: last run 20"), out)

    def test_decode_failure(self):
        """decode failure gives ok false, decode_failed 1; unhealthy"""
        self.server_file("D0004D/raw/D0004D_001.f2b", "STUB:exit255\nPARSET D0004D_P1.f2p\n1;28.10.2026 16:00:10;-1,5\n")
        self.server_dirs("D0004D/par")
        self.use_settings([self.decode_source("D0004D")], decoding=self.decoding_block(self.stub_decoder()))
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.status_summary(), "version=1 ok=false sources_ok=1 sources_failed=[] files_downloaded=1 "
                         "files_decoded=0 decode_failed=1 decode_waiting=0 decode_skipped=0 decode_error=null "
                         f"error=null meta={self.own()} 644")
        rc, out = self.healthcheck_now()
        self.assertEqual(rc, 1, out)
        self.assertTrue(out.startswith("unhealthy: 1 decode failure(s) in the last run ("), out)

    def test_waiting_and_skipped(self):
        """waiting and skipped data files are counted, ok true; healthy with the counts"""
        self.server_file("D0004D/raw/D0004D_001.f2b", "PARSET D0004D_P1.f2p\n1;28.10.2026 16:00:10;-1,5\n")
        self.server_file("D0004D/raw/D0004D_002.f2b", "PARSET D0004D_P2.f2p\n1;28.10.2026 17:00:10;-1,5\n")
        self.server_dirs("D0004D/par")
        self.use_settings([self.decode_source("D0004D")], decoding=self.decoding_block(self.stub_decoder()))
        # D0004D_001.f2b was downloaded 10 days ago (default: skipped after 7 days)
        raw = self.downloads / "D0004D/raw"
        raw.mkdir(parents=True)
        (raw / "D0004D_001.f2b").write_text("PARSET D0004D_P1.f2p\n1;28.10.2026 16:00:10;-1,5\n")
        old = time.time() - 10 * 86400
        os.utime(raw / "D0004D_001.f2b", (old, old))
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.status_summary(), "version=1 ok=true sources_ok=1 sources_failed=[] files_downloaded=1 "
                         "files_decoded=0 decode_failed=0 decode_waiting=1 decode_skipped=1 decode_error=null "
                         f"error=null meta={self.own()} 644")
        rc, out = self.healthcheck_now()
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.endswith(", 1 sources, 1 downloaded, 0 decoded, 1 waiting for a parameter set, "
                                     "1 skipped (parameter set missing)"), out)
        # next run: the skipped file stays skipped (and counted), the other one still waits
        rc, log = self.run_main()
        st = self.status()
        self.assertEqual((st["ok"], st["decode_waiting"], st["decode_skipped"], st["decode_failed"]),
                         (True, 1, 1, 0), log)

    def test_no_decoder(self):
        """missing decoder gives ok false with decode_error; unhealthy"""
        self.server_file("G0007G/raw/G0007G_001.f2b", "PARSET G0007G_P1.f2p\n1;x;1\n")
        self.server_dirs("G0007G/par")
        self.use_settings([self.decode_source("G0007G")], decoding=self.decoding_block("/opt/nothere/decode3"))
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.status_summary(), "version=1 ok=false sources_ok=1 sources_failed=[] files_downloaded=1 "
                         'files_decoded=0 decode_failed=0 decode_waiting=0 decode_skipped=0 decode_error="decoder '
                         f'/opt/nothere/decode3 is missing or not executable" error=null meta={self.own()} 644')
        rc, out = self.healthcheck_now()
        self.assertEqual(rc, 1, out)
        self.assertIn("could not decode: decoder /opt/nothere/decode3 is missing or not executable", out)

    def test_exception(self):
        """run ending with an unexpected exception writes ok false with the error; unhealthy"""
        self.server_dirs("A0001A/raw")
        self.use_settings([{"serial": "A0001A", "folders": "raw"}])

        def boom(*args):
            raise RuntimeError("boom")
        with mock.patch.object(fs, "scrape_source", boom), capture_logs(), self.assertRaises(RuntimeError):
            fs.main()
        st = self.status()
        self.assertEqual((st["ok"], st["error"]), (False, "RuntimeError: boom"))
        rc, out = self.healthcheck_now()
        self.assertEqual(rc, 1, out)
        self.assertIn("failed: RuntimeError: boom", out)
        # the lock was released: the next run runs
        self.assertIsNotNone(fd := fs.acquire_run_lock())
        os.close(fd)

    def test_lock_skipped(self):
        """run skipped by the lock leaves the status file alone"""
        self.server_file("M0013M/raw/m1.f2p", "m raw\n")
        self.use_settings([{"serial": "M0013M", "folders": ["raw"]}])
        self.status_path.write_text("earlier\n")
        fd = os.open(fs.LOCK_PATH, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            rc, log = self.run_main()
        finally:
            os.close(fd)
        self.assertEqual(rc, 0, log)
        self.assertIn("another run is in progress, skipping this run.", log)
        self.assertEqual(os.listdir(self.state), ["status.json"])
        self.assertEqual(self.status_path.read_text(), "earlier\n")

    def test_unwritable(self):
        """unwritable status file logs an ERROR, exit code unchanged"""
        self.server_file("M0013M/raw/m1.f2p", "m raw\n")
        self.use_settings([{"serial": "M0013M", "folders": ["raw"]}])
        os.environ["FLEXGATE_SCRAPER_STATUS_FILE"] = "/nonexistent/x/status.json"
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertIn("ERROR - Could not write the status file /nonexistent/x/status.json", log)
        self.assertIn("Run finished", log)
