"""Data files whose parameter set is missing: waiting, skipped (decoded/skipped.txt) and
retried, with the stub decoder (tests/stub_decode3.py), through whole runs."""
import os
import stat
import time
from unittest import mock

from support import ScraperTestCase, decoding, fs, listing

DAY = 86400
HEADER = "NO;TIME;#1(oC);#2(oC)"


def data(parset: str, extra: str = "") -> str:
    return f"{extra}PARSET {parset}\n1;28.10.2026 16:00:10;-1,5;-2,5\n"


class SkippedTest(ScraperTestCase):

    def setUp(self):
        super().setUp()
        self.folder = self.downloads / "D0004D"
        self.skipped_txt = self.folder / "decoded" / "skipped.txt"
        self.server_dirs("D0004D/raw", "D0004D/par")

    def settings(self, **decoding_extra):
        block = self.decoding_block(self.stub_decoder())
        block.update(decoding_extra)
        self.use_settings([self.decode_source("D0004D")], decoding=block)

    def local_raw(self, name: str, content: str, days_ago: float = 10):
        """A data file that was downloaded days_ago (already in raw/, so not downloaded again)."""
        p = self.folder / "raw" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        t = time.time() - days_ago * DAY
        os.utime(p, (t, t))
        return p

    def local_par(self, name: str, mtime: float, content: str = HEADER + "\n"):
        """A parameter set put into par/ by hand, with the given mtime."""
        p = self.folder / "par" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        os.utime(p, (mtime, mtime))
        return p

    def test_named_par_old_mtime(self):
        """the named parameter set copied into par/ with an old mtime retries the file: decoded, line removed"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.local_raw("D0004D_002.f2b", data("D0004D_P2.f2p"))
        self.settings()
        self.run_ok()
        self.calls()
        # copied with its original modification time (long before the skip time)
        self.local_par("D0004D_P1.f2p", time.time() - 400 * DAY)
        log = self.run_ok()
        self.assertEqual(self.calls(), ["D0004D_P1.f2p", "D0004D_001.f2b"])
        self.assertIn("INFO - Decoding D0004D/raw/D0004D_001.f2b: retrying, its parameter set D0004D_P1.f2p is now "
                      "in par/", log)
        self.assertNotIn("WARNING", log)
        self.assertIn("Decoding D0004D: 1 decoded, 0 failed", log)
        self.assertTrue((self.folder / "decoded/D0004D_001.csv").is_file())
        self.assertEqual([e[0] for e in self.entries()], ["D0004D_002.f2b"])
        self.run_ok()
        self.assertEqual(self.calls(), [])

    def test_named_par_not_accepted(self):
        """named parameter set in par/ but still reported missing: re-skipped once with a marker, not retried again"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.settings()
        self.run_ok()
        self.calls()
        # a parameter set the decoder cannot use (the stub fails on it and does not keep it)
        self.local_par("D0004D_P1.f2p", time.time() - 400 * DAY, "STUB:exit255\n")
        log = self.run_ok()
        self.assertEqual(self.calls(), ["D0004D_P1.f2p", "D0004D_001.f2b"])
        self.assertEqual(log.count("WARNING - Decoding D0004D/raw/"), 1, log)
        self.assertIn("WARNING - Decoding D0004D/raw/D0004D_001.f2b: parameter set D0004D_P1.f2p is in par/ but the "
                      "decoder still reports it missing; skipped, listed in decoded/skipped.txt", log)
        [(_, _, err)] = self.entries()
        self.assertTrue(err.endswith("D0004D_P1.f2p'> [present in par/ but not accepted by the decoder]"), err)
        before = self.skipped_txt.read_bytes()
        for _ in range(2):
            log = self.run_ok()
            self.assertEqual(self.calls(), [])
            self.assertNotIn("WARNING", log)
            self.assertIn("INFO - D0004D: 1 file skipped (parameter set missing)", log)
            self.assertEqual(self.skipped_txt.read_bytes(), before)
        # deleting the line still retries it (and it is skipped again with the marker)
        self.skipped_txt.write_text("")
        self.run_ok()
        self.assertEqual(self.calls(), ["D0004D_P1.f2p", "D0004D_001.f2b"])
        self.run_ok()
        self.assertEqual(self.calls(), [])

    def test_named_par_case_insensitive(self):
        """the named parameter set is found in par/ case-insensitively"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.settings()
        self.run_ok()
        self.calls()
        self.local_par("d0004d_p1.f2p", time.time() - 400 * DAY)
        log = self.run_ok()
        self.assertIn("INFO - Decoding D0004D/raw/D0004D_001.f2b: retrying, its parameter set D0004D_P1.f2p is now "
                      "in par/", log)
        self.assertEqual(self.calls(), ["d0004d_p1.f2p", "D0004D_001.f2b"])
        # the stub (like a decoder on Linux) wants the exact name: re-skipped with the marker, once
        self.assertIn("parameter set D0004D_P1.f2p is in par/ but the decoder still reports it missing", log)
        [(_, _, err)] = self.entries()
        self.assertTrue(err.endswith(" [present in par/ but not accepted by the decoder]"), err)
        self.run_ok()
        self.assertEqual(self.calls(), [])

    def entries(self):
        """The data lines of skipped.txt as (name, skip time, error)."""
        lines = [ln for ln in self.skipped_txt.read_text().splitlines() if ln and not ln.startswith("#")]
        return [tuple(ln.split("\t")) for ln in lines]

    def run_ok(self):
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        return log

    def test_old_file_skipped(self):
        """old data file without its parameter set is listed in skipped.txt, WARNING once, not failed"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.settings()
        log = self.run_ok()
        self.assertEqual(self.calls(), ["D0004D_001.f2b"])
        self.assertNotIn("ERROR", log)
        self.assertEqual(log.count("WARNING - "), 1, log)
        self.assertIn("WARNING - Decoding D0004D/raw/D0004D_001.f2b: parameter set D0004D_P1.f2p still missing, "
                      "given up after 7 days, listed in decoded/skipped.txt", log)
        self.assertIn("Decoding D0004D: 0 decoded, 0 failed, 0 waiting for a parameter set, 1 skipped", log)
        [(name, at, err)] = self.entries()
        self.assertEqual(name, "D0004D_001.f2b")
        self.assertRegex(at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertTrue(err.startswith("<ERROR: PARAMETER SET NOT FOUND(1): '") and err.endswith("D0004D_P1.f2p'>"), err)
        text = self.skipped_txt.read_text()
        self.assertTrue(text.startswith("# Data files (raw/*.f2b) of this logger"), text)
        for words in ("PARAMETER SET NOT FOUND", "newer than its skip time", "deleting a file's line"):
            self.assertIn(words, text)
        st = self.status()
        self.assertEqual((st["ok"], st["decode_failed"], st["decode_waiting"], st["decode_skipped"]), (True, 0, 0, 1))
        rc, out = self.healthcheck_now()
        self.assertEqual(rc, 0, out)
        self.assertIn(", 1 skipped (parameter set missing)", out)

    def test_skipped_not_retried(self):
        """skipped file: no decoder call in the next runs, one INFO count line per logger"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.local_raw("D0004D_002.f2b", data("D0004D_P2.f2p"))
        self.settings()
        self.run_ok()
        self.calls()
        before = self.skipped_txt.read_bytes()
        for _ in range(2):
            log = self.run_ok()
            self.assertEqual(self.calls(), [])
            self.assertNotIn("WARNING", log)
            self.assertEqual(log.count("files skipped"), 1, log)
            self.assertIn("INFO - D0004D: 2 files skipped (parameter set missing), see decoded/skipped.txt", log)
            self.assertEqual(self.skipped_txt.read_bytes(), before)
            self.assertEqual(self.status()["decode_skipped"], 2)

    def test_new_par_retries(self):
        """a parameter set newer than the skip time retries: decoded -> line removed, still missing -> skipped again"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.local_raw("D0004D_002.f2b", data("D0004D_P9.f2p"))
        self.settings()
        self.run_ok()
        self.calls()
        [(_, first, _), _] = self.entries()
        # a parameter set from before the skip time changes nothing
        self.local_par("D0004D_P0.f2p", time.time() - 2 * DAY)
        self.run_ok()
        self.assertEqual(self.calls(), [])
        # the missing parameter set, obtained by other means, put into par/ later
        later = time.time() + 120
        self.local_par("D0004D_P1.f2p", later)
        log = self.run_ok()
        self.assertEqual(self.calls(), ["D0004D_P0.f2p", "D0004D_P1.f2p", "D0004D_001.f2b", "D0004D_002.f2b"])
        self.assertIn("INFO - Decoding D0004D/raw/D0004D_001.f2b: retrying, a parameter set in par/ is newer than "
                      f"its skip time {first}", log)
        self.assertEqual(log.count("WARNING - "), 1, log)
        self.assertIn("WARNING - Decoding D0004D/raw/D0004D_002.f2b: parameter set D0004D_P9.f2p still missing "
                      "after a newer parameter set arrived in par/; skipped again, listed in decoded/skipped.txt", log)
        self.assertIn("Decoding D0004D: 1 decoded, 0 failed, 0 waiting for a parameter set, 1 skipped", log)
        self.assertTrue((self.folder / "decoded/D0004D_001.csv").is_file())
        [(name, again, _)] = self.entries()
        self.assertEqual(name, "D0004D_002.f2b")
        self.assertGreater(again, first)
        # no loop: the next run does not retry it
        log = self.run_ok()
        self.assertEqual(self.calls(), [])
        self.assertIn("INFO - D0004D: 1 file skipped (parameter set missing)", log)
        st = self.status()
        self.assertEqual((st["ok"], st["files_decoded"], st["decode_skipped"]), (True, 0, 1))

    def test_all_decoded_removes_file(self):
        """when the last skipped file decodes, skipped.txt is removed"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.settings()
        self.run_ok()
        self.local_par("D0004D_P1.f2p", time.time() + 120)
        log = self.run_ok()
        self.assertIn("Decoding D0004D: 1 decoded, 0 failed", log)
        self.assertEqual(listing(self.folder / "decoded"), ["f D0004D_001.csv"])
        self.assertEqual(self.status()["decode_skipped"], 0)

    def test_deleted_line_retried(self):
        """deleting a line from skipped.txt retries the file; still missing -> skipped again at once"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.local_raw("D0004D_002.f2b", data("D0004D_P2.f2p"))
        self.settings()
        self.run_ok()
        self.calls()
        text = self.skipped_txt.read_text()
        self.skipped_txt.write_text("".join(ln for ln in text.splitlines(keepends=True)
                                            if not ln.startswith("D0004D_001.f2b")))
        log = self.run_ok()
        self.assertEqual(self.calls(), ["D0004D_001.f2b"])
        self.assertIn("INFO - D0004D: 1 file skipped (parameter set missing)", log)
        self.assertEqual(log.count("WARNING - "), 1, log)
        self.assertIn("WARNING - Decoding D0004D/raw/D0004D_001.f2b: parameter set D0004D_P1.f2p still missing, "
                      "given up after 7 days", log)
        self.assertEqual([e[0] for e in self.entries()], ["D0004D_001.f2b", "D0004D_002.f2b"])

    def test_other_errors_unchanged(self):
        """other decoder errors of old files stay ERRORs, retried every run, failed; unhealthy"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p", "STUB:exit255\n"))
        # missing parameter set AND another error reported by the decoder
        self.local_raw("D0004D_002.f2b", data("D0004D_P1.f2p") + "<ERROR: CRC MISMATCH (Line 3)>\n")
        self.settings(missing_parameter_set_days=0)
        for _ in range(2):
            log = self.run_ok()
            self.assertEqual(self.calls(), ["D0004D_001.f2b", "D0004D_002.f2b"])
            self.assertIn("ERROR - Decoding D0004D/raw/D0004D_001.f2b failed (retried next run): decoder exit code 255",
                          log)
            self.assertIn("ERROR - Decoding D0004D/raw/D0004D_002.f2b failed (retried next run): decoder reported "
                          "<ERROR: PARAMETER SET NOT FOUND", log)
            self.assertIn("Decoding D0004D: 0 decoded, 2 failed\n", log)
            self.assertNotIn("WARNING", log)
            self.assertFalse(self.skipped_txt.exists())
            st = self.status()
            self.assertEqual((st["ok"], st["decode_failed"], st["decode_waiting"], st["decode_skipped"]),
                             (False, 2, 0, 0))
            rc, out = self.healthcheck_now()
            self.assertEqual(rc, 1, out)

    def test_days_zero(self):
        """missing_parameter_set_days 0 skips a new data file on the first failure"""
        self.server_file("D0004D/raw/D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.settings(missing_parameter_set_days=0)
        log = self.run_ok()
        self.assertEqual(self.calls(), ["D0004D_001.f2b"])
        self.assertIn("still missing, given up after 0 days, listed in decoded/skipped.txt", log)
        self.assertEqual([e[0] for e in self.entries()], ["D0004D_001.f2b"])

    def test_days_setting(self):
        """missing_parameter_set_days: a file younger than the setting waits, an older one is skipped"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"), days_ago=2)
        self.local_raw("D0004D_002.f2b", data("D0004D_P1.f2p"), days_ago=4)
        self.settings(missing_parameter_set_days=3)
        log = self.run_ok()
        self.assertIn("D0004D_001.f2b: waiting for its parameter set D0004D_P1.f2p", log)
        self.assertIn(", 3 days after the download)", log)
        self.assertIn("D0004D_002.f2b: parameter set D0004D_P1.f2p still missing, given up after 3 days", log)
        self.assertEqual([e[0] for e in self.entries()], ["D0004D_002.f2b"])
        st = self.status()
        self.assertEqual((st["decode_waiting"], st["decode_skipped"]), (1, 1))

    def test_atomic_write(self):
        """skipped.txt is written through .skipped.txt.tmp and renamed, mode 0644 under umask 077"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.settings()
        renames = []
        real_replace = os.replace

        def replace(src, dst):   # os is one module: the scraper's own writes come here too
            renames.append((os.path.basename(src), os.path.basename(dst)))
            return real_replace(src, dst)
        with mock.patch.object(decoding.os, "replace", replace):
            rc, log = self.run_main(umask=0o077)
        self.assertEqual(rc, 0, log)
        self.assertIn((".skipped.txt.tmp", "skipped.txt"), renames)
        self.assertEqual(stat.S_IMODE(self.skipped_txt.stat().st_mode), 0o644)
        self.assertEqual(listing(self.folder / "decoded"), ["f skipped.txt"])

    def test_write_failure(self):
        """a failing write of skipped.txt is an ERROR, leaves no temporary file and counts as a failure"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.settings()

        real_replace = os.replace

        def replace(src, dst):   # only skipped.txt fails (the status file is written the same way)
            if os.path.basename(dst) == "skipped.txt":
                raise OSError(28, "No space left on device")
            return real_replace(src, dst)
        with mock.patch.object(decoding.os, "replace", replace):
            log = self.run_ok()
        self.assertIn("ERROR - Decoding D0004D: could not write decoded/skipped.txt: [Errno 28] No space left on device",
                      log)
        self.assertEqual(listing(self.folder / "decoded"), [])
        self.assertEqual(self.status()["decode_failed"], 1)

    def test_malformed_lines(self):
        """blank, comment and malformed lines of skipped.txt are ignored; files on such lines are pending"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.local_raw("D0004D_002.f2b", data("D0004D_P1.f2p"))
        self.local_raw("D0004D_003.f2b", data("D0004D_P1.f2p"))
        self.settings()
        text = ("# my own comment\n\n   \n"
                "D0004D_001.f2b\t2026-01-01T00:00:00Z\t<ERROR: PARAMETER SET NOT FOUND(1): x/D0004D_P1.f2p>\n"
                "D0004D_002.f2b 2026-01-01T00:00:00Z no tabs\n"
                "D0004D_003.f2b\tyesterday\t<ERROR: x>\n"
                "raw/D0004D_002.f2b\t2026-01-01T00:00:00Z\n"
                "\t\t\n")
        self.skipped_txt.parent.mkdir(parents=True)
        self.skipped_txt.write_text(text)
        log = self.run_ok()
        # D0004D_001.f2b stays skipped (no parameter set in par/); the others are pending
        self.assertEqual(self.calls(), ["D0004D_002.f2b", "D0004D_003.f2b"])
        self.assertIn("INFO - D0004D: 1 file skipped (parameter set missing)", log)
        self.assertEqual([e[0] for e in self.entries()], ["D0004D_001.f2b", "D0004D_002.f2b", "D0004D_003.f2b"])

    def test_malformed_kept_when_unchanged(self):
        """a skipped.txt with odd lines is not rewritten while its entries do not change"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.settings()
        text = ("# kept\n\nnot a line\n"
                "D0004D_001.f2b\t2026-01-01T00:00:00Z\t<ERROR: PARAMETER SET NOT FOUND(1): x/D0004D_P1.f2p>\n")
        self.skipped_txt.parent.mkdir(parents=True)
        self.skipped_txt.write_text(text)
        log = self.run_ok()
        self.assertEqual(self.calls(), [])
        self.assertIn("INFO - D0004D: 1 file skipped (parameter set missing)", log)
        self.assertEqual(self.skipped_txt.read_text(), text)

    def test_stale_line_removed(self):
        """lines of data files that are decoded or gone are removed"""
        self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"))
        self.settings()
        self.skipped_txt.parent.mkdir(parents=True)
        self.skipped_txt.write_text(
            "D0004D_001.f2b\t2026-01-01T00:00:00Z\t<ERROR: PARAMETER SET NOT FOUND(1): x/D0004D_P1.f2p>\n"
            "D0004D_000.f2b\t2026-01-01T00:00:00Z\t<ERROR: PARAMETER SET NOT FOUND(1): x/D0004D_P0.f2p>\n")
        self.run_ok()
        self.assertEqual(self.calls(), [])
        self.assertEqual([e[0] for e in self.entries()], ["D0004D_001.f2b"])
        self.assertTrue(self.skipped_txt.read_text().startswith("# Data files"))

    def test_detection(self):
        """PARAMETER SET NOT FOUND lines of the real decoder and the stub are recognised, the .f2p named"""
        real = ["<ERROR: PARAMETER SET NOT FOUND(1): /work/data/D1022D/par/D1022D_20250928120758.f2p (Line 1)>",
                "<ERROR: PARAMETER SET NOT FOUND(1): /work/data/D1022D/par/D1022D_20250928120758.f2p (Line 0)>",
                "  <error: parameter set not found(2): C:\\x\\par\\D1022D_20250928120758.f2p>"]
        for line in real:
            self.assertTrue(decoding.MISSING_PARSET.match(line), line)
            self.assertEqual(decoding.parameter_set_name(line), "D1022D_20250928120758.f2p")
        self.assertEqual(decoding.parameter_set_name("<ERROR: PARAMETER SET NOT FOUND(1): '/w/par/D0004D_P1.f2p'>"),
                         "D0004D_P1.f2p")
        self.assertEqual(decoding.parameter_set_name("<ERROR: PARAMETER SET NOT FOUND>"),
                         "(name not in the decoder's error)")
        for line in ["<ERROR: CRC MISMATCH>", "<INFO: PARAMETER SET NOT FOUND>", "PARAMETER SET NOT FOUND"]:
            self.assertFalse(decoding.MISSING_PARSET.match(line), line)

    def test_nonzero_exit_missing_par(self):
        """a non-zero exit code whose only error output is PARAMETER SET NOT FOUND also waits"""
        self.settings()
        dec = decoding.Decoder(fs.settings.decoding)
        self.addCleanup(dec.close)
        raw = self.local_raw("D0004D_001.f2b", data("D0004D_P1.f2p"), days_ago=0)
        src = decoding.LoggerSource("D0004D", "", self.folder)
        out = "<ERROR: PARAMETER SET NOT FOUND(1): /w/par/D0004D_P1.f2p (Line 0)>\n"
        with mock.patch.object(dec, "run", return_value=(1, out)):
            reason, missing = decoding.decode_file(dec, src, raw, self.folder / "decoded", None)
        self.assertEqual(reason, "decoder exit code 1: " + out.strip())
        self.assertEqual(missing, out.strip())
        with mock.patch.object(dec, "run", return_value=(1, out + "<ERROR: OTHER>\n")):
            _, missing = decoding.decode_file(dec, src, raw, self.folder / "decoded", None)
        self.assertIsNone(missing)
        with mock.patch.object(dec, "run", return_value=(None, out)):
            _, missing = decoding.decode_file(dec, src, raw, self.folder / "decoded", None)
        self.assertIsNone(missing)
