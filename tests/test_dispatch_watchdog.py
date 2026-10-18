from datetime import datetime, timezone
import json
import unittest
from scripts.dispatch_watchdog import reconcile


class WatchdogTests(unittest.TestCase):
    def run_case(self, state='active', enabled=True, runs=None, dry_run=False):
        calls = []
        def call(*args):
            calls.append(args)
            if args[0] == 'api':
                return json.dumps({'state': state})
            if args[0] == 'variable':
                return json.dumps([{'name': key, 'value': 'true' if enabled else 'false'}
                                   for key in ('RETRY_ENABLED', 'ZERO_COST_CONFIRMED')])
            if args[0] == 'run':
                return json.dumps(runs or [])
            return ''
        result = reconcile(call, datetime(2026, 10, 18, 15, tzinfo=timezone.utc), dry_run)
        dispatches = [c for c in calls if c[0] == 'workflow']
        return result, dispatches

    def test_missed_cron_dispatches_real_retry_without_resume(self):
        result, calls = self.run_case(runs=[{'status': 'completed', 'createdAt': '2026-10-18T14:20:00Z'}])
        self.assertEqual(len(calls), 1)
        self.assertIn('resume_blocked=false', calls[0])
        self.assertIn('diagnostic=false', calls[0])

    def test_paused_or_complete_workflow_is_never_reenabled(self):
        self.assertEqual(self.run_case(state='disabled_manually')[1], [])
        self.assertEqual(self.run_case(enabled=False)[1], [])

    def test_recent_or_active_runs_prevent_duplicate_dispatch(self):
        for status, stamp in [('queued', '2026-10-18T14:00:00Z'),
                              ('in_progress', '2026-10-18T14:00:00Z'),
                              ('completed', '2026-10-18T14:55:00Z')]:
            self.assertEqual(self.run_case(runs=[{'status': status, 'createdAt': stamp}])[1], [])

    def test_dry_run_does_not_dispatch(self):
        self.assertEqual(self.run_case(dry_run=True)[1], [])
