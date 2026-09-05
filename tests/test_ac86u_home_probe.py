import functools
import hashlib
import http.server
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from router.ac86u import home_probe
from router.ac86u.home_contract import (
    CANDIDATE_SCHEMA,
    make_candidate,
    object_sha256,
    validate_home_report_v2,
)
from router.ac86u.home_decision import update_backup_pool


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


NOW = 1_800_000_000


def measured(name, url, floor, status="GOOD"):
    row = home_probe.empty_result(name, url, floor)
    row.update({
        "observed_status": status,
        "status": status,
        "sample_count": 2,
        "startup_s": 0.8,
        "min_download_mbps": 20.0,
        "stream_mbps": 6.0,
        "headroom_ratio": 3.333,
        "deep_checked": True,
        "width": 1920,
        "height": floor,
        "codec": "h264",
        "fps": 50.0,
        "bitrate_mbps": 6.0,
        "error": "" if status == "GOOD" else "confirmed_quality_failure",
    })
    return row


def seed_backup_pool(root, formal, channels):
    current_urls = {}
    qualified = []
    candidates = {}
    for index in channels:
        name = f"CCTV-{index}"
        key = f"cctv{index}"
        current_urls[key] = f"https://current.test/{key}.m3u8"
        item = make_candidate({
            "name": name,
            "url": f"https://candidate.test/{key}.m3u8",
            "sources": ["test-source"],
        })
        candidates[key] = item
        qualified.append((item, measured(name, item["url"], 1080)))
    pool = update_backup_pool(
        None,
        qualified,
        probe_id="home-ac86u-test",
        now_epoch=NOW,
        formal_playlist_sha256=hashlib.sha256(formal).hexdigest(),
        candidate_manifest_sha256=hashlib.sha256(b"candidate-manifest").hexdigest(),
        current_urls=current_urls,
        ttl_hours=36,
    )
    home_probe.atomic_json(Path(root) / "qualified-backups.json", pool)
    return candidates


