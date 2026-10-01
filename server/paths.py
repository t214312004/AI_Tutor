"""Resolve private storage without importing account credentials or creating data."""
import json
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def local_settings():
    # Explicit test/core data roots must never read this machine's private config.
    if os.environ.get('TUTOR_HOME') or os.environ.get('TUTOR_DATA_DIR') or os.environ.get('TUTOR_TEST_MODE') == '1':
        return {}
    file = PROJECT / 'app-data' / 'local-settings.json'
    if not file.exists():
        return {}
    value = json.loads(file.read_text(encoding='utf-8'))
    home = Path(value.get('home', ''))
    if not home.is_absolute() or not home.is_dir():
        raise ValueError('本機資料位置無法存取，請檢查私有啟動設定。')
    return value


def absolute_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('資料位置必須是絕對路徑。')
    return path


def tutor_home():
    if os.environ.get('TUTOR_HOME'):
        return absolute_path(os.environ['TUTOR_HOME'])
    if os.environ.get('TUTOR_DATA_DIR'):
        return absolute_path(os.environ['TUTOR_DATA_DIR']).parent
    configured = local_settings().get('home')
    if configured:
        return absolute_path(configured)
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local' / 'share')))
    return base / 'AI-Tutor'


def data_dir():
    override = os.environ.get('TUTOR_DATA_DIR')
    return absolute_path(override) if override else tutor_home() / 'data'


def curriculum_dir():
    override = os.environ.get('TUTOR_CURRICULUM_DIR')
    if override:
        return absolute_path(override)
    configured = local_settings().get('curriculum_dir')
    return absolute_path(configured) if configured else tutor_home() / 'curriculum'
