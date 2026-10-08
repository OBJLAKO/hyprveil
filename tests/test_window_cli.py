import unittest
from unittest.mock import Mock

import test_service as fixtures

service = fixtures.service


class WindowCLITests(unittest.TestCase):
    def setUp(self):
        self.controller = object.__new__(service.Controller)
        self.controller.raw = Mock(return_value='ok')
        self.snapshot = dict(state='hidden', address='0x1234', stable_id='18446744073709551615',
                             native_private=True, inherited=False)
        self.controller.query = Mock(return_value=self.snapshot)

    def test_public_actions_use_one_atomic_native_callback_and_current_snapshot(self):
        for action in ('toggle', 'hide', 'show', 'reset-sharing'):
            with self.subTest(action=action):
                self.assertEqual(self.controller.window_action(action), self.snapshot)
                args = self.controller.raw.call_args.args
                self.assertEqual(args[0], 'eval')
                self.assertIn('hl.plugin.hyprveil', args[1])
                self.assertNotIn('require(', args[1])
                if action in ('hide', 'show'):
                    self.assertIn('p.set_hidden(s.address,s.stable_id,', args[1])
                self.controller.query.assert_called_with('hyprveil', 'active-privacy')

    def test_refused_mutation_is_not_reported_as_success(self):
        self.controller.raw.return_value = 'error: focus changed'
        with self.assertRaises(service.Refused):
            self.controller.window_action('show')
        self.controller.query.assert_not_called()
        self.controller.raw.reset_mock()
        with self.assertRaises(service.Refused):
            self.controller.window_action('show; arbitrary_code()')
        self.controller.raw.assert_not_called()

    def test_unknown_or_inconsistent_privacy_acknowledgements_are_rejected(self):
        invalid = [dict(self.snapshot, title='private'), dict(self.snapshot, stable_id=123),
                   dict(self.snapshot, address='../../socket'), dict(self.snapshot, native_private=1),
                   dict(self.snapshot, state='visible'), dict(self.snapshot, state='unknown'),
                   dict(self.snapshot, stable_id='1' * 21), dict(self.snapshot, stable_id='18446744073709551616'),
                   dict(self.snapshot, stable_id='0'), dict(self.snapshot, stable_id='01'),
                   dict(self.snapshot, inherited=True), dict(self.snapshot, state='none')]
        for value in invalid:
            with self.subTest(value=value):
                self.controller.query.return_value = value
                with self.assertRaises(service.Refused):
                    self.controller.window_action('toggle')
        self.controller.query.return_value = dict(state='none', address='', stable_id='', native_private=False, inherited=False)
        self.assertEqual(self.controller.window_action('reset-sharing')['state'], 'none')
