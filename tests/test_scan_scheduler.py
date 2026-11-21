"""Regression tests for the signal scanner; the strategy file stays identical."""
import hashlib
import pathlib
import unittest

from scan_scheduler import (
    MAX_ATTEMPTS_PER_MINUTE, RETRY_DELAY_SECONDS,
    can_start_scan, note_scan_attempt, note_scan_success, note_scan_failure,
    scan_diagnostics,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


class ScanSchedulerTests(unittest.TestCase):
    def test_original_strategy_source_is_unchanged(self):
        content=(ROOT/"engine.py").read_bytes()
        git_sha=hashlib.sha1(f"blob {len(content)}\0".encode("ascii")+content).hexdigest()
        self.assertEqual(git_sha,"14bea5041a17aa48858510296c4e0c46ae5c4a1b",
                         "Do not modify strategy rules or presets in a data pipeline fix")

    def test_start_at_first_closed_minute_not_before_two_seconds(self):
        health={"spot_subscribed":True}
        minute=100
        self.assertFalse(can_start_scan(health,6000.9,minute,None,True))
        self.assertTrue(can_start_scan(health,6002.1,minute,None,True))

    def test_failure_does_not_mark_minute_done_and_retries_same_minute(self):
        health={}
        minute=100
        note_scan_attempt(health,minute,6002.1)
        note_scan_failure(health,6002.1,"خطأ بيانات")
        self.assertFalse(can_start_scan(health,6007,minute,None,True))
        self.assertTrue(can_start_scan(health,6009,minute,None,True))
        self.assertEqual(health["scan_attempts_this_minute"],1)

    def test_after_three_attempts_wait_for_next_minute(self):
        health={}
        minute=100
        for second in (6003,6011,6019):
            note_scan_attempt(health,minute,second)
            note_scan_failure(health,second,"API error")
        self.assertEqual(health["scan_attempts_this_minute"],MAX_ATTEMPTS_PER_MINUTE)
        self.assertFalse(can_start_scan(health,6035,minute,None,True))
        self.assertTrue(can_start_scan(health,6062.1,101,None,True))
        note_scan_attempt(health,101,6062.1)
        self.assertEqual(health["scan_attempts_this_minute"],1)

    def test_success_marks_health_and_no_duplicate_scan(self):
        health={}
        note_scan_attempt(health,100,6003)
        note_scan_success(health,6005)
        self.assertEqual(health["scan_successes_total"],1)
        self.assertIsNone(health["analysis_error"])
        self.assertFalse(can_start_scan(health,6012,100,100,True))

    def test_market_health_is_independent(self):
        btc={};gold={}
        note_scan_attempt(btc,100,6002.2)
        note_scan_failure(btc,6002.2,"BTC candles issue")
        self.assertTrue(can_start_scan(gold,6002.5,100,None,True))
        self.assertFalse(can_start_scan(btc,6005,100,None,True))

    def test_diagnostics_does_not_report_ready_when_bars_missing(self):
        health={"last_scan_success_at":6005,"scan_attempts_total":1}
        d=scan_diagnostics(health,6006,100,None,{"reason":"WARMUP"},True)
        self.assertEqual(d["scan_state"],"WAIT_CANDLES")
        self.assertEqual(d["reason"],"WARMUP")

    def test_diagnostics_show_error_retry(self):
        health={}
        note_scan_attempt(health,100,6002.1)
        note_scan_failure(health,6002.2,"خطأ بيانات cTrader: REQUEST_FREQUENCY_EXCEEDED")
        d=scan_diagnostics(health,6002.3,None,None,{"reason":"WAIT"},True)
        self.assertEqual(d["scan_state"],"ERROR_RETRY")
        self.assertIsNotNone(d["last_scan_error_code"])


if __name__=="__main__":
    unittest.main()
