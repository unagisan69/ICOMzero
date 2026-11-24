import subprocess
import unittest
from unittest.mock import patch
from ft8station import chrony_health, clock_health


def report(**changes):
    values = dict(
        reference="A29FC801",
        stratum="3",
        stamp="1000",
        correction="0.001",
        last="-0.885",
        rms="0.327",
        freq="2",
        residual="0",
        skew="1",
        delay="0.03",
        dispersion="0.002",
        interval="32",
        leap="Normal",
    )
    values.update(changes)
    return ",".join(values.values())


class ChronyClock(unittest.TestCase):
    def test_historical_large_offset_does_not_override_current_correction(self):
        ok, status = chrony_health(report(), now=1010)
        self.assertTrue(ok, status)

    def test_current_csv_with_reference_name(self):
        fields = report().split(",")
        fields.insert(1, "162.159.200.1")
        self.assertTrue(chrony_health(",".join(fields), now=1010)[0])

    def test_large_remaining_correction_still_blocks(self):
        self.assertFalse(chrony_health(report(correction=".3"), now=1010)[0])

    def test_no_external_source_stale_reference_and_uncertainty_block(self):
        for change in (
            {"leap": "Not synchronised"},
            {"reference": "7F7F0101"},
            {"stamp": "0"},
            {"delay": "1.8"},
            {"dispersion": ".3"},
            {"skew": "100000"},
            {"stratum": "0"},
        ):
            self.assertFalse(chrony_health(report(**change), now=1010)[0], change)

    def test_invalid_tracking_is_rejected(self):
        for data in ("", "bad", report(correction="nan"), report(stamp="inf")):
            with self.assertRaises(ValueError):
                chrony_health(data, now=1010)

    def test_health_prefers_current_chrony_tracking(self):
        responses = [
            subprocess.CompletedProcess([], 0, b"yes\n"),
            subprocess.CompletedProcess([], 0, report().encode()),
        ]
        with patch("ft8station.shutil.which", return_value="/usr/bin/chronyc"), patch(
            "ft8station.subprocess.run", side_effect=responses
        ) as run, patch("ft8station.time.time", return_value=1010):
            self.assertTrue(clock_health()[0])
            self.assertEqual(run.call_args.args[0], ["chronyc", "-n", "-c", "tracking"])

    def test_unavailable_or_malformed_report_does_not_bypass_guard(self):
        for output in ("bad", report(correction=".3"), report(leap="Not synchronised")):
            responses = [
                subprocess.CompletedProcess([], 0, b"yes\n"),
                subprocess.CompletedProcess([], 0, output.encode()),
            ]
            with patch("ft8station.shutil.which", return_value="chronyc"), patch(
                "ft8station.subprocess.run", side_effect=responses
            ), patch("ft8station.time.time", return_value=1010):
                self.assertFalse(clock_health()[0])

    def test_no_monitoring_backend_does_not_report_healthy(self):
        responses = [
            subprocess.CompletedProcess([], 0, b"yes\n"),
            subprocess.CompletedProcess([], 1, b""),
        ]
        with patch("ft8station.shutil.which", return_value=None), patch(
            "ft8station.subprocess.run", side_effect=responses
        ):
            self.assertFalse(clock_health()[0])
