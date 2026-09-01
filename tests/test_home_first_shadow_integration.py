import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from router.ac86u import home_probe
from router.ac86u.home_contract import (
    CANDIDATE_SCHEMA,
    ROUTE_CONTEXT,
    make_candidate,
    object_sha256,
)
from router.ac86u.push_home_report import push
from scripts.publish_home_decisions import (
    CONFIG_SCHEMA,
    PRODUCTION_FILES,
    publish_latest,
)


ROOT = Path(__file__).resolve().parents[1]
FORMAL_URL = "https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u"
PROBE_ID = "home-ac86u-shadow"
UTC = timezone.utc


def run_git(args, cwd=None):
    return subprocess.run(
        [shutil.which("git") or "git", *args],
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )


def report_remote(root: Path) -> Path:
    remote = root / "home-reports.git"
    seed = root / "report-seed"
    run_git(["init", "--bare", str(remote)])
    run_git(["init", "-b", "home-reports", str(seed)])
    run_git(["config", "user.name", "shadow-simulation"], seed)
    run_git(["config", "user.email", "shadow@example.invalid"], seed)
    (seed / ".gitkeep").write_text("home report branch\n", encoding="utf-8")
    run_git(["add", ".gitkeep"], seed)
    run_git(["commit", "-m", "bootstrap"], seed)
    run_git(["remote", "add", "origin", str(remote)], seed)
    run_git(["push", "origin", "home-reports"], seed)
    return remote


def measured(name: str, url: str, floor: int) -> dict:
    row = home_probe.empty_result(name, url, floor)
    row.update({
        "observed_status": "GOOD",
        "status": "GOOD",
        "sample_count": 2,
        "startup_s": 0.8,
        "min_download_mbps": 20.0,
        "stream_mbps": 6.0,
        "headroom_ratio": 3.333,
        "deep_checked": True,
        "width": 3840 if floor == 2160 else 1920,
        "height": floor,
        "codec": "h264",
        "fps": 50.0,
        "bitrate_mbps": 6.0,
        "error": "",
    })
    return row


class HomeFirstShadowIntegrationTests(unittest.TestCase):
    def test_two_primary_runs_and_one_recheck_need_no_hong_kong_and_change_no_playlist(self):
        production_before = {name: (ROOT / name).read_bytes() for name in PRODUCTION_FILES}
        formal = production_before["tv-core.m3u"]
        formal_count = len(home_probe.parse_playlist(formal))
        first_primary = datetime(2026, 9, 1, 18, 0, tzinfo=UTC).timestamp()
        candidate = make_candidate({
            "name": "CCTV-8",
            "url": "https://candidate.test/cctv8-home.m3u8",
            "sources": ["shadow-simulation"],
        })
        manifest = {
            "schema": CANDIDATE_SCHEMA,
            "generated_utc": home_probe.utc_text(first_primary - 90 * 60),
            "source_revision": "shadow-simulation",
            "scope": ["cctv", "provincial_satellite"],
            "formal_playlist": {
                "url": FORMAL_URL,
                "sha256": hashlib.sha256(formal).hexdigest(),
                "channel_count": formal_count,
            },
            "cloud_stream_probe_performed": False,
            "home_verified": False,
            "production_eligible": False,
            "candidate_count": 1,
            "candidate_set_sha256": object_sha256([candidate]),
            "candidates": [candidate],
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "router-state"
            remote = report_remote(root)
            config_path = root / "router-config.json"
            config = {
                "probe_id": PROBE_ID,
                "output_dir": str(output),
                "playlist_url": FORMAL_URL,
                "candidate_manifest_url": "https://raw.githubusercontent.com/pppaaasss/-/master/harvest/home-candidates.json",
                "route_context": ROUTE_CONTEXT,
                "actionable": False,
                "maximum_load1": 10000,
                "minimum_mem_available_kib": 1,
                "github_push_enabled": True,
                "github_repository": "pppaaasss/-",
                "github_report_branch": "home-reports",
                "git": shutil.which("git") or "git",
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            def good_probe(name, url, *, floor, **_kwargs):
                return measured(name, url, floor)

            schedule = (
                ("primary-0200", first_primary),
                ("recheck-1300", first_primary + 11 * 3600),
                ("primary-0200", first_primary + 24 * 3600),
            )
            reports = []
            with mock.patch.object(
                home_probe,
                "fetch_playlist",
                return_value=(formal, FORMAL_URL, 0.1),
            ), mock.patch.object(
                home_probe,
                "fetch_candidate_manifest",
                return_value=(manifest, b"shadow-candidate-manifest", "https://repo.test/home-candidates.json"),
            ) as candidate_fetch, mock.patch.object(
                home_probe,
                "probe_route",
                side_effect=good_probe,
            ):
                for run_kind, epoch in schedule:
                    report, _state = home_probe.run(config, run_kind=run_kind, now_epoch=epoch)
                    reports.append(report)
                    self.assertTrue(push(
                        config_path,
                        output / "latest.json",
                        remote_url_override=str(remote),
                        transport_env_override=dict(os.environ),
                    ))

            self.assertEqual(2, candidate_fetch.call_count)
            self.assertEqual([kind for kind, _epoch in schedule], [row["run_kind"] for row in reports])
            self.assertTrue(all(row["actionable"] is False for row in reports))
            self.assertTrue(all(row["production_modified"] is False for row in reports))
            self.assertEqual("not_requested", reports[1]["policy"]["candidate_manifest_state"])
            self.assertGreaterEqual(reports[0]["summary"]["qualified_backups"], 1)

            report_files = run_git([
                "--git-dir", str(remote), "ls-tree", "-r", "--name-only",
                "home-reports", "--", f"inbox/{PROBE_ID}",
            ]).stdout.splitlines()
            self.assertEqual(3, len(report_files))

            checkout = root / "report-checkout"
            run_git(["clone", "--quiet", "--branch", "home-reports", str(remote), str(checkout)])
            publish_root = root / "publisher"
            (publish_root / "config").mkdir(parents=True)
            for name, raw in production_before.items():
                (publish_root / name).write_bytes(raw)
            (publish_root / "config/home-route-feedback.json").write_text(
                json.dumps({"good": {}, "bad": {}}), encoding="utf-8"
            )
            publisher_config = publish_root / "config/home-publisher.json"
            publisher_config.write_text(json.dumps({
                "schema": CONFIG_SCHEMA,
                "enabled": True,
                "expected_probe_id": PROBE_ID,
                "repository": "pppaaasss/-",
                "report_branch": "home-reports",
                "formal_playlist": "tv-core.m3u",
                "formal_playlist_url": FORMAL_URL,
                "production_files": list(PRODUCTION_FILES),
                "maximum_report_age_hours": 18,
                "exact_reported_route_only": True,
                "branch_protection_required": True,
                "home_feedback": "config/home-route-feedback.json",
                "receipt_path": "home-publish/latest.json",
            }), encoding="utf-8")
            result = publish_latest(
                root=publish_root,
                config_path=publisher_config,
                inbox=checkout / "inbox",
                now_epoch=first_primary + 24 * 3600 + 10 * 60,
                apply=True,
            )
            self.assertEqual("shadow", result["status"])
            self.assertFalse((publish_root / "home-publish/latest.json").exists())
            self.assertEqual(
                production_before,
                {name: (publish_root / name).read_bytes() for name in PRODUCTION_FILES},
            )

        self.assertEqual(
            production_before,
            {name: (ROOT / name).read_bytes() for name in PRODUCTION_FILES},
        )


if __name__ == "__main__":
    unittest.main()
