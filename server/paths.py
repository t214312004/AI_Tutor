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
    if not isinstance(value, dict):
        raise ValueError('本機資料位置無法存取，請檢查私有啟動設定。')
    configured_home = value.get('home')
    home = Path(configured_home) if isinstance(configured_home, str) and configured_home else Path('')
    if not home.is_absolute() or not home.is_dir():
        raise ValueError('本機資料位置無法存取，請檢查私有啟動設定。')
    return value


def absolute_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('資料位置必須是絕對路徑。')
    return path


def _tutor_home_from(settings=None):
    if os.environ.get('TUTOR_HOME'):
        return absolute_path(os.environ['TUTOR_HOME'])
    if os.environ.get('TUTOR_DATA_DIR'):
        return absolute_path(os.environ['TUTOR_DATA_DIR']).parent
    if settings is None:
        settings = local_settings()
    configured = settings.get('home')
    if configured:
        return absolute_path(configured)
    base = Path(os.environ.get('LOCALAPPDATA') or str(Path.home() / '.local' / 'share'))
    return base / 'AI-Tutor'


def tutor_home():
    return _tutor_home_from()


def data_dir():
    override = os.environ.get('TUTOR_DATA_DIR')
    return absolute_path(override) if override else tutor_home() / 'data'


def curriculum_dir():
    override = os.environ.get('TUTOR_CURRICULUM_DIR')
    if override:
        return absolute_path(override)
    if os.environ.get('TUTOR_HOME') or os.environ.get('TUTOR_DATA_DIR'):
        return _tutor_home_from({}) / 'curriculum'
    settings = local_settings()
    configured = settings.get('curriculum_dir')
    return absolute_path(configured) if configured else _tutor_home_from(settings) / 'curriculum'
