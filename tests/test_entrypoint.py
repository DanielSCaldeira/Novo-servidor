import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch

from scripts.controller import fresh
from scripts.github_api import GitHub
from scripts.tentar_vps import job_failure_summary, main, public_error, provider_summary, disk_summary


class EntrypointTests(unittest.TestCase):
    def test_temporary_oci_errors_preserve_pending_token_and_enabled_workflow(self):
        import oci
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                gh = Mock(spec=GitHub)
                state = dict(fresh(), phase='pending', token='durable-token', operation='PLAN')
                gh.load.return_value = state
                output = io.StringIO()
                with self.env(RETRY_ENABLED='true', ZERO_COST_CONFIRMED='true', APPROVED_STACK_SHA256='a'*64), \
                        patch('scripts.tentar_vps.GitHub', return_value=gh), \
                        patch('scripts.oracle_api.Oracle'), \
                        patch('scripts.tentar_vps.reconcile', side_effect=oci.exceptions.ServiceError(
                            status, 'TooManyRequests', {}, 'SECRET-KEY-CONTENT')), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(main(), 1)
                self.assertEqual(state['phase'], 'pending')
                self.assertEqual(state['token'], 'durable-token')
                gh.disable.assert_not_called()
                gh.notify.assert_not_called()
                self.assertNotIn('SECRET-KEY-CONTENT', output.getvalue())

    def test_permanent_oci_auth_error_still_blocks(self):
        import oci
        gh = Mock(spec=GitHub)
        gh.load.return_value = fresh()
        with self.env(RETRY_ENABLED='true', ZERO_COST_CONFIRMED='true', APPROVED_STACK_SHA256='a'*64), \
                patch('scripts.tentar_vps.GitHub', return_value=gh), \
                patch('scripts.oracle_api.Oracle', side_effect=oci.exceptions.ServiceError(
                    401, 'NotAuthenticated', {}, 'secret')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 1)
        gh.disable.assert_called_once()
        self.assertEqual(gh.load.return_value['phase'], 'blocked')

    def test_disk_configuration_never_runs_controller_or_sends_messages(self):
        oracle = Mock()
        oracle.configure_boot_disk.return_value = 'b' * 64
        with self.env(CONFIGURE_BOOT_DISK='true', ZERO_COST_CONFIRMED='true', APPROVED_STACK_SHA256='a'*64), \
                patch('scripts.oracle_api.Oracle', return_value=oracle), \
                patch('scripts.tentar_vps.GitHub') as gh, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        oracle.configure_boot_disk.assert_called_once_with('a'*64)
        gh.assert_not_called()

    def test_disk_configuration_requires_confirmed_scope(self):
        with self.env(CONFIGURE_BOOT_DISK='true'), patch('scripts.oracle_api.Oracle') as oracle, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 1)
        oracle.assert_not_called()

    def test_disk_summary_only_exposes_numeric_disk_fields(self):
        import json
        summary = disk_summary({'resource_changes':[{'type':'oci_core_instance','change':{'after':{
            'source_details':[{'boot_volume_size_in_gbs':'50', 'boot_volume_vpus_per_gb':'SECRET-KEY-CONTENT'}]}}}]})
        self.assertNotIn('SECRET-KEY-CONTENT', json.dumps(summary))
        self.assertEqual(summary[0]['boot_volume_size_in_gbs'], {'type':'str','value':'50'})

    def test_provider_summary_does_not_expose_plan_values(self):
        import json
        summary = provider_summary({'configuration': {'provider_config': {'oci': {
            'name': 'oci', 'full_name': 'SECRET-KEY-CONTENT',
            'expressions': {'private_key': 'SECRET-KEY-CONTENT'}}}},
            'resource_changes': [{'provider_name': 'registry.terraform.io/oracle/oci',
                                  'change': {'after': {'private_key': 'SECRET-KEY-CONTENT'}}}]})
        self.assertNotIn('SECRET-KEY-CONTENT', json.dumps(summary))
        self.assertEqual(summary['configuration'][0]['name'], 'oci')
        self.assertEqual(summary['resource_providers'], ['registry.terraform.io/oracle/oci'])

    def test_job_failure_summary_never_reproduces_sensitive_logs(self):
        result = job_failure_summary(['Error: 500-InternalError, Out of host capacity', 'SECRET-KEY-CONTENT'])
        self.assertIn('Out of host capacity', result)
        self.assertNotIn('SECRET-KEY-CONTENT', result)
        self.assertIn('QuotaExceeded', job_failure_summary(['Error: QuotaExceeded SECRET-KEY-CONTENT']))
        self.assertNotIn('SECRET-KEY-CONTENT', job_failure_summary(['Error: SECRET-KEY-CONTENT']))

    def test_service_error_reports_category_without_request_or_message(self):
        import oci
        error = oci.exceptions.ServiceError(
            401, 'NotAuthenticated', {'secret-header': 'SECRET-KEY-CONTENT'}, 'SECRET-KEY-CONTENT')
        output = public_error(error)
        self.assertIn('HTTP 401', output)
        self.assertIn('NotAuthenticated', output)
        self.assertNotIn('SECRET-KEY-CONTENT', output)

    def env(self, **extra):
        return patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'DIAGNOSTIC': 'false',
            'TEST_NOTIFICATION': 'false', 'RETRY_ENABLED': 'false', 'RESUME_BLOCKED': 'false',
            'ZERO_COST_CONFIRMED': 'false', **extra}, clear=True)

    def test_disabled_retries_never_construct_oracle_client(self):
        gh = Mock(spec=GitHub)
        gh.load.return_value = fresh()
        with self.env(), patch('scripts.tentar_vps.GitHub', return_value=gh), \
                patch('scripts.oracle_api.Oracle') as oracle, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 1)
        oracle.assert_not_called()
        gh.disable.assert_called_once()

    def test_notification_test_never_calls_oracle(self):
        gh = Mock(spec=GitHub)
        gh.load.return_value = None
        with self.env(TEST_NOTIFICATION='true', GITHUB_RUN_ID='123'), \
                patch('scripts.tentar_vps.GitHub', return_value=gh), \
                patch('scripts.oracle_api.Oracle') as oracle, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        oracle.assert_not_called()
        self.assertEqual(gh.notify.call_args.args[0], 'notification-test-123')

    def test_api_exception_never_prints_secret(self):
        gh = Mock(spec=GitHub)
        gh.load.return_value = fresh()
        output = io.StringIO()
        with self.env(RETRY_ENABLED='true', ZERO_COST_CONFIRMED='true', APPROVED_STACK_SHA256='a' * 64), \
                patch('scripts.tentar_vps.GitHub', return_value=gh), \
                patch('scripts.oracle_api.Oracle', side_effect=RuntimeError('SECRET-KEY-CONTENT')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(), 1)
        self.assertNotIn('SECRET-KEY-CONTENT', output.getvalue())
        self.assertEqual(gh.save.call_args.args[0]['phase'], 'blocked')

    def test_private_repository_gate_prevents_oracle_calls(self):
        from scripts.safety import Blocked
        gh = Mock(spec=GitHub)
        gh.assert_public.side_effect = Blocked('Custo zero exige repositório público.')
        with self.env(), patch('scripts.tentar_vps.GitHub', return_value=gh), \
                patch('scripts.oracle_api.Oracle') as oracle, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 1)
        oracle.assert_not_called()
        gh.save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
