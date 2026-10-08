import io
import contextlib
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import setup as setup_cli
import service


class SetupTests(unittest.TestCase):
    def test_check_is_read_only_and_does_not_build_or_install(self):
        pins = {"old_sha256": "a" * 64, "plugin_sha256": "b" * 64}
        with patch.object(setup_cli, "preflight", return_value=pins), patch.object(setup_cli, "build") as build, \
             patch.object(setup_cli.install, "Installer") as installer, patch.object(setup_cli.upgrade, "upgrade") as upgrade, \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(setup_cli.main(["check"]), 0)
        build.assert_not_called()
        installer.assert_not_called()
        upgrade.assert_not_called()

    def test_unsupported_header_abi_refuses_before_connect_or_write(self):
        with patch.object(setup_cli.install, "read_owned"), patch.object(setup_cli, "command", return_value="unsupported"), \
             patch.object(setup_cli.service, "Controller") as controller:
            with self.assertRaisesRegex(service.Refused, "not supported"):
                setup_cli.preflight()
        controller.assert_not_called()

    def test_unfinished_capture_gate_refuses_install_preflight(self):
        controller = Mock()
        controller.locked.return_value = contextlib.nullcontext()
        controller.target.pid = 123
        controller.raw.return_value = ''
        controller.query.return_value = [{'name': 'hyprveil-upgrade-guard'}]
        with patch.object(setup_cli.install, 'read_owned'), \
             patch.object(setup_cli, 'command', return_value=setup_cli.TESTED_ABI), \
             patch.object(setup_cli.service, 'Controller', return_value=controller), \
             patch.object(setup_cli.service, 'digest_file', return_value='a' * 64):
            with self.assertRaisesRegex(service.Refused, 'unfinished protected update'):
                setup_cli.preflight()
        controller.plugins.assert_not_called()

    def test_install_computes_pins_without_requiring_user_hash_arguments(self):
        pins = {"plugin_sha256": "a" * 64, "compositor_sha256": "b" * 64, "abi_hash": setup_cli.TESTED_ABI,
                "signature": "synthetic-instance", "pid": 123, "old_sha256": None, "guard_sha256": "c" * 64}
        with patch.object(setup_cli, "build"), patch.object(setup_cli, "preflight", return_value=pins), \
             patch.object(setup_cli.install, "Installer") as installer, patch('sys.stdout', new_callable=io.StringIO) as output:
            installer.return_value.run.return_value = {"report": "/tmp/synthetic-report"}
            self.assertEqual(setup_cli.main(["install"]), 0)
            self.assertIn("next login", output.getvalue())
        args = installer.call_args.args[0]
        self.assertEqual(args.plugin_sha256, pins["plugin_sha256"])
        self.assertEqual(args.signature, "synthetic-instance")

    def test_known_predecessor_uses_protected_upgrade(self):
        pins = {"plugin_sha256": "a" * 64, "signature": "synthetic-instance", "old_sha256": "b" * 64,
                "guard_sha256": "c" * 64}
        with patch.object(setup_cli, "build"), patch.object(setup_cli, "preflight", return_value=pins), \
             patch.object(setup_cli.upgrade, "upgrade", return_value={}) as upgrade, patch.dict('os.environ', {}, clear=False), \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(setup_cli.main(["install"]), 0)
        args = upgrade.call_args.args[0]
        self.assertEqual(args.old_sha256, pins["old_sha256"])
        self.assertEqual(args.guard_sha256, pins["guard_sha256"])


if __name__ == "__main__":
    unittest.main()
