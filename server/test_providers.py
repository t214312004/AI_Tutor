import unittest
from unittest.mock import patch

from .providers import ProviderError, codex_command


class CodexCommandTests(unittest.TestCase):
    def test_explicit_executable_is_passed_as_a_single_argument(self):
        executable = 'C:/Program Files/Codex/codex.exe'
        with patch.dict('os.environ', {'TUTOR_CODEX_EXECUTABLE': executable}):
            self.assertEqual(codex_command(), [executable])

    def test_does_not_run_shell_wrappers(self):
        with patch.dict('os.environ', {'TUTOR_CODEX_EXECUTABLE': ''}), \
             patch('server.providers.shutil.which', side_effect=lambda name: 'codex.cmd' if name == 'codex' else None):
            with self.assertRaises(ProviderError):
                codex_command()
