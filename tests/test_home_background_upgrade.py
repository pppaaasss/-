"""Pinned updater and hard-interruption recovery on a temporary router."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from router.ac86u import background_upgrade as upgrade

REPO=Path(__file__).resolve().parents[1]


class UpgradeTests(unittest.TestCase):
    def exercise(self, interrupt=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'state';root.mkdir()
            base=Path(tmp)/'installed';base.mkdir()
            for name in upgrade.FILES:
                (base/name).write_bytes((REPO/'router/ac86u'/name).read_bytes())
            cfg=Path(tmp)/'config.json'
            config=dict(output_dir=str(root),probe_id='test',daily_worker_enabled=True,actionable=True,
                        minimum_height_overrides={'CCTV-4K':1080}, minimum_headroom_ratio=1.35)
            cfg.write_text(json.dumps(config))
            state=b'{"candidate_queue":[{"candidate_id":"saved"}]}'
            (root/'state.json').write_bytes(state)
            old=(base/'home_probe.py').read_bytes()
            def download(url,target): target.write_bytes((REPO/'router/ac86u'/url.rsplit('/',1)[1]).read_bytes())
            atomic=upgrade.atomic_json
            def save(path,value):
                if interrupt and path.name=='installed-version.json': raise KeyboardInterrupt('power lost')
                return atomic(path,value)
            with mock.patch.multiple(upgrade,ROOT=root,BASE=base,CONFIG=cfg), \
                 mock.patch.object(upgrade,'download',side_effect=download), \
                 mock.patch.object(upgrade,'atomic_json',side_effect=save):
                if interrupt:
                    with self.assertRaises(KeyboardInterrupt): upgrade.apply('a'*40)
                    self.assertTrue((root/'background-upgrade.locked').exists())
                    self.assertFalse(json.loads(cfg.read_text())['daily_worker_enabled'])
                    upgrade.recover()
                    upgrade.recover()  # Idempotent; no media process launched.
                    self.assertEqual(old,(base/'home_probe.py').read_bytes())
                else:
                    upgrade.apply('a'*40)
                    self.assertEqual('a'*40,json.loads((root/'installed-version.json').read_text())['revision'])
                    self.assertFalse((root/'background-upgrade.locked').exists())
                expected = config if interrupt else dict(config, minimum_headroom_ratio=1.05)
                self.assertEqual(expected,json.loads(cfg.read_text()))
                if not interrupt:
                    backup=json.loads((root/'installed-version.json').read_text())['rollback']
                    upgrade.recover(backup)
                    self.assertEqual(config,json.loads(cfg.read_text()))
                self.assertEqual(state,(root/'state.json').read_bytes())

    def test_upgrade_preserves_activation_without_old_ack(self): self.exercise()
    def test_power_loss_disabled_state_and_marker_can_be_recovered(self): self.exercise(True)

    def test_invalid_revision_is_rejected_before_any_download(self):
        with mock.patch.object(upgrade,'download') as call:
            with self.assertRaises(ValueError): upgrade.apply('master')
            call.assert_not_called()
