import functools
import hashlib
import http.server
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from router.ac86u import home_probe
from router.ac86u.home_contract import CANDIDATE_SCHEMA, make_candidate, object_sha256
from scripts.home_probe_report import validate_home_report


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def result(name="CCTV-1", url="https://one.test/live.m3u8", status="UNKNOWN"):
    row = home_probe.empty_result(name, url, 1080)
    row["observed_status"] = status
    row["status"] = status
    return row


class AC86UHomeProbeTests(unittest.TestCase):
    def test_generated_1300_report_matches_existing_cross_host_schema(self):
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
                validate_home_report(report)
                self.assertEqual("GOOD", report["results"][0]["status"])
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

    def test_dead_requires_three_runs_spanning_six_hours(self):
        config = {"circuit_breaker_min_unknown": 20}
        state = {}
        for offset in (0, 3 * 3600, 6 * 3600):
            rows = [result()]
            state, circuit = home_probe.update_current_state(
                rows,
                state,
                now_epoch=1_800_000_000 + offset,
                mode="light",
                config=config,
            )
        self.assertFalse(circuit)
        self.assertEqual("DEAD", rows[0]["status"])
        self.assertTrue(rows[0]["home_dead_confirmed"])
        self.assertEqual(3, rows[0]["consecutive_failures"])

    def test_mass_failure_circuit_does_not_advance_failures(self):
        state = {"channels": {f"CCTV-{i}": {"last_url": f"https://{i}.test/live", "consecutive_failures": 1} for i in range(1, 13)}}
        rows = [result(f"CCTV-{i}", f"https://{i}.test/live") for i in range(1, 13)]
        state, circuit = home_probe.update_current_state(
            rows,
            state,
            now_epoch=1_800_000_000,
            mode="light",
            config={"circuit_breaker_min_unknown": 12, "circuit_breaker_unknown_ratio": 0.35},
        )
        self.assertTrue(circuit)
        self.assertTrue(all(row["consecutive_failures"] == 1 for row in rows))

    def test_candidate_needs_two_good_deep_runs_six_hours_apart(self):
        state = {}
        first = result(status="GOOD")
        first.update(deep_checked=True, height=1080, sample_count=2)
        home_probe.update_candidate_state([first], state, now_epoch=1_800_000_000, config={})
        self.assertFalse(first["candidate_confirmed"])
        second = result(status="GOOD")
        second.update(deep_checked=True, height=1080, sample_count=2)
        home_probe.update_candidate_state([second], state, now_epoch=1_800_000_000 + 6 * 3600, config={})
        self.assertTrue(second["candidate_confirmed"])

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
            self.assertEqual(0, primary["summary"]["candidate_queue_remaining"])
            candidate_fetch.assert_called_once()

            candidate_fetch.reset_mock()
            recheck, _ = home_probe.run(config, run_kind="recheck-1300", now_epoch=1_800_000_100)
            self.assertEqual("not_requested", recheck["policy"]["candidate_manifest_state"])
            candidate_fetch.assert_not_called()

    def test_resource_guard_protects_router(self):
        self.assertIn("load", home_probe.resource_guard({"load1": 1.6, "mem_available_kib": 200000}, {}))
        self.assertIn("memory", home_probe.resource_guard({"load1": 0.2, "mem_available_kib": 64000}, {}))


if __name__ == "__main__":
    unittest.main()
