from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from scripts import candidate_release as release

ROOT = Path(__file__).resolve().parents[1]


class ReleaseSafetyTests(unittest.TestCase):
    def manifest(self) -> dict:
        hashes = {name: f"hash-{name}" for name in release.PRODUCTION}
        return {
            "build_ok": True,
            "production_hashes_unchanged": True,
            "missing_core": [],
            "changed_core": ["cctv8"],
            "core_streaks": {"cctv8": 2},
            "production_sha256_before": hashes,
        }

    def validate_manifest(self, data: dict, visual: bool, run_id: str) -> None:
        with mock.patch.object(release, "current_hashes", return_value=data["production_sha256_before"]), \
             mock.patch.object(release.Path, "exists", return_value=True), \
             mock.patch.object(release.Path, "stat") as stat:
            stat.return_value.st_size = 1024
            release.validate(data, visual_confirmed=visual, audit_run_id=run_id)

    def test_changed_core_needs_two_consecutive_scans(self) -> None:
        data = self.manifest()
        data["core_streaks"]["cctv8"] = 1
        with self.assertRaises(SystemExit):
            self.validate_manifest(data, True, "12345")

    def test_changed_core_needs_visual_artifact_confirmation(self) -> None:
        data = self.manifest()
        with self.assertRaises(SystemExit):
            self.validate_manifest(data, False, "12345")
        with self.assertRaises(SystemExit):
            self.validate_manifest(data, True, "")

    def test_reviewed_two_round_candidate_can_pass_gate(self) -> None:
        self.validate_manifest(self.manifest(), True, "12345")

    def test_missing_core_blocks_promotion(self) -> None:
        data = self.manifest()
        data["missing_core"] = ["cctv4k"]
        with self.assertRaises(SystemExit):
            self.validate_manifest(data, True, "12345")

    def test_candidate_schedule_can_only_stage_candidate_directory(self) -> None:
        text = (ROOT / ".github/workflows/build-candidate.yml").read_text(encoding="utf-8")
        self.assertIn("git add candidate/", text)
        self.assertNotIn("git add tv-easy.m3u", text)
        self.assertNotIn("git pull --rebase", text)
        self.assertIn("production-before.sha256", text)
        self.assertIn("production-after.sha256", text)

    def test_promotion_and_rollback_share_production_lock(self) -> None:
        for name in ("promote-candidate.yml", "rollback-production.yml", "pin-viewer-channels.yml"):
            text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            self.assertIn("group: production-publish-lock", text)

    def test_rollback_restores_all_four_production_files(self) -> None:
        text = (ROOT / ".github/workflows/rollback-production.yml").read_text(encoding="utf-8")
        self.assertIn("git checkout '${{ steps.previous.outputs.sha }}' -- tv-easy.m3u tv.m3u tv-all.m3u tv-core.m3u", text)


if __name__ == "__main__":
    unittest.main()
