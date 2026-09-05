import hashlib
import unittest
from datetime import datetime, timezone

from router.ac86u.home_contract import (
    ROUTE_CONTEXT,
    candidate_id,
    validate_backup_pool,
)
from router.ac86u.home_decision import (
    MAX_BACKUPS_PER_CHANNEL,
    backup_refresh_candidates,
    candidate_result,
    current_result,
    eligible_backups,
    mass_failure_circuit,
    update_backup_pool,
)


NOW = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc).timestamp()
FORMAL_SHA = hashlib.sha256(b"formal").hexdigest()
MANIFEST_SHA = hashlib.sha256(b"manifest").hexdigest()


def raw_probe(status="GOOD", *, height=1080, speed=20.0, deep=True):
    return {
        "observed_status": status,
        "sample_count": 2,
        "startup_s": 0.7,
        "min_download_mbps": speed,
        "stream_mbps": 6.0,
        "headroom_ratio": speed / 6.0,
        "width": 1920 if height else 0,
        "height": height,
        "codec": "h264" if height else "",
        "fps": 50.0 if height else 0.0,
        "bitrate_mbps": 6.0 if height else 0.0,
        "deep_checked": deep,
        "min_height": 1080,
        "error": "" if status == "GOOD" else "test_failure",
    }


def candidate(suffix="one", *, channel_key="cctv1"):
    url = f"https://candidate.test/{suffix}.m3u8"
    options = ""
    return {
        "candidate_id": candidate_id(channel_key, url, options),
        "channel_key": channel_key,
        "url": url,
        "request_options": options,
    }


class HomeDecisionTests(unittest.TestCase):
    def test_current_route_requires_two_non_good_attempts(self):
        url = "https://current.test/cctv1.m3u8"
        good = current_result("CCTV-1", url, [raw_probe()], circuit_open=False)
        recovered = current_result(
            "CCTV-1", url, [raw_probe("UNKNOWN", height=0), raw_probe()], circuit_open=False
        )
        bad = current_result(
            "CCTV-1",
            url,
            [raw_probe("DEGRADED"), raw_probe("DEGRADED")],
            circuit_open=False,
        )
        self.assertEqual(("GOOD", False, 1), (good["status"], good["failure_confirmed"], good["attempt_count"]))
        self.assertEqual(("UNKNOWN", False, 2), (recovered["status"], recovered["failure_confirmed"], recovered["attempt_count"]))
        self.assertEqual(("BAD", True, 2), (bad["status"], bad["failure_confirmed"], bad["attempt_count"]))

    def test_mass_failure_circuit_turns_confirmed_failure_into_unknown(self):
        attempts = {
            f"cctv{index}": [raw_probe("UNKNOWN", height=0), raw_probe("UNKNOWN", height=0)]
            for index in range(1, 13)
        }
        self.assertTrue(mass_failure_circuit(attempts, minimum_channels=12, failure_ratio=0.35))
        row = current_result(
            "CCTV-1",
            "https://current.test/cctv1.m3u8",
            attempts["cctv1"],
            circuit_open=True,
        )
        self.assertEqual("UNKNOWN", row["status"])
        self.assertFalse(row["failure_confirmed"])

    def test_backup_pool_accepts_only_deep_home_qualified_routes(self):
        qualified = candidate("qualified")
        shallow = candidate("shallow")
        pool = update_backup_pool(
            None,
            [(qualified, raw_probe()), (shallow, raw_probe(deep=False))],
            probe_id="home-ac86u-test",
            now_epoch=NOW,
            formal_playlist_sha256=FORMAL_SHA,
            candidate_manifest_sha256=MANIFEST_SHA,
            current_urls={"cctv1": "https://current.test/cctv1.m3u8"},
            ttl_hours=36,
        )
        validate_backup_pool(pool, expected_probe_id="home-ac86u-test", now_epoch=NOW)
        self.assertEqual(1, pool["backup_count"])
        self.assertEqual(qualified["candidate_id"], pool["backups"][0]["candidate_id"])
        self.assertEqual(MANIFEST_SHA, pool["backups"][0]["source_manifest_sha256"])
        self.assertEqual(ROUTE_CONTEXT, pool["route_context"])

    def test_pool_drops_expired_and_current_routes_and_sorts_best_first(self):
        lower = candidate("lower")
        higher = candidate("higher")
        current = candidate("current")
        pool = update_backup_pool(
            None,
            [
                (lower, raw_probe(height=1080, speed=12)),
                (higher, raw_probe(height=2160, speed=10)),
                (current, raw_probe()),
            ],
            probe_id="home-ac86u-test",
            now_epoch=NOW,
            formal_playlist_sha256=FORMAL_SHA,
            candidate_manifest_sha256=MANIFEST_SHA,
            current_urls={"cctv1": current["url"]},
            ttl_hours=1,
        )
        selected = eligible_backups(pool, "cctv1", now_epoch=NOW)
        self.assertEqual([higher["candidate_id"], lower["candidate_id"]], [row["candidate_id"] for row in selected])

        refreshed = update_backup_pool(
            pool,
            [],
            probe_id="home-ac86u-test",
            now_epoch=NOW + 2 * 3600,
            formal_playlist_sha256=FORMAL_SHA,
            candidate_manifest_sha256=MANIFEST_SHA,
            current_urls={"cctv1": "https://current.test/cctv1.m3u8"},
            ttl_hours=1,
        )
        self.assertEqual(0, refreshed["backup_count"])

    def test_corrupt_existing_pool_is_never_trusted(self):
        pool = update_backup_pool(
            {"schema": "made-up", "backups": [{"url": "https://bad.test"}]},
            [],
            probe_id="home-ac86u-test",
            now_epoch=NOW,
            formal_playlist_sha256=FORMAL_SHA,
            candidate_manifest_sha256=MANIFEST_SHA,
            current_urls={},
            ttl_hours=36,
        )
        self.assertEqual([], pool["backups"])

    def test_candidate_report_requires_deep_quality(self):
        item = candidate()
        qualified = candidate_result(item, raw_probe(), purpose="daily-qualification", switch_reverified=False)
        rejected = candidate_result(
            item, raw_probe("DEGRADED", speed=2), purpose="switch-reverification", switch_reverified=True
        )
        self.assertEqual("QUALIFIED", qualified["qualification"])
        self.assertFalse(qualified["switch_reverified"])
        self.assertEqual("REJECTED", rejected["qualification"])
        self.assertFalse(rejected["switch_reverified"])

    def test_pool_is_bounded_per_channel_and_expiring_rows_are_refreshed(self):
        items = [candidate(f"backup-{index}") for index in range(MAX_BACKUPS_PER_CHANNEL + 3)]
        pool = update_backup_pool(
            None,
            [(item, raw_probe(speed=10 + index)) for index, item in enumerate(items)],
            probe_id="home-ac86u-test",
            now_epoch=NOW,
            formal_playlist_sha256=FORMAL_SHA,
            candidate_manifest_sha256=MANIFEST_SHA,
            current_urls={"cctv1": "https://current.test/cctv1.m3u8"},
            ttl_hours=36,
        )
        self.assertEqual(MAX_BACKUPS_PER_CHANNEL, pool["backup_count"])
        self.assertEqual([], backup_refresh_candidates(pool, now_epoch=NOW, refresh_before_hours=12))
        refresh = backup_refresh_candidates(pool, now_epoch=NOW + 24 * 3600, refresh_before_hours=18)
        self.assertEqual(MAX_BACKUPS_PER_CHANNEL, len(refresh))
        self.assertTrue(all(row["_queue_priority"] == 0 for row in refresh))


if __name__ == "__main__":
    unittest.main()
