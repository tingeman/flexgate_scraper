"""healthcheck.py: the verdict on a status file, with an injected clock and start time."""
import json

from support import ScraperTestCase, healthcheck

FINISHED = "2026-11-07T12:00:00Z"


class HealthcheckTest(ScraperTestCase):

    def write_status(self, **fields):
        st = {"version": 1, "started": "2026-11-07T11:59:00Z", "finished": FINISHED, "ok": True,
              "sources_ok": 2, "sources_failed": [], "files_downloaded": 3, "files_decoded": 1,
              "decode_failed": 0, "decode_error": None, "error": None}
        st.update(fields)
        self.status_path.write_text(json.dumps(st))

    def assert_hc(self, rc, start, *args):
        """Healthcheck exits rc and its one output line starts with start."""
        got_rc, out = self.healthcheck(*args)
        self.assertEqual(got_rc, rc, out)
        self.assertTrue(out.startswith(start), out)
        return out

    def test_healthy(self):
        """clean status file is healthy with the counts"""
        self.write_status()
        out = self.assert_hc(0, "healthy: last run 2026-11-07T12:00:00Z", "--now", "2026-11-07T12:01:00Z",
                             "--started", "2026-11-07T07:00:00Z")
        self.assertIn(", 2 sources, 3 downloaded, 1 decoded", out)

    def test_waiting_skipped_healthy(self):
        """decode_waiting and decode_skipped are reported but never unhealthy"""
        args = ("--now", "2026-11-07T12:01:00Z", "--started", "2026-11-07T07:00:00Z")
        self.write_status(decode_waiting=2, decode_skipped=4)
        out = self.assert_hc(0, "healthy: last run ", *args)
        self.assertTrue(out.endswith(", 1 decoded, 2 waiting for a parameter set, 4 skipped (parameter set missing)"),
                        out)
        self.write_status(decode_waiting=0, decode_skipped=4)
        out = self.assert_hc(0, "healthy: last run ", *args)
        self.assertTrue(out.endswith(", 1 decoded, 4 skipped (parameter set missing)"), out)
        # with a real failure the counts change nothing: unhealthy
        self.write_status(ok=False, decode_failed=1, decode_waiting=2, decode_skipped=4)
        self.assert_hc(1, f"unhealthy: 1 decode failure(s) in the last run ({FINISHED})", *args)
        # values that are not numbers are ignored
        self.write_status(decode_waiting="many", decode_skipped=None)
        out = self.assert_hc(0, "healthy: last run ", *args)
        self.assertTrue(out.endswith(", 1 decoded"), out)

    def test_old_status_file(self):
        """a status file of an older version without decode_waiting and decode_skipped is read"""
        args = ("--now", "2026-11-07T12:01:00Z", "--started", "2026-11-07T07:00:00Z")
        self.write_status()
        st = json.loads(self.status_path.read_text())
        self.assertNotIn("decode_waiting", st)
        self.assertNotIn("decode_skipped", st)
        out = self.assert_hc(0, "healthy: last run ", *args)
        self.assertEqual(out, f"healthy: last run {FINISHED}, 2 sources, 3 downloaded, 1 decoded")

    def test_age(self):
        """unhealthy when the last run is older than the max age, grace after a restart"""
        self.write_status()
        self.assert_hc(0, "healthy: last run ", "--now", "2026-11-07T13:00:00Z", "--started", "2026-11-07T07:00:00Z")
        self.assert_hc(1, "unhealthy: last run finished 190 min ago (max 130)",
                       "--now", "2026-11-07T15:10:00Z", "--started", "2026-11-07T07:00:00Z")
        # restarted 40 min ago, the old status file is still there: grace
        self.assert_hc(0, "healthy: last run ", "--now", "2026-11-07T15:10:00Z", "--started", "2026-11-07T14:30:00Z")
        # a shorter max age
        self.assert_hc(1, "unhealthy: last run finished 60 min ago (max 30)", "--max-age-minutes", "30",
                       "--now", "2026-11-07T13:00:00Z", "--started", "2026-11-07T07:00:00Z")

    def test_no_status_file(self):
        """without a status file: healthy in the grace after the start, unhealthy after it"""
        args = ("--now", "2026-11-07T12:00:00Z", "--started")
        self.assert_hc(0, "healthy: no run yet, container started 60 min ago (grace 130 min)", *args, "2026-11-07T11:00:00Z")
        self.assert_hc(1, "unhealthy: no run since the container started 130 min ago (max 130)",
                       *args, "2026-11-07T09:50:00Z")

    def test_unreadable(self):
        """unreadable status file (not JSON, a folder, wrong version, bad time) is unhealthy"""
        args = ("--now", "2026-11-07T12:00:00Z", "--started", "2026-11-07T11:00:00Z")
        self.status_path.write_text("flexgate_browse_url: x\n")
        self.assert_hc(1, f"unhealthy: status file unreadable: {self.status_path}", *args)
        self.status_path.unlink()
        self.status_path.mkdir()
        self.assert_hc(1, f"unhealthy: status file unreadable: {self.status_path}", *args)
        self.status_path.rmdir()
        self.write_status(version=2)
        self.assert_hc(1, f"unhealthy: status file unreadable: {self.status_path}", *args)
        self.write_status(finished="yesterday")
        self.assert_hc(1, f"unhealthy: status file unreadable: {self.status_path}", *args)

    def test_reasons(self):
        """failed run, failed source, decode failure, decode error and not ok are unhealthy"""
        args = ("--now", "2026-11-07T12:01:00Z", "--started", "2026-11-07T07:00:00Z")
        cases = [
            ({"ok": False, "error": "RuntimeError: boom"}, f"unhealthy: last run {FINISHED} failed: RuntimeError: boom"),
            ({"ok": False, "sources_ok": 1, "sources_failed": ["H0008H"]},
             f"unhealthy: last run {FINISHED} had 1 failed source(s): H0008H"),
            ({"ok": False, "decode_failed": 1}, f"unhealthy: 1 decode failure(s) in the last run ({FINISHED})"),
            ({"ok": False, "decode_error": "decoder /x is missing or not executable"},
             f"unhealthy: last run {FINISHED} could not decode: decoder /x is missing or not executable"),
            ({"ok": False}, f"unhealthy: last run {FINISHED} was not ok"),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.write_status(**fields)
                out = self.assert_hc(1, expected, *args)
                self.assertEqual(out, expected)

    def test_check(self):
        """check() returns (healthy, message) for an injected clock and start time"""
        self.write_status()
        t = healthcheck.parse_time
        ok, msg = healthcheck.check(130, t("2026-11-07T12:30:00Z"), t("2026-11-07T07:00:00Z"), str(self.status_path))
        self.assertEqual((ok, msg), (True, "healthy: last run 2026-11-07T12:00:00Z, 2 sources, 3 downloaded, 1 decoded"))
        ok, msg = healthcheck.check(130, t("2026-11-07T12:30:00Z"), t("2026-11-07T07:00:00Z"), str(self.tmp / "none"))
        self.assertEqual((ok, msg), (False, "unhealthy: no run since the container started 330 min ago (max 130)"))

    def test_bad_max_age(self):
        """--max-age-minutes must be positive"""
        with self.assertRaises(SystemExit) as cm:
            healthcheck.main(["--max-age-minutes", "0"])
        self.assertEqual(cm.exception.code, 2)
