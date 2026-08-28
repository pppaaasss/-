import tempfile
import unittest
from pathlib import Path

from scripts.hk_probe import ProbeResult, update_state


BASE = 1_700_000_000.0


def result(status="UNKNOWN", url="http://example.test/live.m3u8", name="CCTV-1"):
    return ProbeResult(name=name, url=url, status=status, error="test_failure")


class HongKongDeadStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp.name) / "state.json"

    def tearDown(self):
        self.temp.cleanup()

    def update(self, rows, hours, **kwargs):
        defaults = {
            "dead_after_runs": 3,
            "dead_min_age_hours": 6,
            "circuit_breaker_unknown_ratio": 0.25,
            "circuit_breaker_min_unknown": 20,
        }
        defaults.update(kwargs)
        return update_state(rows, self.state_path, now_epoch=BASE + hours * 3600, **defaults)

    def test_three_failures_spread_over_eight_hours_become_dead(self):
        first = result()
        self.update([first], 0)
        second = result()
        self.update([second], 4)
        third = result()
        state = self.update([third], 8)
        self.assertEqual("DEAD", third.status)
        self.assertTrue(third.hk_dead_confirmed)
        self.assertEqual(3, third.consecutive_failures)
        self.assertEqual(1, len(state["alerts"]))
        self.assertFalse(state["alerts"][0]["replacement_eligible"])

    def test_fast_retries_do_not_satisfy_minimum_age(self):
        self.update([result()], 0)
        self.update([result()], 1)
        third = result()
        self.update([third], 2)
        self.assertEqual("UNKNOWN", third.status)
        self.assertFalse(third.hk_dead_confirmed)

    def test_degraded_stream_is_alive_and_resets_failure_streak(self):
        self.update([result()], 0)
        degraded = result(status="DEGRADED")
        state = self.update([degraded], 4)
        self.assertEqual("DEGRADED", degraded.status)
        self.assertEqual(0, degraded.consecutive_failures)
        self.assertEqual(0, state["channels"]["CCTV-1"]["consecutive_failures"])

    def test_manual_url_change_resets_failure_streak(self):
        self.update([result()], 0)
        self.update([result()], 4)
        changed = result(url="http://example.test/replacement.m3u8")
        self.update([changed], 8)
        self.assertEqual("UNKNOWN", changed.status)
        self.assertEqual(1, changed.consecutive_failures)

    def test_mass_failure_opens_circuit_and_does_not_increment(self):
        rows = [result(name=f"Channel-{index}") for index in range(20)]
        state = self.update(rows, 0)
        self.assertTrue(state["last_run"]["circuit_breaker_open"])
        self.assertTrue(all(row.consecutive_failures == 0 for row in rows))
        self.assertFalse(state["alerts"])

    def test_good_stream_recovers_from_dead(self):
        self.update([result()], 0)
        self.update([result()], 4)
        dead = result()
        self.update([dead], 8)
        self.assertEqual("DEAD", dead.status)
        recovered = result(status="GOOD")
        state = self.update([recovered], 12)
        self.assertEqual("GOOD", recovered.status)
        self.assertEqual(0, recovered.consecutive_failures)
        self.assertFalse(state["alerts"])


if __name__ == "__main__":
    unittest.main()
