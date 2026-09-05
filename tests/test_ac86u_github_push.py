import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from router.ac86u import github_pair
from router.ac86u.home_contract import REPORT_SCHEMA, ROUTE_CONTEXT, url_sha256
from router.ac86u.push_home_report import push


def report(probe_id="home-ac86u-test", generated="2026-09-02T13:00:00Z"):
    current_url = "https://current.test/cctv1.m3u8"
    return {
        "schema": REPORT_SCHEMA,
        "probe_id": probe_id,
        "generated_utc": generated,
        "run_kind": "recheck-1300",
        "run_status": "COMPLETED",
        "production_modified": False,
        "actionable": False,
        "route_context": ROUTE_CONTEXT,
        "formal_playlist": {
            "url": "https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u",
            "sha256": hashlib.sha256(b"formal").hexdigest(),
            "channel_count": 1,
        },
        "baseline": {
            "home_network_ok": True,
            "github_reachable": True,
            "route_verified": True,
            "mass_failure_circuit_breaker": False,
        },
        "current_results": [{
            "channel_key": "cctv1",
            "name": "CCTV-1",
            "url": current_url,
            "url_sha256": url_sha256(current_url),
            "status": "GOOD",
            "failure_confirmed": False,
            "attempt_count": 1,
            "verification": {
                "sample_count": 2,
                "startup_s": 0.8,
                "min_download_mbps": 20.0,
                "stream_mbps": 6.0,
                "headroom_ratio": 3.333,
                "width": 1920,
                "height": 1080,
                "codec": "h264",
                "fps": 50.0,
                "bitrate_mbps": 6.0,
                "deep_checked": True,
            },
        }],
        "candidate_results": [],
        "decisions": [{
            "channel_key": "cctv1",
            "action": "KEEP",
            "reason": "healthy_home_route",
            "replacement_candidate_id": None,
        }],
    }


def run_git(args, cwd=None):
    return subprocess.run(
        [shutil.which("git") or "git", *args],
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )


def report_remote(root: Path) -> Path:
    remote = root / "reports.git"
    seed = root / "seed"
    run_git(["init", "--bare", str(remote)])
    run_git(["init", "-b", "home-reports", str(seed)])
    run_git(["config", "user.name", "test"], seed)
    run_git(["config", "user.email", "test@example.invalid"], seed)
    (seed / ".gitkeep").write_text("home report branch\n", encoding="utf-8")
    run_git(["add", ".gitkeep"], seed)
    run_git(["commit", "-m", "bootstrap"], seed)
    run_git(["remote", "add", "origin", str(remote)], seed)
    run_git(["push", "origin", "home-reports"], seed)
    return remote


def master_rules(*, approvals=0, ruleset_id=917, source="pppaaasss/-"):
    common = {
        "ruleset_id": ruleset_id,
        "ruleset_source_type": "Repository",
        "ruleset_source": source,
    }
    return [
        {
            **common,
            "type": "pull_request",
            "parameters": {
                "allowed_merge_methods": ["squash"],
                "required_approving_review_count": approvals,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": False,
            },
        },
        {**common, "type": "deletion"},
        {**common, "type": "non_fast_forward"},
    ]


