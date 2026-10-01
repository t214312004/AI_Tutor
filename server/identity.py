VOICE_ALIASES = {
    'zh-TW-YunJhenNeural': 'zh-TW-YunJheNeural',
}


def resolve_voice(voice: str) -> str:
    v = (voice or '').strip()
    return VOICE_ALIASES.get(v, v)


STUDENT_PROFILES = {}

DEFAULT_TEACHER_NAME = '伴讀老師'
DEFAULT_EDGE_VOICE = 'zh-TW-HsiaoYuNeural'

# Backward-compatible aliases used by diagnostics and tests
TEACHER_NAME = DEFAULT_TEACHER_NAME
EDGE_VOICE = DEFAULT_EDGE_VOICE

VOICE_OPTIONS = [
    {'id': 'zh-TW-YunJheNeural', 'label': '男生聲音（YunJhe）'},
    {'id': 'zh-TW-HsiaoYuNeural', 'label': '女生聲音（HsiaoYu）'},
    {'id': 'zh-TW-HsiaoChenNeural', 'label': '女生聲音（HsiaoChen）'},
]

ALLOWED_VOICES = {v['id'] for v in VOICE_OPTIONS} | set(VOICE_ALIASES.keys())


def student_profile(student_id, root=None):
    """Use optional private preferences; no family identities in public defaults."""
    import json
    import os
    from pathlib import Path
    result = {'teacher_name': DEFAULT_TEACHER_NAME, 'edge_voice': DEFAULT_EDGE_VOICE}
    if root is not None:
        file = Path(os.environ['TUTOR_BOOTSTRAP_FILE']) if os.environ.get('TUTOR_BOOTSTRAP_FILE') else Path(root) / 'bootstrap.json'
        if file.exists():
            profiles = json.loads(file.read_text(encoding='utf-8')).get('teacher_profiles', {})
            preference = profiles.get(student_id, {})
            if preference.get('teacher_name'):
                result['teacher_name'] = preference['teacher_name']
            if preference.get('edge_voice') in ALLOWED_VOICES:
                result['edge_voice'] = resolve_voice(preference['edge_voice'])
    return result
