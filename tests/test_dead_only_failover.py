import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from scripts.dead_only_failover import PLAYLISTS, run
from scripts.hk_probe import ProbeResult


class DeadOnlyFailoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "candidate").mkdir()
        (self.root / "harvest").mkdir()
        self.old = "http://old.test/cctv1.m3u8"
        self.new = "http://new.test/cctv1.m3u8"
        playlist = f"#EXTM3U\n#EXTINF:-1,CCTV-1\n{self.old}\n"
        for name in PLAYLISTS:
            (self.root / name).write_text(playlist, encoding="utf-8")
        (self.root / "candidate/tv-core.m3u").write_text(
            f"#EXTM3U\n#EXTINF:-1,CCTV-1\n{self.new}\n", encoding="utf-8"
        )
        (self.root / "harvest/candidates.jsonl").write_text(
            json.dumps({
                "name": "CCTV-1", "url": self.new, "sources": ["fixed"],
                "hk_verified": {"height": 1080, "segment_ok": True},
            }) + "\n", encoding="utf-8"
        )
        (self.root / "harvest/pending.jsonl").write_text("", encoding="utf-8")
        (self.root / "config/home-route-feedback.json").write_text(
            json.dumps({"good": {}, "bad": {}}), encoding="utf-8"
        )
        (self.root / "config/dead-only-failover.json").write_text(json.dumps({
            "enabled": True,
            "fixed_candidate_playlist": "candidate/tv-core.m3u",
            "verified_pool": "harvest/candidates.jsonl",
            "pending_candidate_pool": "harvest/pending.jsonl",
            "dynamic_pending_enabled": True,
            "home_feedback": "config/home-route-feedback.json",
            "current_recheck_attempts": 2,
            "candidate_confirm_attempts": 2,
            "maximum_updates_per_cycle": 3,
            "maximum_verified_candidates_per_channel": 3,
            "maximum_pending_candidates_per_channel": 8,
            "minimum_height_default": 1080,
            "minimum_height_overrides": {},
            "minimum_source_references": 1,
        }), encoding="utf-8")
        self.report = self.root / "report.json"

    def tearDown(self):
        self.temp.cleanup()

    def args(self):
        return Namespace(
            repo_root=str(self.root),
            config="config/dead-only-failover.json",
            formal_report=str(self.report),
            apply=True,
        )

    def write_report(self, status="DEAD", circuit=False):
        self.report.write_text(json.dumps({
            "summary": {"circuit_breaker_open": circuit},
            "results": [{
                "name": "CCTV-1", "url": self.old, "status": status,
                "hk_dead_confirmed": status == "DEAD", "consecutive_failures": 3,
                "failure_age_hours": 8, "min_height": 1080,
            }],
        }), encoding="utf-8")

    def probe(self, item):
        _, url, floor = item
        if url == self.old:
            return ProbeResult(name="CCTV-1", url=url, status="UNKNOWN", min_height=floor)
        return ProbeResult(
            name="CCTV-1", url=url, status="GOOD", height=1080, min_height=floor,
            segment_ok=True, codec="h264", bitrate_mbps=3,
        )

    def test_only_confirmed_dead_route_is_replaced_atomically(self):
        self.write_report()
        result = run(self.args(), probe=self.probe)
        self.assertEqual(1, len(result["selected_updates"]))
        self.assertEqual(set(PLAYLISTS), set(result["changed_files"]))
        for name in PLAYLISTS:
            self.assertIn(self.new, (self.root / name).read_text(encoding="utf-8"))

    def test_good_or_unknown_status_never_changes_playlists(self):
        self.write_report(status="UNKNOWN")
        result = run(self.args(), probe=self.probe)
        self.assertFalse(result["selected_updates"])
        self.assertIn(self.old, (self.root / "tv.m3u").read_text(encoding="utf-8"))

    def test_recovered_current_route_is_not_replaced(self):
        self.write_report()
        def recovered(item):
            name, url, floor = item
            return ProbeResult(name=name, url=url, status="GOOD", height=1080, min_height=floor, segment_ok=True)
        result = run(self.args(), probe=recovered)
        self.assertFalse(result["selected_updates"])

    def test_mass_failure_circuit_prevents_all_changes(self):
        self.write_report(circuit=True)
        result = run(self.args(), probe=self.probe)
        self.assertFalse(result["selected_updates"])

    def test_home_rejected_spare_is_never_used(self):
        (self.root / "config/home-route-feedback.json").write_text(
            json.dumps({"good": {}, "bad": {"cctv1": [{"url": self.new}]}}), encoding="utf-8"
        )
        self.write_report()
        result = run(self.args(), probe=self.probe)
        self.assertFalse(result["selected_updates"])

    def test_identical_duplicate_tiles_are_replaced_together(self):
        path = self.root / "tv-easy.m3u"
        path.write_text(path.read_text(encoding="utf-8") + f"#EXTINF:-1,CCTV-1\n{self.old}\n", encoding="utf-8")
        self.write_report()
        run(self.args(), probe=self.probe)
        self.assertEqual(2, path.read_text(encoding="utf-8").count(self.new))

    def test_confirmed_dead_can_use_new_github_pending_candidate(self):
        dynamic = "http://dynamic.test/cctv1.m3u8"
        (self.root / "candidate/tv-core.m3u").write_text("#EXTM3U\n", encoding="utf-8")
        (self.root / "harvest/candidates.jsonl").write_text("", encoding="utf-8")
        (self.root / "harvest/pending.jsonl").write_text(json.dumps({
            "name": "CCTV-1 高清", "url": dynamic, "sources": ["github-upstream"],
        }) + "\n", encoding="utf-8")
        self.write_report()

        def dynamic_probe(item):
            name, url, floor = item
            if url == self.old:
                return ProbeResult(name=name, url=url, status="UNKNOWN", min_height=floor)
            return ProbeResult(
                name=name, url=url, status="GOOD", height=1080, min_height=floor,
                segment_ok=True, codec="h264", bitrate_mbps=3,
            )

        result = run(self.args(), probe=dynamic_probe)
        self.assertEqual(dynamic, result["selected_updates"][0]["new_url"])
        self.assertEqual("github_pending", result["selected_updates"][0]["source_kind"])

    def test_new_candidates_are_not_probed_until_route_is_confirmed_dead(self):
        dynamic = "http://dynamic.test/cctv1.m3u8"
        (self.root / "harvest/pending.jsonl").write_text(json.dumps({
            "name": "CCTV-1", "url": dynamic, "sources": ["github-upstream"],
        }) + "\n", encoding="utf-8")
        self.write_report(status="UNKNOWN")
        calls = []

        def record(item):
            calls.append(item)
            return self.probe(item)

        result = run(self.args(), probe=record)
        self.assertFalse(result["selected_updates"])
        self.assertEqual([], calls)

    def test_failed_fixed_spare_falls_back_to_new_github_candidate(self):
        dynamic = "http://dynamic.test/cctv1-backup.m3u8"
        (self.root / "harvest/pending.jsonl").write_text(json.dumps({
            "name": "CCTV-1", "url": dynamic, "sources": ["github-upstream"],
        }) + "\n", encoding="utf-8")
        self.write_report()

        def fallback_probe(item):
            name, url, floor = item
            if url in {self.old, self.new}:
                return ProbeResult(name=name, url=url, status="UNKNOWN", min_height=floor)
            return ProbeResult(
                name=name, url=url, status="GOOD", height=1080, min_height=floor,
                segment_ok=True, codec="h264", bitrate_mbps=3,
            )

        result = run(self.args(), probe=fallback_probe)
        self.assertEqual(dynamic, result["selected_updates"][0]["new_url"])
        self.assertEqual("github_pending", result["selected_updates"][0]["source_kind"])

    def test_720p_pending_candidate_cannot_replace_1080p_route(self):
        dynamic = "http://dynamic.test/cctv1-720p.m3u8"
        (self.root / "candidate/tv-core.m3u").write_text("#EXTM3U\n", encoding="utf-8")
        (self.root / "harvest/candidates.jsonl").write_text("", encoding="utf-8")
        (self.root / "harvest/pending.jsonl").write_text(json.dumps({
            "name": "CCTV-1", "url": dynamic, "sources": ["github-upstream"],
        }) + "\n", encoding="utf-8")
        self.write_report()

        def soft_probe(item):
            name, url, floor = item
            if url == self.old:
                return ProbeResult(name=name, url=url, status="UNKNOWN", min_height=floor)
            return ProbeResult(
                name=name, url=url, status="GOOD", height=720, min_height=floor,
                segment_ok=True, codec="h264", bitrate_mbps=3,
            )

        result = run(self.args(), probe=soft_probe)
        self.assertFalse(result["selected_updates"])

    def test_cctv8k_pending_never_matches_cctv8(self):
        old = self.old
        for name in PLAYLISTS:
            (self.root / name).write_text(f"#EXTM3U\n#EXTINF:-1,CCTV-8\n{old}\n", encoding="utf-8")
        (self.root / "candidate/tv-core.m3u").write_text("#EXTM3U\n", encoding="utf-8")
        (self.root / "harvest/pending.jsonl").write_text(json.dumps({
            "name": "CCTV-8K", "url": "http://dynamic.test/cctv8k.m3u8",
            "sources": ["github-upstream"],
        }) + "\n", encoding="utf-8")
        self.report.write_text(json.dumps({
            "summary": {"circuit_breaker_open": False},
            "results": [{
                "name": "CCTV-8", "url": old, "status": "DEAD",
                "hk_dead_confirmed": True, "consecutive_failures": 3,
                "failure_age_hours": 8, "min_height": 1080,
            }],
        }), encoding="utf-8")
        result = run(self.args(), probe=self.probe)
        self.assertFalse(result["selected_updates"])


if __name__ == "__main__":
    unittest.main()
