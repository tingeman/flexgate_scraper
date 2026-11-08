"""The decode step with the stub decoder (tests/stub_decode3.py), through whole runs."""
import os
import stat

from support import ScraperTestCase, listing

C_HEADER = "NO;TIME;#1(oC);#2(oC);HK-BAT(V)"
C_DATA = ["PARSET C0003C_P1.f2p", "<READOUT>", "<HK-INFO: 'Server OK'>",
          "1;28.10.2026 16:00:10;-10,87;-9,25;6,400", "2;28.10.2026 17:00:10;-10,92;-9,3",
          "<READOUT>", "<HK-INFO: 'Server Timeout'>", C_HEADER, "3;28.10.2026 18:00:10;-11;-9,37"]


def commented(stub_csv: bytes, c: str) -> bytes:
    """What comment_metadata makes of the stub's CSV (checked line by line, see check)."""
    out, seen = [], False
    for x in stub_csv.splitlines(keepends=True):
        if x.startswith(b"<"):
            out.append(c.encode() + x)
        elif x.startswith((b"NO;TIME", b"NO,TIME")):
            out.append(c.encode() + x if seen else x)
            seen = True
        else:
            out.append(x)
    return b"".join(out)


class DecodingTest(ScraperTestCase):

    def c_logger(self, serial="C0003C", logger_name="SITE-03", comment_char="%"):
        """A parameter set and two data files (one with metadata between the rows and a repeated
        header) on the server; settings with the stub decoder."""
        self.server_file(f"{serial}/par/{serial}_P1.f2p", C_HEADER + "\n")
        self.server_file(f"{serial}/raw/{serial}_001.f2b", "\n".join(C_DATA).replace("C0003C", serial) + "\n")
        self.server_file(f"{serial}/raw/{serial}_002.f2b",
                         f"PARSET {serial}_P1.f2p\n4;28.10.2026 19:00:10;-11,1;-9,4;6,398\n")
        self.use_settings([self.decode_source(serial, logger_name)],
                          decoding=self.decoding_block(self.stub_decoder(), comment_char=comment_char))
        return self.downloads / (f"{serial}_{logger_name}" if logger_name else serial)

    def first_run(self, **kw):
        folder = self.c_logger(**kw)
        rc, log = self.run_main(umask=0o077)
        self.assertEqual(rc, 0, log)
        return folder, log

    def check_commented(self, folder, stem, c):
        """The published CSV is the stub's CSV after comment_metadata with c (the stub's CSV
        has CRLF lines, metadata lines, one repeated header and data)."""
        stub_csv = (self.keep / f"{stem}.csv").read_bytes()
        lines = stub_csv.splitlines(keepends=True)
        self.assertTrue(all(x.endswith(b"\r\n") for x in lines), "stub CSV lines do not all end with CRLF")
        self.assertGreaterEqual(sum(x.startswith(b"<") for x in lines), 6)
        self.assertEqual(sum(x.startswith(b"NO;TIME") for x in lines), 2)
        self.assertEqual((folder / "decoded" / f"{stem}.csv").read_bytes(), commented(stub_csv, c))

    def test_first_run(self):
        """first run decodes parameter sets first, then each .f2b, adds only decoded/"""
        folder, log = self.first_run()
        self.assertEqual(self.calls(), ["C0003C_P1.f2p", "C0003C_001.f2b", "C0003C_002.f2b"])
        self.assertIn("Decoding C0003C_SITE-03: 2 decoded, 0 failed", log)
        self.assertNotIn("ERROR", log)
        self.assertEqual(listing(folder), sorted([
            "d raw", "f raw/C0003C_001.f2b", "f raw/C0003C_002.f2b", "d par", "f par/C0003C_P1.f2p",
            "d decoded", "f decoded/C0003C_001.csv", "f decoded/C0003C_002.csv"]))

    def test_header(self):
        """CSV has the parameter set's header"""
        folder, _ = self.first_run()
        csv = (folder / "decoded/C0003C_001.csv").read_bytes()
        self.assertIn(b"C0003C_P1.f2p>\r\n" + C_HEADER.encode() + b"\r\n%<READOUT>\r\n", csv)
        self.assertNotIn(b"unk.", csv)
        self.assertNotIn(b"<ERROR", csv)

    def test_mode(self):
        """published files have mode 0644 (also with umask 077), no .tmp files left"""
        folder, _ = self.first_run()
        modes = sorted(f"{stat.S_IMODE(p.stat().st_mode):o} {p.name}" for p in (folder / "decoded").iterdir())
        self.assertEqual(modes, ["644 C0003C_001.csv", "644 C0003C_002.csv"])
        self.assertEqual([e for e in listing(self.downloads) if e.endswith(".tmp")], [])

    def test_comment_percent(self):
        """comment_metadata with % comments metadata and repeated headers only"""
        folder, _ = self.first_run()
        self.check_commented(folder, "C0003C_001", "%")
        self.assertIn(b"2;28.10.2026 17:00:10;-10,92;-9,3\r\n%<READOUT>\r\n%<HK-INFO: 'Server Timeout'>\r\n%NO;TIME;",
                      (folder / "decoded/C0003C_001.csv").read_bytes())

    def test_comment_hash(self):
        """comment_metadata with another comment character"""
        folder, log = self.first_run(serial="F0006F", logger_name=None, comment_char="#")
        self.assertIn("Decoding F0006F: 2 decoded, 0 failed", log)
        self.check_commented(folder, "F0006F_001", "#")

    def test_second_run(self):
        """second run calls the decoder for nothing"""
        folder, _ = self.first_run()
        self.calls()
        before = listing(folder)
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.calls(), [])
        self.assertNotIn("Decoding", log)
        self.assertEqual(listing(folder), before)

    def d_logger(self):
        """D0004D: a data file whose parameter set is not on the server (yet)."""
        self.server_file("D0004D/raw/D0004D_001.f2b", "PARSET D0004D_P1.f2p\n1;28.10.2026 16:00:10;-1,5;-2,5\n")
        self.server_dirs("D0004D/par")
        self.use_settings([self.decode_source("D0004D")], decoding=self.decoding_block(self.stub_decoder()))

    def test_missing_par(self):
        """missing parameter set of a new data file: it waits (WARNING, not failed), no CSV; healthy"""
        self.d_logger()
        for _ in range(2):
            rc, log = self.run_main()
            self.assertEqual(rc, 0, log)
            self.assertEqual(self.calls(), ["D0004D_001.f2b"])
            self.assertNotIn("ERROR", log)
            self.assertEqual(log.count("WARNING - "), 1, log)
            self.assertIn("WARNING - Decoding D0004D/raw/D0004D_001.f2b: waiting for its parameter set D0004D_P1.f2p "
                          "(retried every run; skipped from 20", log)
            self.assertIn(", 7 days after the download)", log)
            self.assertIn("Decoding D0004D: 0 decoded, 0 failed, 1 waiting for a parameter set, 0 skipped", log)
            self.assertEqual(listing(self.downloads / "D0004D"), sorted(["d raw", "f raw/D0004D_001.f2b", "d par"]))
            st = self.status()
            self.assertEqual((st["ok"], st["decode_failed"], st["decode_waiting"], st["decode_skipped"]),
                             (True, 0, 1, 0))
            rc, out = self.healthcheck_now()
            self.assertEqual(rc, 0, out)
            self.assertIn(", 1 waiting for a parameter set", out)

    def test_par_appears(self):
        """published after the parameter set appears"""
        self.d_logger()
        self.run_main()
        self.calls()
        self.server_file("D0004D/par/D0004D_P1.f2p", "NO;TIME;#1(oC);#2(oC)\n")
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.calls(), ["D0004D_P1.f2p", "D0004D_001.f2b"])
        self.assertIn("Decoding D0004D: 1 decoded, 0 failed", log)
        self.assertIn(b"NO;TIME;#1(oC);#2(oC)\r\n1;", (self.downloads / "D0004D/decoded/D0004D_001.csv").read_bytes())

    def test_failures(self):
        """exit 255, timeout and no output are ERRORs, other files decoded"""
        self.server_file("E0005E/par/E0005E_P1.f2p", "NO;TIME;#1(oC)\n")
        self.server_file("E0005E/raw/E0005E_a.f2b", "STUB:exit255\nPARSET E0005E_P1.f2p\n1;x;1\n")
        self.server_file("E0005E/raw/E0005E_b.f2b", "STUB:sleep 30\nPARSET E0005E_P1.f2p\n1;x;1\n")
        self.server_file("E0005E/raw/E0005E_c.f2b", "PARSET E0005E_P1.f2p\n<READOUT>\n1;28.10.2026 16:00:10;-3,25\n")
        self.server_file("E0005E/raw/E0005E_d.f2b", "STUB:noout\nPARSET E0005E_P1.f2p\n1;x;1\n")
        self.use_settings([self.decode_source("E0005E")],
                          decoding=self.decoding_block(self.stub_decoder(), timeout=0.5))
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.calls(), ["E0005E_P1.f2p", "E0005E_a.f2b", "E0005E_b.f2b", "E0005E_c.f2b", "E0005E_d.f2b"])
        prefix = "ERROR - Decoding E0005E/raw/E0005E_{} failed (retried next run): "
        self.assertIn(prefix.format("a.f2b") + "decoder exit code 255", log)
        self.assertIn(prefix.format("b.f2b") + "decoder timed out after 0.5 s", log)
        self.assertIn(prefix.format("d.f2b") + "decoder wrote no", log)
        self.assertIn("Decoding E0005E: 1 decoded, 3 failed", log)
        self.assertEqual(os.listdir(self.downloads / "E0005E/decoded"), ["E0005E_c.csv"])

    def test_postprocessing_off(self):
        """post-processing disabled publishes the decoder output unchanged"""
        self.server_file("E0005E/par/E0005E_P1.f2p", "NO;TIME;#1(oC)\n")
        self.server_file("E0005E/raw/E0005E_c.f2b", "PARSET E0005E_P1.f2p\n<READOUT>\n1;28.10.2026 16:00:10;-3,25\n")
        self.use_settings([self.decode_source("E0005E")],
                          decoding=self.decoding_block(self.stub_decoder(), comment_char=None))
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertIn("Decoding E0005E: 1 decoded, 0 failed", log)
        self.assertEqual((self.downloads / "E0005E/decoded/E0005E_c.csv").read_bytes(),
                         (self.keep / "E0005E_c.csv").read_bytes())

    def test_no_decoder(self):
        """missing decoder gives one ERROR, downloads still happen"""
        self.server_file("G0007G/raw/G0007G_001.f2b", "PARSET G0007G_P1.f2p\n1;x;1\n")
        self.server_dirs("G0007G/par")
        self.use_settings([self.decode_source("G0007G"), self.decode_source("G0008G")],
                          decoding=self.decoding_block("/opt/nothere/decode3"))
        self.server_dirs("G0008G/raw", "G0008G/par")
        rc, log = self.run_main()
        self.assertEqual(rc, 0, log)
        self.assertEqual(log.count("ERROR - Decoder /opt/nothere/decode3 is missing or not executable"), 1)
        self.assertEqual(log.count("Downloading "), 1)
        self.assertEqual(listing(self.downloads / "G0007G"), sorted(["d raw", "f raw/G0007G_001.f2b", "d par"]))
