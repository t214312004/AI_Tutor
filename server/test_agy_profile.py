import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .agy_profile import AgyPrivateProfile, agy_onboarding
from .paths import PROJECT
from .providers import ProviderError


@unittest.skipUnless(os.name == 'nt', 'agy profiles are currently Windows-only')
class AgyProfileTests(unittest.TestCase):
    def test_child_cannot_inherit_api_login_or_other_cli_state(self):
        inherited = dict.fromkeys((
            'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS',
            'AGY_ADC_AUTH', 'OPENAI_API_KEY', 'ANTIGRAVITY_MIC', 'CODEX_HOME',
        ), 'synthetic')
        with patch.dict(os.environ, inherited):
            profile = AgyPrivateProfile()
        self.addCleanup(profile.close)

        self.assertFalse(profile.root.is_relative_to(PROJECT))
        self.assertFalse(inherited.keys() & profile.env.keys())
        self.assertEqual(profile.app_data.parent, profile.gemini)
        self.assertFalse(profile.gemini.exists())
        self.assertEqual(list(profile.workspace.iterdir()), [])
        for key in ('USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP'):
            self.assertTrue(Path(profile.env[key]).is_relative_to(profile.root))
        profile.close()
        self.assertFalse(profile.root.exists())

    def test_temp_inside_repository_is_rejected(self):
        with patch('server.agy_profile.agy_setup_root', return_value=PROJECT / 'synthetic-setup'):
            with self.assertRaises(ProviderError):
                AgyPrivateProfile()

    def test_only_setup_completion_flags_are_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'profile' / '.gemini' / 'antigravity-cli' / 'cache' / 'onboarding.json'
            state.parent.mkdir(parents=True)
            with patch('server.agy_profile.agy_setup_root', return_value=root):
                with self.assertRaises(ProviderError):
                    agy_onboarding()
                state.write_text(json.dumps({'consumerOnboardingComplete': True, 'other': 'synthetic'}))
                self.assertEqual(agy_onboarding(), {'consumerOnboardingComplete': True})
