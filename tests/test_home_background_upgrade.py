"""Exercise the field updater against a temporary router and acknowledged report."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from router.ac86u import background_upgrade as upgrade
from tests.test_ac86u_github_push import report

REPO = Path(__file__).resolve().parents[1]


class UpgradeTests(unittest.TestCase):
    def exercise(self, bad_hash=False, schedule_only=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'state'; root.mkdir()
            base=Path(tmp)/'installed'; base.mkdir()
            for source in (REPO/'router/ac86u').glob('*.py'):
                (base/source.name).write_bytes(source.read_bytes())
            (base/'run.sh').write_bytes((REPO/'router/ac86u/run.sh').read_bytes())
            cfg=Path(tmp)/'config.json'
            value=report()
            # This test's clock is bound to its report, not the machine's date.
            stamp=upgrade.time.mktime(upgrade.time.strptime(value['generated_utc'], '%Y-%m-%dT%H:%M:%SZ'))
            raw=json.dumps(value).encode()
            config=dict(output_dir=str(root), probe_id=value['probe_id'], daily_worker_enabled=True,
                        github_push_enabled=True, protected_publishing_ready=True,
                        route_context='living-room-path-equivalent', actionable=False)
            cfg.write_text(json.dumps(config))
            (root/'github-state.json').write_text(json.dumps({'successful_report_evidence':{
                value['generated_utc']: {'safe':True,'probe_id':value['probe_id'],
                    'sha256':'0'*64 if bad_hash else hashlib.sha256(raw).hexdigest()}}}))
            (root/'state.json').write_text(json.dumps({'candidate_observations':{},'candidate_queue':[]}))
            jobs=root/'daily-jobs'; jobs.mkdir()
            (jobs/'primary.json').write_text(json.dumps(dict(kind='primary-0200',phase='candidates',state='RUNNING')))
            def download(url,target):
                target.write_bytes(raw if url==upgrade.ACK_URL else (REPO/'router/ac86u'/url.rsplit('/',1)[1]).read_bytes())
            with mock.patch.multiple(upgrade, BASE=base, ROOT=root, CONFIG=cfg), \
                 mock.patch.object(upgrade,'download',side_effect=download), \
                 mock.patch.object(upgrade.time,'time',return_value=stamp+60), \
                 mock.patch.object(upgrade.subprocess,'Popen') as start, \
                 mock.patch.object(upgrade,'Path',wraps=Path) as path_type:
                # Redirect the production log open to the temporary router.
                path_type.side_effect=lambda p: root/'worker.log' if p=='/opt/var/log/iptv-home-probe.log' else Path(p)
                if bad_hash:
                    with self.assertRaisesRegex(RuntimeError,'hash'):
                        upgrade.apply('a'*40, schedule_only=schedule_only)
                    self.assertEqual(config,json.loads(cfg.read_text()))
                    start.assert_not_called()
                else:
                    upgrade.apply('a'*40, schedule_only=schedule_only)
                    final=json.loads(cfg.read_text())
                    self.assertTrue(final['daily_worker_enabled'])
                    self.assertEqual(not schedule_only,final['actionable'])
                    self.assertEqual('final',json.loads((jobs/'primary.json').read_text())['phase'])
                    self.assertFalse((root/'background-upgrade.locked').exists())
                    start.assert_called_once()

    def test_detached_upgrade_preserves_progress_and_enables_publishing(self):
        self.exercise()

    def test_unacknowledged_file_does_not_pause_existing_worker(self):
        self.exercise(bad_hash=True)

    def test_schedule_update_preserves_activation_without_requiring_old_report(self):
        self.exercise(schedule_only=True)
