"""Exercise whole-queue admission, window recovery and safe publication."""
import copy
from router.ac86u.home_contract import ContractError, candidate_id, validate_home_report_v2
from router.ac86u.home_decision import mass_failure_circuit
from router.ac86u.thin_contract import encode, epoch, seal_task, validate_task
from scripts import home_thin_control as cloud
from scripts.publish_home_decisions import publish_latest
from tests.test_home_thin import ThinFixture, measured


class QueueDrainTests(ThinFixture):
    def setUp(self):
        super().setUp()
        self.config['candidate_mode'] = 'drain_queue'
        self.config['candidate_run_kinds'] = list(cloud.KINDS)
        self.migrated()

    def add_queue(self, count):
        # Use the same channel and distinct exact URLs, as the real queue does.
        original = self.manifest['candidates'][0]
        self.state['queue'] = {}
        for n in range(count):
            url = original['url'] if n == 0 else f'http://candidate.test/{n}.m3u8'
            row = dict(original, url=url, candidate_id=candidate_id('cctv1', url))
            self.state['queue'][row['candidate_id']] = row

    def test_all_246_candidates_are_measured_once_in_four_route_batches(self):
        self.add_queue(246)
        current, _ = self.step()
        self.deliver(current)
        seen, batch_sizes = [], []
        for _ in range(70):
            task, report = self.step()
            if report:
                break
            self.assertIsNone(report)
            self.assertEqual({'candidate'}, {r['role'] for r in task['tasks']})
            validate_task(task, self.probe, self.now)
            seen.extend(r['candidate_id'] for r in task['tasks'])
            batch_sizes.append(len(task['tasks']))
            self.deliver(task)
        self.assertEqual(246, len(seen))
        self.assertEqual(246, len(set(seen)))
        self.assertEqual([4] * 61 + [2], batch_sizes)
        self.assertFalse(self.state['queue'])
        self.assertEqual(246, len(self.state['tested']))
        self.assertEqual(246, len(report['candidate_results']))
        self.assertEqual('SLOT_COMPLETE', task['state'])
        self.assertEqual(0, report['summary']['replacements'])

    def test_afternoon_and_evening_allow_fresh_candidates_and_safe_publication(self):
        for date in ('2026-09-15T05:05:00Z', '2026-09-15T12:05:00Z'):
            with self.subTest(date=date):
                # Reinitialize the fixture independently for both windows.
                self.state = cloud.new_state(self.probe)
                self.migrated()
                (self.root/'harvest/home-candidates.json').write_bytes(encode(self.manifest))
                self.now = epoch(date)
                task, _ = self.step()
                self.deliver(task, {'cctv1': 'UNAVAILABLE'})
                task, _ = self.step()
                self.deliver(task, {'cctv1': 'UNAVAILABLE'})
                task, _ = self.step()
                self.assertEqual(['candidate'], [r['role'] for r in task['tasks']])
                validate_task(task, self.probe, self.now)
                legacy = seal_task(dict(task, candidate_mode='daily'))
                with self.assertRaises(ValueError):
                    validate_task(legacy, self.probe, self.now)
                self.deliver(task)
                _, report = self.step()
                self.assertEqual(1, report['summary']['replacements'])
                self.assertTrue(report['candidate_results'][0]['switch_reverified'])
                validate_home_report_v2(report, now_epoch=self.now, max_age_hours=18)
                forged = copy.deepcopy(report)
                forged['candidate_results'][0].update(switch_reverified=False, purpose='daily-qualification')
                with self.assertRaises(ContractError):
                    validate_home_report_v2(forged, now_epoch=self.now, max_age_hours=18)
                path = self.reports/'inbox'/self.probe/cloud.report_filename(report, encode(report))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(encode(report))
                result = publish_latest(root=self.root, config_path=self.root/'config/home-publisher.json',
                    inbox=self.reports/'inbox', now_epoch=self.now, apply=True)
                self.assertEqual(1, result['replacement_count'])
                for name in ('tv-core.m3u', 'tv.m3u', 'tv-all.m3u', 'tv-easy.m3u'):
                    self.assertIn(b'http://candidate.test/one.m3u8', (self.root/name).read_bytes())
                    (self.root/name).write_bytes(self.formal)

    def test_window_end_resumes_remaining_queue_without_retesting_accepted_routes(self):
        self.add_queue(14)
        self.now = epoch('2026-09-15T05:05:00Z')
        task, _ = self.step()
        self.deliver(task)
        task, _ = self.step()
        self.assertEqual(4, len(task['tasks']))
        accepted = {r['candidate_id'] for r in task['tasks'][:2]}
        self.deliver(task, subset=2, stop='interrupted_batch')
        task, _ = self.step()
        self.now = epoch('2026-09-15T08:00:01Z')
        idle, _ = self.step()
        self.assertEqual('WAITING_WINDOW', idle['state'])
        self.assertEqual(12, len(self.state['queue']))
        self.now = epoch('2026-09-15T12:05:00Z')
        task, _ = self.step()
        self.assertEqual({'current'}, {r['role'] for r in task['tasks']})
        self.deliver(task)
        seen = []
        for _ in range(10):
            task, report = self.step()
            if report:
                break
            seen.extend(r['candidate_id'] for r in task['tasks'])
            self.deliver(task)
        self.assertEqual(12, len(seen))
        self.assertFalse(accepted.intersection(seen))
        self.assertFalse(self.state['queue'])

    def test_byte_guard_still_stops_admission_without_losing_the_queue(self):
        task, _ = self.step()
        self.deliver(task)
        self.config['daily_bytes'] = 64 * 1024 * 1024 - 1
        task, report = self.step()
        self.assertEqual('SLOT_COMPLETE', task['state'])
        self.assertEqual(1, len(self.state['queue']))
        self.assertFalse(self.state['tested'])
        self.assertFalse(report['candidate_results'])

    def test_september_15_metadata_mix_does_not_mean_network_outage(self):
        task, _ = self.step()
        row = task['tasks'][0]
        missing = measured(row)
        missing.update(deep_checked=False, width=0, height=0, fps=0, codec='')
        missing = cloud.normalise(missing, row)
        self.assertEqual('UNKNOWN', missing['observed_status'])
        good = cloud.normalise(measured(row), row)
        failed = cloud.normalise(measured(row, 'UNAVAILABLE'), row)
        attempts = {str(n): [result, result] for n, result in
                    enumerate([good] * 34 + [missing] * 15 + [failed] * 4)}
        self.assertFalse(mass_failure_circuit(attempts, minimum_channels=12, failure_ratio=.35))
        # A real 19/53 transfer failure still pauses discovery and replacement.
        attempts.update({str(n): [failed, failed] for n in range(34, 53)})
        self.assertTrue(mass_failure_circuit(attempts, minimum_channels=12, failure_ratio=.35))
