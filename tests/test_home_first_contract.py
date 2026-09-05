import copy
import hashlib
import unittest
from datetime import datetime, timezone

from router.ac86u.home_contract import (
    BACKUP_SCHEMA,
    REPORT_SCHEMA,
    ROUTE_CONTEXT,
    ContractError,
    candidate_id,
    canonical_name,
    object_sha256,
    station_key,
    url_sha256,
    validate_backup_pool,
    validate_candidate_manifest,
    validate_home_report_v2,
)
from scripts.build_home_candidate_manifest import build_manifest


UTC = timezone.utc
FORMAL_URL = "https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u"


def verification(*, deep=True, height=1080):
    return {
        "sample_count": 2,
        "startup_s": 0.8,
        "min_download_mbps": 20.0,
        "stream_mbps": 6.0,
        "headroom_ratio": 3.333,
        "width": 1920 if height else 0,
        "height": height,
        "codec": "h264" if height else "",
        "fps": 50.0 if height else 0.0,
        "bitrate_mbps": 6.0 if height else 0.0,
        "deep_checked": deep,
    }


class HomeFirstContractTests(unittest.TestCase):
    def setUp(self):
        self.formal = (
            "#EXTM3U\n"
            "#EXTINF:-1 group-title=\"卫视台\",CCTV-1\n"
            "http://current.test/cctv1.m3u8\n"
            "#EXTINF:-1 group-title=\"卫视台\",东方卫视\n"
            "http://current.test/dongfang.m3u8\n"
        ).encode()
        self.rows = [
            {
                "name": "CCTV1 高清",
                "group": "大陆",
                "url": "http://candidate.test/cctv1.m3u8",
                "sources": ["source-a"],
                "hk_verified": {"segment_ok": True, "height": 1080},
            },
            {
                "name": "CCTV-1",
                "group": "大陆",
                "url": "http://candidate.test/cctv1.m3u8",
                "sources": ["source-b"],
            },
            {
                "name": "上海卫视",
                "group": "大陆",
                "url": "http://candidate.test/dongfang.m3u8",
                "sources": ["source-c"],
            },
            {
                "name": "CCTV-8K",
                "group": "大陆",
                "url": "http://candidate.test/cctv8k.m3u8",
                "sources": ["source-d"],
            },
            {
                "name": "CCTV-1",
                "group": "大陆",
                "url": "http://current.test/cctv1.m3u8",
                "sources": ["source-current"],
            },
        ]
        self.manifest, _ = build_manifest(
            discovery_rows=self.rows,
            formal_bytes=self.formal,
            formal_url=FORMAL_URL,
            source_revision="abc123",
            generated_utc="2026-09-01T01:30:00Z",
        )

    def test_station_keys_cover_only_cctv_and_mainland_satellites(self):
        self.assertEqual("cctv5plus", station_key("CCTV-5+ 高清"))
        self.assertEqual("cctv4k", station_key("CCTV 4K"))
        self.assertEqual("cctv8", station_key("CCTV-8 1080p"))
        self.assertIsNone(station_key("CCTV-8K"))
        self.assertEqual("东方卫视", station_key("上海卫视高清"))
        self.assertEqual("湖南卫视", station_key("湖南卫视 1080p"))
        self.assertIsNone(station_key("凤凰卫视中文台"))

    def test_builder_creates_unverified_home_only_candidates(self):
        self.assertEqual(2, self.manifest["candidate_count"])
        by_key = {row["channel_key"]: row for row in self.manifest["candidates"]}
        self.assertEqual({"cctv1", "东方卫视"}, set(by_key))
        self.assertEqual(["source-a", "source-b"], by_key["cctv1"]["sources"])
        self.assertNotIn("hk_verified", by_key["cctv1"])
        self.assertFalse(by_key["cctv1"]["cloud_stream_probe_performed"])
        self.assertFalse(by_key["cctv1"]["home_verified"])
        self.assertFalse(by_key["cctv1"]["production_eligible"])
        validate_candidate_manifest(self.manifest)

    def test_candidate_manifest_rejects_cloud_promotion_and_tampering(self):
        promoted = copy.deepcopy(self.manifest)
        promoted["candidates"][0]["production_eligible"] = True
        promoted["candidate_set_sha256"] = object_sha256(promoted["candidates"])
        with self.assertRaisesRegex(ContractError, "illegally claims"):
            validate_candidate_manifest(promoted)

        tampered = copy.deepcopy(self.manifest)
        tampered["candidates"][0]["url"] += "?changed=1"
        tampered["candidate_set_sha256"] = object_sha256(tampered["candidates"])
        with self.assertRaisesRegex(ContractError, "URL hash"):
            validate_candidate_manifest(tampered)

    def backup_pool(self):
        row = self.manifest["candidates"][0]
        return {
            "schema": BACKUP_SCHEMA,
            "probe_id": "home-ac86u-123",
            "generated_utc": "2026-09-01T02:00:00Z",
            "route_context": ROUTE_CONTEXT,
            "formal_playlist_sha256": self.manifest["formal_playlist"]["sha256"],
            "candidate_manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
            "backup_count": 1,
            "backups": [{
                "candidate_id": row["candidate_id"],
                "channel_key": row["channel_key"],
                "name": row["name"],
                "url": row["url"],
                "url_sha256": row["url_sha256"],
                "request_options": row["request_options"],
                "qualification": "QUALIFIED",
                "source_manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
                "qualified_utc": "2026-09-01T01:58:00Z",
                "last_verified_utc": "2026-09-01T02:00:00Z",
                "expires_utc": "2026-09-02T02:00:00Z",
                "verification": verification(),
            }],
        }

    def test_backup_pool_requires_home_path_deep_evidence_and_freshness(self):
        now = datetime(2026, 9, 1, 3, 0, tzinfo=UTC).timestamp()
        validate_backup_pool(self.backup_pool(), expected_probe_id="home-ac86u-123", now_epoch=now)

        wrong_path = self.backup_pool()
        wrong_path["route_context"] = "router-origin-direct-wan"
        with self.assertRaisesRegex(ContractError, "living-room"):
            validate_backup_pool(wrong_path, now_epoch=now)

        shallow = self.backup_pool()
        shallow["backups"][0]["verification"]["deep_checked"] = False
        with self.assertRaisesRegex(ContractError, "deep two-sample"):
            validate_backup_pool(shallow, now_epoch=now)

        expired = self.backup_pool()
        expired["backups"][0]["expires_utc"] = "2026-09-01T02:30:00Z"
        with self.assertRaisesRegex(ContractError, "expired"):
            validate_backup_pool(expired, now_epoch=now)

    def report(self, *, current_status="BAD", action="REPLACE"):
        candidate = self.manifest["candidates"][0]
        current_url = "http://current.test/cctv1.m3u8"
        replacement = candidate["candidate_id"] if action == "REPLACE" else None
        return {
            "schema": REPORT_SCHEMA,
            "probe_id": "home-ac86u-123",
            "generated_utc": "2026-09-01T13:00:00Z",
            "run_kind": "primary-0200",
            "run_status": "COMPLETED",
            "production_modified": False,
            "actionable": True,
            "route_context": ROUTE_CONTEXT,
            "formal_playlist": {
                "url": FORMAL_URL,
                "sha256": hashlib.sha256(self.formal).hexdigest(),
                "channel_count": 2,
            },
            "baseline": {
                "home_network_ok": True,
                "github_reachable": True,
                "route_verified": True,
                "mass_failure_circuit_breaker": False,
            },
            "current_results": [{
                "channel_key": "cctv1",
                "name": canonical_name("cctv1"),
                "url": current_url,
                "url_sha256": url_sha256(current_url),
                "status": current_status,
                "failure_confirmed": current_status == "BAD",
                "attempt_count": 2 if current_status == "BAD" else 1,
                "verification": verification(deep=False, height=0 if current_status != "GOOD" else 1080),
            }],
            "candidate_results": [{
                "candidate_id": candidate["candidate_id"],
                "channel_key": candidate["channel_key"],
                "url": candidate["url"],
                "request_options": candidate["request_options"],
                "qualification": "QUALIFIED",
                "purpose": "switch-reverification",
                "switch_reverified": True,
                "verification": verification(),
            }],
            "decisions": [{
                "channel_key": "cctv1",
                "action": action,
                "reason": "confirmed_home_failure" if action == "REPLACE" else "healthy",
                "replacement_candidate_id": replacement,
            }],
        }

    def test_home_report_allows_only_home_confirmed_reverified_replacement(self):
        report = self.report()
        validate_home_report_v2(report, expected_probe_id="home-ac86u-123")

        unknown = self.report(current_status="UNKNOWN")
        unknown["decisions"][0]["action"] = "REPLACE"
        with self.assertRaisesRegex(ContractError, "confirmed home evidence"):
            validate_home_report_v2(unknown)

        no_network = self.report()
        no_network["baseline"]["home_network_ok"] = False
        with self.assertRaisesRegex(ContractError, "confirmed home evidence"):
            validate_home_report_v2(no_network)

        not_reverified = self.report()
        not_reverified["candidate_results"][0]["switch_reverified"] = False
        with self.assertRaisesRegex(ContractError, "not reverified"):
            validate_home_report_v2(not_reverified)

        wrong_channel = self.report()
        wrong_channel["candidate_results"][0]["channel_key"] = "cctv2"
        wrong_channel["candidate_results"][0]["candidate_id"] = candidate_id(
            "cctv2",
            wrong_channel["candidate_results"][0]["url"],
            wrong_channel["candidate_results"][0]["request_options"],
        )
        wrong_channel["decisions"][0]["replacement_candidate_id"] = wrong_channel["candidate_results"][0]["candidate_id"]
        with self.assertRaisesRegex(ContractError, "not qualified"):
            validate_home_report_v2(wrong_channel)

        incomplete = self.report()
        incomplete["decisions"] = []
        with self.assertRaisesRegex(ContractError, "cover every"):
            validate_home_report_v2(incomplete)

    def test_1300_report_cannot_scan_general_candidates(self):
        report = self.report()
        report["run_kind"] = "recheck-1300"
        report["candidate_results"][0]["purpose"] = "daily-qualification"
        report["candidate_results"][0]["switch_reverified"] = False
        with self.assertRaisesRegex(ContractError, "13:00"):
            validate_home_report_v2(report)


if __name__ == "__main__":
    unittest.main()
