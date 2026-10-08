"""A generated proof must not claim a failed or partially completed run passed."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("proof", Path(__file__).parents[1] / "tools/make_proof.py")
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class ProofEvidenceTests(unittest.TestCase):
    def caption(self, report):
        with tempfile.TemporaryDirectory(prefix="hyprveil-proof-") as directory:
            root = Path(directory)
            (root / "report.json").write_text(json.dumps(report))
            return proof.evidence_caption(root)

    def test_count_and_version_come_from_completed_report(self):
        caption = self.caption({"ok": True, "checks": [{"ok": True}] * 20,
                                "compositor": {"version": "0.56.2"}, "capture_tool": "grim"})
        self.assertIn("20 проверок", caption)
        self.assertNotIn("ext-image", caption)

    def test_failed_report_cannot_generate_pass_claim(self):
        with self.assertRaises(ValueError):
            self.caption({"ok": False, "checks": [{"ok": True}]})

    def test_partial_or_inconsistent_report_cannot_generate_pass_claim(self):
        for checks in ([], [{"ok": True}, {"ok": False}], [{"name": "never completed"}]):
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                self.caption({"ok": True, "checks": checks})
