import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import audit_public
from scripts.audit_public import scan


class ReleaseAuditTests(unittest.TestCase):
    def test_secret_values_are_never_returned_in_findings(self):
        synthetic = 'ghp_' + 'A' * 36
        findings = scan('example.py', synthetic.encode(), [])
        self.assertEqual(findings[0]['category'], 'github_secret')
        self.assertNotIn(synthetic, str(findings))

    def test_private_terms_and_binary_are_rejected(self):
        self.assertEqual(scan('example.md', '虛構私有詞'.encode(), ['虛構私有詞'])[0]['category'], 'private_term')
        self.assertEqual(scan('example.bin', b'\xff\x00', [])[0]['category'], 'non_utf8_binary')

    def test_parameter_names_and_synthetic_examples_are_allowed(self):
        self.assertEqual(scan('example.py', b"key = os.environ['OPENAI_API_KEY']", []), [])

    def test_staged_snapshot_and_every_historical_path_are_audited(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / audit_public.MANIFEST_NAME
            manifest.parent.mkdir()
            manifest.write_text(json.dumps({'files': [audit_public.MANIFEST_NAME, 'public.txt']}), encoding='utf-8')
            (root / 'public.txt').write_text('same file contents', encoding='utf-8')
            (root / 'unlisted.txt').write_bytes((root / 'public.txt').read_bytes())
            with patch.multiple(audit_public, ROOT=root, MANIFEST=manifest):
                audit_public.git('init', '-q')
                audit_public.git('add', '.')
                audit_public.git('-c', 'user.name=Audit Test', '-c', 'user.email=audit@example.test',
                                 '-c', 'core.hooksPath=.git', 'commit', '-qm', 'Synthetic history')
                audit_public.git('rm', '-q', 'unlisted.txt')
                manifest.write_text('{}', encoding='utf-8')
                (root / 'public.txt').unlink()
                files, findings = audit_public.audit(staged=True, history=True)
            self.assertEqual(files['public.txt'], b'same file contents')
            self.assertEqual(findings, [{'path': 'unlisted.txt', 'category': 'history_outside_allowlist'}])
