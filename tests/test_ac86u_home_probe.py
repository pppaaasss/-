import unittest
from unittest import mock

from router.ac86u import home_probe


def result(name="CCTV-1", url="https://one.test/live.m3u8", status="UNKNOWN"):
    row = home_probe.empty_result(name, url, 1080)
    row["observed_status"] = status
    row["status"] = status
    return row


class AC86UHomeProbeTests(unittest.TestCase):
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

    def test_auto_mode_is_deep_only_after_72_hours(self):
        now = 1_800_000_000
        state = {"last_deep_utc": home_probe.utc_text(now - 71 * 3600)}
        self.assertEqual("light", home_probe.selected_mode("auto", state, now, {}))
        state["last_deep_utc"] = home_probe.utc_text(now - 72 * 3600)
        self.assertEqual("deep", home_probe.selected_mode("auto", state, now, {}))

    def test_resource_guard_protects_router(self):
        self.assertIn("load", home_probe.resource_guard({"load1": 1.6, "mem_available_kib": 200000}, {}))
        self.assertIn("memory", home_probe.resource_guard({"load1": 0.2, "mem_available_kib": 64000}, {}))


if __name__ == "__main__":
    unittest.main()