class AC86UGitHubPushTests(unittest.TestCase):
    def config(self, root: Path, *, enabled: bool) -> Path:
        path = root / "config.json"
        path.write_text(json.dumps({
            "probe_id": "home-ac86u-test",
            "output_dir": str(root / "state"),
            "github_push_enabled": enabled,
            "github_repository": "pppaaasss/-",
            "github_report_branch": "home-reports",
            "git": shutil.which("git") or "git",
        }), encoding="utf-8")
        return path

    def latest(self, root: Path) -> Path:
        path = root / "state" / "latest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(report()), encoding="utf-8")
        return path

    def test_disabled_transport_queues_valid_report_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root, enabled=False)
            latest = self.latest(root)
            self.assertFalse(push(config, latest))
            pending = list((root / "state" / "pending-reports").glob("*.json"))
            state = json.loads((root / "state" / "github-state.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(pending))
        self.assertEqual(1, state["pending_reports"])

    def test_queued_report_pushes_to_dedicated_branch_and_is_acknowledged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = report_remote(root)
            config = self.config(root, enabled=False)
            latest = self.latest(root)
            push(config, latest)
            value = json.loads(config.read_text(encoding="utf-8"))
            value["github_push_enabled"] = True
            config.write_text(json.dumps(value), encoding="utf-8")
            self.assertTrue(push(
                config,
                latest,
                remote_url_override=str(remote),
                transport_env_override=dict(os.environ),
            ))
            names = run_git([
                "--git-dir", str(remote),
                "ls-tree", "-r", "--name-only", "home-reports", "--", "inbox/home-ac86u-test",
            ]).stdout.splitlines()
            state = json.loads((root / "state" / "github-state.json").read_text(encoding="utf-8"))
            pending = list((root / "state" / "pending-reports").glob("*.json"))
        self.assertEqual(1, len(names))
        self.assertTrue(names[0].startswith("inbox/home-ac86u-test/"))
        self.assertEqual(1, state["successful_reports"])
        self.assertEqual(0, state["pending_reports"])
        self.assertEqual([], pending)

    def test_failed_github_connection_keeps_local_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root, enabled=True)
            latest = self.latest(root)
            with self.assertRaisesRegex(RuntimeError, "clone"):
                push(
                    config,
                    latest,
                    remote_url_override=str(root / "missing.git"),
                    transport_env_override=dict(os.environ),
                )
            pending = list((root / "state" / "pending-reports").glob("*.json"))
            state = json.loads((root / "state" / "github-state.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(pending))
        self.assertEqual(1, state["pending_reports"])
        self.assertIn("last_error", state)

    def test_tampered_queued_report_is_never_pushed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = report_remote(root)
            config = self.config(root, enabled=False)
            latest = self.latest(root)
            push(config, latest)
            queued = next((root / "state" / "pending-reports").glob("*.json"))
            queued.write_text("{}", encoding="utf-8")
            latest.write_text(json.dumps(report(generated="2026-09-02T14:00:00Z")), encoding="utf-8")
            value = json.loads(config.read_text(encoding="utf-8"))
            value["github_push_enabled"] = True
            config.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "schema"):
                push(
                    config,
                    latest,
                    remote_url_override=str(remote),
                    transport_env_override=dict(os.environ),
                )
            names = run_git([
                "--git-dir", str(remote),
                "ls-tree", "-r", "--name-only", "home-reports", "--", "inbox",
            ]).stdout.splitlines()
        self.assertEqual([], names)


class AC86UGitHubPairTests(unittest.TestCase):
    def test_enable_records_the_verified_public_ruleset_before_allowing_pushes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.json"
            path.write_text(json.dumps({
                "probe_id": "home-ac86u-test",
                "github_repository": "pppaaasss/-",
                "github_report_branch": "home-reports",
                "github_push_enabled": False,
                "protected_publishing_ready": False,
                "actionable": True,
            }), encoding="utf-8")
            known = root / "known_hosts"
            with (
                mock.patch.object(
                    github_pair,
                    "verify_protected_publisher",
                    return_value=("a" * 64, "b" * 64, 917),
                ),
                mock.patch.object(github_pair, "pin_host_key", return_value=known),
                mock.patch.object(github_pair, "authenticate") as authenticate,
            ):
                enabled = github_pair.enable(path)
            authenticate.assert_called_once_with(enabled, known)
            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(stored["github_push_enabled"])
        self.assertTrue(stored["protected_publishing_ready"])
        self.assertFalse(stored["actionable"])
        self.assertEqual(917, stored["master_ruleset_id"])
        self.assertEqual("a" * 64, stored["publisher_config_sha256"])
        self.assertEqual("b" * 64, stored["master_rules_sha256"])

    def test_official_ed25519_fingerprint_is_pinned_before_enabling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            known = root / "known_hosts"
            config = {
                "ssh_keyscan": "ssh-keyscan",
                "ssh_keygen": "ssh-keygen",
                "github_known_hosts": str(known),
            }
            scan = subprocess.CompletedProcess(
                [],
                0,
                stdout=b"[ssh.github.com]:443 ssh-ed25519 AAAAC3NzaTest\n",
                stderr=b"",
            )
            checked = subprocess.CompletedProcess(
                [],
                0,
                stdout=f"256 {github_pair.GITHUB_ED25519_FINGERPRINT} host (ED25519)\n".encode(),
                stderr=b"",
            )
            with mock.patch.object(github_pair.subprocess, "run", side_effect=[scan, checked]):
                result = github_pair.pin_host_key(config)
            self.assertEqual(known, result)
            self.assertEqual(scan.stdout, known.read_bytes())
            self.assertEqual(0, known.stat().st_mode & 0o077)

        with self.assertRaisesRegex(RuntimeError, "differs"):
            github_pair.pin_host_key({}, "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    def test_enable_refuses_any_unpinned_repository_or_branch(self):
        with self.assertRaisesRegex(RuntimeError, "repository"):
            github_pair.validate_destination({
                "github_repository": "someone/else",
                "github_report_branch": "home-reports",
            })
        with self.assertRaisesRegex(RuntimeError, "isolated"):
            github_pair.validate_destination({
                "github_repository": "pppaaasss/-",
                "github_report_branch": "master",
            })
        with self.assertRaisesRegex(RuntimeError, "publishing is not ready"):
            github_pair.validate_destination({
                "github_repository": "pppaaasss/-",
                "github_report_branch": "home-reports",
                "protected_publishing_ready": False,
            })
        github_pair.validate_destination({
            "github_repository": "pppaaasss/-",
            "github_report_branch": "home-reports",
            "protected_publishing_ready": True,
        })

    def test_publisher_and_public_master_rules_must_match_this_probe(self):
        router = {
            "probe_id": "home-ac86u-test",
            "github_repository": "pppaaasss/-",
            "github_report_branch": "home-reports",
        }
        publisher = {
            "schema": github_pair.PUBLISHER_CONFIG_SCHEMA,
            "enabled": True,
            "expected_probe_id": "home-ac86u-test",
            "repository": "pppaaasss/-",
            "report_branch": "home-reports",
            "branch_protection_required": True,
        }
        rules = master_rules()
        self.assertEqual(917, github_pair.validate_protected_publisher(publisher, rules, router))

        wrong_probe = dict(publisher, expected_probe_id="home-ac86u-other")
        with self.assertRaisesRegex(RuntimeError, "assigned"):
            github_pair.validate_protected_publisher(wrong_probe, rules, router)
        with self.assertRaisesRegex(RuntimeError, "no-human-approval ruleset"):
            github_pair.validate_protected_publisher(publisher, [], router)
        with self.assertRaisesRegex(RuntimeError, "no-human-approval ruleset"):
            github_pair.validate_protected_publisher(publisher, master_rules(approvals=1), router)
        no_squash = master_rules()
        no_squash[0]["parameters"]["allowed_merge_methods"] = ["merge"]
        with self.assertRaisesRegex(RuntimeError, "no-human-approval ruleset"):
            github_pair.validate_protected_publisher(publisher, no_squash, router)
        manual_conversation = master_rules()
        manual_conversation[0]["parameters"]["required_review_thread_resolution"] = True
        with self.assertRaisesRegex(RuntimeError, "no-human-approval ruleset"):
            github_pair.validate_protected_publisher(publisher, manual_conversation, router)
        with self.assertRaisesRegex(RuntimeError, "no-human-approval ruleset"):
            github_pair.validate_protected_publisher(
                publisher,
                master_rules(source="another/repository"),
                router,
            )

        split = master_rules()
        split[2]["ruleset_id"] = 918
        with self.assertRaisesRegex(RuntimeError, "no-human-approval ruleset"):
            github_pair.validate_protected_publisher(publisher, split, router)


if __name__ == "__main__":
    unittest.main()
