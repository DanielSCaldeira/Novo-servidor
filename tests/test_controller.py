import copy
import unittest
from types import SimpleNamespace as Obj
from unittest.mock import Mock

from scripts.controller import fresh, now, reconcile
from scripts.safety import Blocked
from tests.test_safety import plan

APPROVED = 'a' * 64


def job(operation='PLAN', status='SUCCEEDED', identifier='job-1', name='manual'):
    return Obj(id=identifier, operation=operation, lifecycle_state=status, display_name=name)


def setup():
    oracle = Mock(stack_id='stack', compartment_id='compartment')
    oracle.digest.return_value = APPROVED
    oracle.jobs.return_value = []
    oracle.instances.return_value = []
    oracle.inventory.return_value = {'storage_gb': 150, 'a1_ocpus': 0, 'a1_memory_gb': 0, 'targets': []}
    oracle.plan.return_value = plan()
    oracle.create.return_value = job(status='ACCEPTED')
    gh = Mock()
    return oracle, gh


class ControllerTests(unittest.TestCase):
    def test_one_plan_and_durable_token_before_create(self):
        oracle, gh = setup()
        events = []
        gh.save.side_effect = lambda s: events.append(('save', copy.deepcopy(s)))
        oracle.create.side_effect = lambda *args: events.append(('create', args)) or job(status='ACCEPTED')
        state = fresh()
        reconcile(oracle, gh, state, APPROVED)
        self.assertEqual(state['phase'], 'tracking')
        self.assertEqual(events[0][1]['phase'], 'pending')
        self.assertEqual(events[1][0], 'create')
        self.assertEqual(events[0][1]['token'], events[1][1][2])
        oracle.create.assert_called_once()
        self.assertEqual(oracle.create.call_args.args[0], 'PLAN')

    def test_active_job_prevents_new_job(self):
        for status in ('ACCEPTED', 'IN_PROGRESS', 'CANCELING'):
            oracle, gh = setup()
            oracle.jobs.return_value = [job(status=status)]
            reconcile(oracle, gh, fresh(), APPROVED)
            oracle.create.assert_not_called()

    def test_valid_plan_submits_only_apply_from_plan(self):
        oracle, gh = setup()
        oracle.jobs.return_value = [job()]
        oracle.create.return_value = job('APPLY', 'ACCEPTED', 'apply-1')
        state = dict(fresh(), phase='tracking', job_id='job-1', operation='PLAN')
        reconcile(oracle, gh, state, APPROVED)
        self.assertEqual(oracle.create.call_args.args[0], 'APPLY')
        self.assertEqual(oracle.create.call_args.args[3], 'job-1')
        self.assertEqual(state['job_id'], 'apply-1')

    def test_failed_capacity_replans_same_stack(self):
        oracle, gh = setup()
        oracle.jobs.return_value = [job('APPLY', 'FAILED')]
        oracle.logs.return_value = ['Error: Out of host capacity']
        state = dict(fresh(), phase='tracking', job_id='job-1', operation='APPLY')
        reconcile(oracle, gh, state, APPROVED)
        self.assertEqual(oracle.create.call_args.args[0], 'PLAN')

    def test_unknown_quota_or_auth_error_blocks(self):
        for message in ('Error: unknown', 'Error: QuotaExceeded', 'Error: NotAuthenticated'):
            oracle, gh = setup()
            oracle.jobs.return_value = [job('APPLY', 'FAILED')]
            oracle.logs.return_value = [message]
            with self.assertRaises(Blocked):
                reconcile(oracle, gh, dict(fresh(), phase='tracking', job_id='job-1', operation='APPLY'), APPROVED)
            oracle.create.assert_not_called()

    def test_existing_running_instance_completes_and_disables(self):
        oracle, gh = setup()
        instance = Obj(id='instance', lifecycle_state='RUNNING', shape='VM.Standard.A1.Flex',
                       display_name='vps-ampere-a1', shape_config=Obj(ocpus=2, memory_in_gbs=12))
        oracle.instances.return_value = [instance]
        oracle.inventory.return_value['targets'] = [instance]
        oracle.jobs.return_value = [job('APPLY')]
        state = fresh()
        reconcile(oracle, gh, state, APPROVED)
        self.assertEqual(state['phase'], 'complete')
        gh.notify.assert_called_once()
        gh.disable.assert_called_once()
        oracle.create.assert_not_called()

    def test_partial_failure_with_instance_never_recreates(self):
        oracle, gh = setup()
        oracle.instances.return_value = [Obj(id='instance', lifecycle_state='RUNNING')]
        oracle.jobs.return_value = [job('APPLY', 'FAILED')]
        with self.assertRaises(Blocked):
            reconcile(oracle, gh, fresh(), APPROVED)
        oracle.create.assert_not_called()

    def test_orphan_with_same_name_blocks(self):
        oracle, gh = setup()
        oracle.inventory.return_value['targets'] = [Obj(id='unknown')]
        with self.assertRaises(Blocked):
            reconcile(oracle, gh, fresh(), APPROVED)
        oracle.create.assert_not_called()

    def test_pending_lost_response_adopts_job(self):
        oracle, gh = setup()
        oracle.jobs.return_value = [job('PLAN', 'ACCEPTED', name='free-vps-token')]
        state = dict(fresh(), phase='pending', token='token', operation='PLAN', pending_at=now())
        reconcile(oracle, gh, state, APPROVED)
        self.assertEqual(state['phase'], 'tracking')
        self.assertEqual(state['job_id'], 'job-1')
        oracle.create.assert_not_called()

    def test_pending_reuses_exact_token(self):
        oracle, gh = setup()
        state = dict(fresh(), phase='pending', token='token', operation='PLAN', pending_at=now())
        reconcile(oracle, gh, state, APPROVED)
        self.assertEqual(oracle.create.call_args.args[2], 'token')

    def test_expired_pending_blocks_without_creation(self):
        oracle, gh = setup()
        state = dict(fresh(), phase='pending', token='token', operation='PLAN', pending_at='2000-01-01T00:00:00+00:00')
        with self.assertRaises(Blocked):
            reconcile(oracle, gh, state, APPROVED)
        oracle.create.assert_not_called()

    def test_hash_change_blocks(self):
        oracle, gh = setup()
        oracle.digest.return_value = 'b' * 64
        with self.assertRaises(Blocked):
            reconcile(oracle, gh, fresh(), APPROVED)
        oracle.create.assert_not_called()

    def test_hash_change_during_validation_blocks(self):
        oracle, gh = setup()
        oracle.jobs.return_value = [job()]
        oracle.digest.side_effect = [APPROVED, 'b' * 64]
        with self.assertRaises(Blocked):
            reconcile(oracle, gh, dict(fresh(), phase='tracking', job_id='job-1', operation='PLAN'), APPROVED)
        oracle.create.assert_not_called()

    def test_notification_failure_keeps_complete_state(self):
        oracle, gh = setup()
        state = dict(fresh(), phase='complete', completed_at=now())
        gh.notify.side_effect = Blocked('API unavailable')
        with self.assertRaises(Blocked):
            reconcile(oracle, gh, state, APPROVED)
        self.assertEqual(state['phase'], 'complete')
        oracle.create.assert_not_called()
        gh.disable.assert_not_called()

    def test_storage_limit_blocks(self):
        oracle, gh = setup()
        oracle.inventory.return_value['storage_gb'] = 151
        with self.assertRaises(Blocked):
            reconcile(oracle, gh, fresh(), APPROVED)
        oracle.create.assert_not_called()

    def test_explicit_resume_replans_after_configuration_correction(self):
        oracle, gh = setup()
        oracle.jobs.return_value = [job('APPLY', 'FAILED')]
        oracle.logs.return_value = ['Error: invalid configuration']
        state = dict(fresh(), phase='blocked', job_id='job-1', operation='APPLY', reason='failed')
        reconcile(oracle, gh, state, APPROVED, resume=True)
        self.assertEqual(oracle.create.call_args.args[0], 'PLAN')

    def test_resume_still_blocks_partial_creation(self):
        oracle, gh = setup()
        oracle.instances.return_value = [Obj(id='instance', lifecycle_state='RUNNING')]
        oracle.jobs.return_value = [job('APPLY', 'FAILED')]
        state = dict(fresh(), phase='blocked', job_id='job-1', operation='APPLY', reason='failed')
        with self.assertRaises(Blocked):
            reconcile(oracle, gh, state, APPROVED, resume=True)
        oracle.create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