class AC86UHomeProbeTests(unittest.TestCase):
    def test_generated_1300_report_matches_home_decision_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.ts").write_bytes(b"x" * (128 * 1024))
            (root / "two.ts").write_bytes(b"y" * (128 * 1024))
            (root / "live.m3u8").write_text(
                "#EXTM3U\n#EXTINF:4,\none.ts\n#EXTINF:4,\ntwo.ts\n",
                encoding="utf-8",
            )
            handler = functools.partial(QuietHandler, directory=temporary)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                (root / "tv-core.m3u").write_text(
                    f"#EXTM3U\n#EXTINF:-1,CCTV-1\nhttp://127.0.0.1:{port}/live.m3u8\n",
                    encoding="utf-8",
                )
                config = {
                    "probe_id": "home-ac86u-test",
                    "output_dir": str(root / "state"),
                    "playlist_url": f"http://127.0.0.1:{port}/tv-core.m3u",
                    "candidate_playlist_url": "",
                    "maximum_load1": 10000,
                    "minimum_mem_available_kib": 1,
                    "actionable": False,
                }
                report, _ = home_probe.run(config, run_kind="recheck-1300", now_epoch=1_800_000_000)
                validate_home_report_v2(report)
                self.assertEqual("GOOD", report["current_results"][0]["status"])
                self.assertEqual("recheck-1300", report["run_kind"])
                self.assertEqual("not_requested", report["policy"]["candidate_manifest_state"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_recent_segments_are_distinct_and_newest(self):
        playlist = """#EXTM3U
#EXTINF:4,
1.ts
#EXTINF:5,
2.ts
#EXTINF:6,
3.ts
"""
        self.assertEqual(
            [("https://x.test/2.ts", 5.0), ("https://x.test/3.ts", 6.0)],
            home_probe.recent_segments(playlist, "https://x.test/live.m3u8"),
        )

    def test_light_probe_uses_two_samples_and_measures_headroom(self):
        playlist = b"#EXTM3U\n#EXTINF:4,\n1.ts\n#EXTINF:4,\n2.ts\n"
        samples = [
            {
                "url": f"https://x.test/{index}.ts",
                "downloaded_bytes": 2 * 1024 * 1024,
                "total_bytes": 3 * 1024 * 1024,
                "duration_s": 4.0,
                "elapsed_s": 0.5,
                "download_mbps": 33.554,
                "stream_mbps": 6.291,
                "complete": False,
            }
            for index in (1, 2)
        ]
        with mock.patch.object(home_probe, "fetch_playlist", return_value=(playlist, "https://x.test/live.m3u8", 0.1)), mock.patch.object(
            home_probe, "segment_sample", side_effect=samples
        ) as sampler:
            row = home_probe.probe_route("CCTV-1", "https://x.test/live.m3u8", floor=1080, mode="light", config={})
        self.assertEqual("GOOD", row["status"])
        self.assertEqual(2, row["sample_count"])
        self.assertGreater(row["headroom_ratio"], 5)
        self.assertEqual(home_probe.LIGHT_SAMPLE_BYTES, sampler.call_args_list[0].args[2])

    def test_deep_probe_rejects_low_h264_intrinsic_bitrate(self):
        playlist = b"#EXTM3U\n#EXTINF:4,\n1.ts\n#EXTINF:4,\n2.ts\n"
        samples = [
            {
                "url": f"https://x.test/{index}.ts",
                "downloaded_bytes": 2 * 1024 * 1024,
                "total_bytes": 2 * 1024 * 1024,
                "duration_s": 4.0,
                "elapsed_s": 0.5,
                "download_mbps": 33.554,
                "stream_mbps": 4.194,
                "complete": True,
            }
            for index in (1, 2)
        ]
        meta = {"width": 1920, "height": 1080, "codec": "h264", "fps": 50.0, "bitrate_mbps": 0.0}
        with mock.patch.object(home_probe, "fetch_playlist", return_value=(playlist, "https://x.test/live.m3u8", 0.1)), mock.patch.object(
            home_probe, "segment_sample", side_effect=samples
        ), mock.patch.object(home_probe, "ffprobe_meta", return_value=meta):
            row = home_probe.probe_route("CCTV-10", "https://x.test/live.m3u8", floor=1080, mode="deep", config={})
        self.assertEqual("DEGRADED", row["status"])
        self.assertIn("h264_stream", row["error"])

    def test_deep_probe_rejects_unknown_intrinsic_bitrate(self):
        playlist = b"#EXTM3U\n#EXTINF:0,\n1.ts\n#EXTINF:0,\n2.ts\n"
        samples = [
            {
                "url": f"https://x.test/{index}.ts",
                "downloaded_bytes": 2 * 1024 * 1024,
                "total_bytes": 0,
                "duration_s": 0.0,
                "elapsed_s": 0.5,
                "download_mbps": 33.554,
                "stream_mbps": 0.0,
                "complete": False,
            }
            for index in (1, 2)
        ]
        meta = {"width": 1920, "height": 1080, "codec": "hevc", "fps": 50.0, "bitrate_mbps": 0.0}
        with mock.patch.object(home_probe, "fetch_playlist", return_value=(playlist, "https://x.test/live.m3u8", 0.1)), mock.patch.object(
            home_probe, "segment_sample", side_effect=samples
        ), mock.patch.object(home_probe, "ffprobe_meta", return_value=meta):
            row = home_probe.probe_route("CCTV-10", "https://x.test/live.m3u8", floor=1080, mode="deep", config={})
        self.assertEqual("UNKNOWN", row["status"])
        self.assertIn("bitrate_unknown", row["error"])

    def test_bad_route_without_home_backup_is_unresolved(self):
        formal = b"#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://current.test/cctv1.m3u8\n"

        def degraded(name, url, *, floor, **_kwargs):
            return measured(name, url, floor, "DEGRADED")

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            home_probe, "fetch_playlist", return_value=(formal, "https://repo.test/tv-core.m3u", 0.1)
        ), mock.patch.object(home_probe, "probe_route", side_effect=degraded):
            report, _ = home_probe.run({
                "probe_id": "home-ac86u-test",
                "output_dir": temporary,
                "actionable": True,
                "maximum_load1": 10000,
                "minimum_mem_available_kib": 1,
            }, run_kind="recheck-1300", now_epoch=NOW)
        self.assertEqual("BAD", report["current_results"][0]["status"])
        self.assertEqual("UNRESOLVED", report["decisions"][0]["action"])
        self.assertEqual("no_home_qualified_backup", report["decisions"][0]["reason"])

    def test_no_six_channel_replacement_limit(self):
        channels = list(range(1, 9))
        formal = "#EXTM3U\n" + "".join(
            f"#EXTINF:-1,CCTV-{index}\nhttps://current.test/cctv{index}.m3u8\n"
            for index in channels
        )
        formal_bytes = formal.encode()

        def route_probe(name, url, *, floor, **_kwargs):
            status = "DEGRADED" if "current.test" in url else "GOOD"
            return measured(name, url, floor, status)

        with tempfile.TemporaryDirectory() as temporary:
            seed_backup_pool(temporary, formal_bytes, channels)
            with mock.patch.object(
                home_probe, "fetch_playlist", return_value=(formal_bytes, "https://repo.test/tv-core.m3u", 0.1)
            ), mock.patch.object(home_probe, "probe_route", side_effect=route_probe) as probe:
                report, _ = home_probe.run({
                    "probe_id": "home-ac86u-test",
                    "output_dir": temporary,
                    "actionable": True,
                    "maximum_load1": 10000,
                    "minimum_mem_available_kib": 1,
                }, run_kind="recheck-1300", now_epoch=NOW)
        self.assertEqual(8, report["summary"]["bad"])
        self.assertEqual(8, report["summary"]["replacements"])
        self.assertTrue(all(row["action"] == "REPLACE" for row in report["decisions"]))
        self.assertTrue(all(row["purpose"] == "primary-cache" for row in report["candidate_results"]))
        self.assertEqual(16, probe.call_count)  # 8 first + 8 confirmations; zero backup probes.

    def test_good_current_route_is_kept_without_testing_a_better_backup(self):
        formal = b"#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://current.test/cctv1.m3u8\n"

        def good(name, url, *, floor, **_kwargs):
            return measured(name, url, floor, "GOOD")

        with tempfile.TemporaryDirectory() as temporary:
            seed_backup_pool(temporary, formal, [1])
            with mock.patch.object(
                home_probe, "fetch_playlist", return_value=(formal, "https://repo.test/tv-core.m3u", 0.1)
            ), mock.patch.object(home_probe, "probe_route", side_effect=good) as probe:
                report, _ = home_probe.run({
                    "probe_id": "home-ac86u-test",
                    "output_dir": temporary,
                    "actionable": True,
                    "maximum_load1": 10000,
                    "minimum_mem_available_kib": 1,
                }, run_kind="recheck-1300", now_epoch=NOW)
        self.assertEqual("KEEP", report["decisions"][0]["action"])
        self.assertEqual([], report["candidate_results"])
        self.assertEqual(1, probe.call_count)

    def test_failed_backup_reverification_is_removed_and_never_selected(self):
        formal = b"#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://current.test/cctv1.m3u8\n"

        def degraded(name, url, *, floor, **_kwargs):
            return measured(name, url, floor, "DEGRADED")

        with tempfile.TemporaryDirectory() as temporary:
            seed_backup_pool(temporary, formal, [1])
            with mock.patch.object(
                home_probe, "fetch_playlist", return_value=(formal, "https://repo.test/tv-core.m3u", 0.1)
            ), mock.patch.object(home_probe, "probe_route", side_effect=degraded):
                report, _ = home_probe.run({
                    "probe_id": "home-ac86u-test",
                    "output_dir": temporary,
                    "actionable": True,
                    "candidate_manifest_url": "",
                    "maximum_load1": 10000,
                    "minimum_mem_available_kib": 1,
                }, run_kind="primary-0200", now_epoch=NOW)
            pool = home_probe.load_json(Path(temporary) / "qualified-backups.json")
        self.assertEqual("UNRESOLVED", report["decisions"][0]["action"])
        self.assertEqual("home_backups_failed_reverification", report["decisions"][0]["reason"])
        self.assertEqual("REJECTED", report["candidate_results"][0]["qualification"])
        self.assertEqual(0, pool["backup_count"])

    def test_mass_failure_circuit_blocks_every_replacement(self):
        channels = list(range(1, 13))
        formal = "#EXTM3U\n" + "".join(
            f"#EXTINF:-1,CCTV-{index}\nhttps://current.test/cctv{index}.m3u8\n"
            for index in channels
        )
        formal_bytes = formal.encode()

        def degraded(name, url, *, floor, **_kwargs):
            return measured(name, url, floor, "DEGRADED")

        with tempfile.TemporaryDirectory() as temporary:
            seed_backup_pool(temporary, formal_bytes, channels)
            with mock.patch.object(
                home_probe, "fetch_playlist", return_value=(formal_bytes, "https://repo.test/tv-core.m3u", 0.1)
            ), mock.patch.object(home_probe, "probe_route", side_effect=degraded) as probe:
                report, _ = home_probe.run({
                    "probe_id": "home-ac86u-test",
                    "output_dir": temporary,
                    "actionable": True,
                    "maximum_load1": 10000,
                    "minimum_mem_available_kib": 1,
                }, run_kind="recheck-1300", now_epoch=NOW)
        self.assertTrue(report["baseline"]["mass_failure_circuit_breaker"])
        self.assertEqual(0, report["summary"]["bad"])
        self.assertEqual(12, report["summary"]["unknown"])
        self.assertEqual(0, report["summary"]["replacements"])
        self.assertEqual([], report["candidate_results"])
        self.assertEqual(24, probe.call_count)

    def test_run_profiles_have_only_0200_primary_and_1300_recheck(self):
        primary = home_probe.run_profile("primary-0200", {})
        recheck = home_probe.run_profile("recheck-1300", {})
        self.assertTrue(primary["scan_candidates"])
        self.assertFalse(recheck["scan_candidates"])
        self.assertTrue(primary["current_metadata"])
        self.assertTrue(recheck["current_metadata"])
        self.assertEqual(home_probe.LIGHT_SAMPLE_BYTES, primary["current_sample_bytes"])
        self.assertEqual(home_probe.LIGHT_SAMPLE_BYTES, recheck["current_sample_bytes"])
        with self.assertRaisesRegex(RuntimeError, "unsupported_run_kind"):
            home_probe.run_profile("six-hour", {})

    def test_candidate_queue_is_persistent_deduplicated_and_drops_current_route(self):
        one = {
            "candidate_id": "a" * 64,
            "channel_key": "cctv1",
            "url": "https://candidate.test/one.m3u8",
        }
        changed = dict(one, url="https://candidate.test/updated.m3u8")
        current = {
            "candidate_id": "b" * 64,
            "channel_key": "cctv1",
            "url": "https://current.test/one.m3u8",
        }
        queue = home_probe.merge_candidate_queue(
            [one],
            [changed, current],
            {"cctv1": "https://current.test/one.m3u8"},
        )
        self.assertEqual([changed], queue)

    def test_0200_primary_consumes_candidates_but_1300_never_scans_them(self):
        formal = b"#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://current.test/one.m3u8\n"
        candidate = make_candidate({
            "name": "CCTV-1",
            "url": "https://candidate.test/one.m3u8",
            "sources": ["source-a"],
        })
        manifest = {
            "schema": CANDIDATE_SCHEMA,
            "generated_utc": "2027-01-15T08:00:00Z",
            "source_revision": "test",
            "scope": ["cctv", "provincial_satellite"],
            "formal_playlist": {
                "url": "https://repo.test/tv-core.m3u",
                "sha256": hashlib.sha256(formal).hexdigest(),
                "channel_count": 1,
            },
            "cloud_stream_probe_performed": False,
            "home_verified": False,
            "production_eligible": False,
            "candidate_count": 1,
            "candidate_set_sha256": object_sha256([candidate]),
            "candidates": [candidate],
        }

        def good_probe(name, url, *, floor, **_kwargs):
            row = home_probe.empty_result(name, url, floor)
            row.update({
                "observed_status": "GOOD",
                "status": "GOOD",
                "sample_count": 2,
                "deep_checked": True,
                "width": 1920,
                "height": floor,
                "codec": "h264",
            })
            return row

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            home_probe, "fetch_playlist", return_value=(formal, "https://repo.test/tv-core.m3u", 0.1)
        ), mock.patch.object(
            home_probe,
            "fetch_candidate_manifest",
            return_value=(manifest, b"candidate-json", "https://repo.test/home-candidates.json"),
        ) as candidate_fetch, mock.patch.object(home_probe, "probe_route", side_effect=good_probe):
            config = {
                "probe_id": "home-ac86u-test",
                "output_dir": temporary,
                "playlist_url": "https://repo.test/tv-core.m3u",
                "candidate_manifest_url": "https://repo.test/home-candidates.json",
                "maximum_load1": 10000,
                "minimum_mem_available_kib": 1,
            }
            primary, state = home_probe.run(config, run_kind="primary-0200", now_epoch=1_800_000_000)
            self.assertEqual("accepted", state["candidate_manifest_state"])
            self.assertEqual(1, primary["summary"]["candidate_confirmed"])
            self.assertEqual(1, primary["summary"]["qualified_backups"])
            self.assertEqual(0, primary["summary"]["candidate_queue_remaining"])
            self.assertTrue((Path(temporary) / "qualified-backups.json").exists())
            candidate_fetch.assert_called_once()

            candidate_fetch.reset_mock()
            recheck, _ = home_probe.run(config, run_kind="recheck-1300", now_epoch=1_800_000_100)
            self.assertEqual("not_requested", recheck["policy"]["candidate_manifest_state"])
            candidate_fetch.assert_not_called()

    def test_0200_refreshes_expiring_home_backups_even_when_github_candidates_are_disabled(self):
        formal = b"#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://current.test/cctv1.m3u8\n"

        def good(name, url, *, floor, **_kwargs):
            return measured(name, url, floor, "GOOD")

        refresh_time = NOW + 24 * 3600
        with tempfile.TemporaryDirectory() as temporary:
            seed_backup_pool(temporary, formal, [1])
            with mock.patch.object(
                home_probe, "fetch_playlist", return_value=(formal, "https://repo.test/tv-core.m3u", 0.1)
            ), mock.patch.object(home_probe, "probe_route", side_effect=good) as probe, mock.patch.object(
                home_probe, "fetch_candidate_manifest"
            ) as candidate_fetch:
                report, _ = home_probe.run({
                    "probe_id": "home-ac86u-test",
                    "output_dir": temporary,
                    "candidate_manifest_url": "",
                    "maximum_load1": 10000,
                    "minimum_mem_available_kib": 1,
                }, run_kind="primary-0200", now_epoch=refresh_time)
            pool = home_probe.load_json(Path(temporary) / "qualified-backups.json")
        self.assertEqual("disabled", report["policy"]["candidate_manifest_state"])
        self.assertEqual(1, report["summary"]["candidate_confirmed"])
        self.assertEqual(home_probe.utc_text(refresh_time), pool["backups"][0]["last_verified_utc"])
        self.assertEqual(2, probe.call_count)  # One current route plus one expiring backup.
        candidate_fetch.assert_not_called()

    def test_resource_guard_protects_router(self):
        self.assertIn("load", home_probe.resource_guard({"load1": 1.6, "mem_available_kib": 200000}, {}))
        self.assertIn("memory", home_probe.resource_guard({"load1": 0.2, "mem_available_kib": 64000}, {}))


if __name__ == "__main__":
    unittest.main()
