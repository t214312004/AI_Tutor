import unittest
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
