import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch

from scripts.controller import fresh
from scripts.github_api import GitHub
from scripts.tentar_vps import main


class EntrypointTests(unittest.TestCase):
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
