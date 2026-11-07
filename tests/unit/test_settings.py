"""Settings: load_settings and the command line (cli) with valid and invalid settings."""
import contextlib
import copy
import io
from unittest import mock

from support import ScraperTestCase, capture_logs, fs


class SettingsTest(ScraperTestCase):

    def valid(self) -> dict:
        """Settings with a decoding block (stub decoder path, post-processing on)."""
        return self.settings_dict([self.decode_source("C0003C", "SITE-03")],
                                  decoding=self.decoding_block("/stub/decode3"))

    def cli(self, d, *argv):
        """cli(argv) with settings d written to the settings file; returns (exit code, log,
        stdout)."""
        path = self.write_settings(d) if isinstance(d, dict) else d
        out = io.StringIO()
        with mock.patch.object(fs, "settings_path", path), capture_logs() as buf, contextlib.redirect_stdout(out):
            rc = fs.cli(list(argv))
        return rc, buf.getvalue(), out.getvalue()

    def assert_rejected(self, change, message):
        """Settings changed by change(d) are refused: exit 2 with the message, no run."""
        d = copy.deepcopy(self.valid())
        change(d)
        path = self.tmp / "settings.yml"
        rc, log, _ = self.cli(d)
        self.assertEqual(rc, 2, log)
        self.assertIn(f"ERROR - Invalid settings in {path}, not running", log)
        self.assertIn(message, log)
        self.assertNotIn("Downloading", log)
        self.assertNotIn("Traceback", log)
        self.assertFalse(self.status_path.exists())

    def test_unknown_step(self):
        """unknown post-processing step is rejected"""
        def change(d):
            d["decoding"]["postprocessing"]["steps"] = ["comment_metadata", "frobnicate"]
        self.assert_rejected(change, "unknown post-processing step(s) ['frobnicate']")

    def test_two_char_comment(self):
        """two-character comment_char is rejected"""
        def change(d):
            d["decoding"]["postprocessing"]["comment_char"] = "%%"
        self.assert_rejected(change, "comment_char must be exactly one non-whitespace character, got '%%'")

    def test_space_comment(self):
        """whitespace comment_char is rejected"""
        def change(d):
            d["decoding"]["postprocessing"]["comment_char"] = " "
        self.assert_rejected(change, "comment_char must be exactly one non-whitespace character")

    def test_unknown_decoding_key(self):
        """unknown key in decoding is rejected"""
        def change(d):
            d["decoding"]["colour"] = "blue"
        self.assert_rejected(change, "decoding.colour")

    def test_unknown_postprocessing_key(self):
        """unknown key in postprocessing is rejected"""
        def change(d):
            d["decoding"]["postprocessing"]["colour"] = "blue"
        self.assert_rejected(change, "decoding.postprocessing.colour")

    def test_output_folder_is_source_folder(self):
        """decoding output_folder that is a download folder is rejected"""
        def change(d):
            d["sources"][0]["folders"].append("decoded")
        self.assert_rejected(change, "decoding.output_folder 'decoded' is also a download folder of source C0003C")

    def test_not_a_mapping(self):
        """a settings file that is not a YAML mapping is rejected"""
        path = self.tmp / "settings.yml"
        path.write_text("- just\n- a list\n")
        rc, log, _ = self.cli(path)
        self.assertEqual(rc, 2, log)
        self.assertIn("not running: the file does not contain a YAML mapping", log)

    def test_check_settings_valid(self):
        """--check-settings with valid settings exits 0, says so and runs nothing"""
        path = self.tmp / "settings.yml"
        rc, log, out = self.cli(self.valid(), "--check-settings")
        self.assertEqual(rc, 0, log)
        self.assertEqual(out, f"Settings in {path} are valid.\n")
        self.assertEqual(log, "")
        self.assertFalse(self.status_path.exists())
        self.assertEqual(self.server.requests, [])

    def test_check_settings_invalid(self):
        """--check-settings with invalid settings exits 2 with the reason"""
        d = self.valid()
        d["decoding"]["postprocessing"]["steps"] = ["frobnicate"]
        rc, log, out = self.cli(d, "--check-settings")
        self.assertEqual(rc, 2, log)
        self.assertIn("unknown post-processing step(s) ['frobnicate']", log)
        self.assertNotIn("are valid", out)

    def test_import_loads_nothing(self):
        """importing the scraper loads no settings; cli sets them"""
        self.assertIsNone(fs.settings)
        self.server_dirs("A0001A/raw")
        rc, log, _ = self.cli(self.settings_dict([{"serial": "A0001A", "folders": "raw"}]))
        self.assertEqual(rc, 0, log)
        self.assertEqual(fs.settings.sources[0].serial, "A0001A")
