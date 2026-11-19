"""Optional teacher names from private bootstrap preferences."""
import json
import os
from pathlib import Path

DEFAULT_TEACHER_NAME = '伴讀老師'


def student_profile(student_id, root=None, seed=None):
    """Use optional private preferences; no family identities in public defaults."""
    result = {'teacher_name': DEFAULT_TEACHER_NAME}
    if seed is None and root is not None:
        file = Path(os.environ['TUTOR_BOOTSTRAP_FILE']) if os.environ.get('TUTOR_BOOTSTRAP_FILE') else Path(root) / 'bootstrap.json'
        if file.exists():
            seed = json.loads(file.read_text(encoding='utf-8'))
    if isinstance(seed, dict):
        profiles = seed.get('teacher_profiles')
        preference = profiles.get(student_id) if isinstance(profiles, dict) else None
        if isinstance(preference, dict):
            name = str(preference.get('teacher_name') or '').strip()
            if name:
                result['teacher_name'] = name
    return result
