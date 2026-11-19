"""Private Windows CLI state shared by the agy voice and vision adapters."""

import json
import os
from pathlib import Path
import shutil
import tempfile

from .collaboration_cli import safe_path
from .paths import PROJECT
from .providers import ProviderError


WINDOWS_ENV = {
    'COMSPEC', 'NUMBER_OF_PROCESSORS', 'OS', 'PATHEXT', 'PROCESSOR_ARCHITECTURE',
    'PROGRAMDATA', 'SYSTEMDRIVE', 'SYSTEMROOT', 'USERDOMAIN', 'USERNAME', 'WINDIR',
}


def agy_executable():
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        installed = Path(local_app_data) / 'agy' / 'bin' / 'agy.exe'
        if installed.is_file():
            return installed.resolve()
    found = shutil.which('agy.exe')
    if not found:
        raise ProviderError('找不到 Antigravity CLI。請先安裝並登入 agy。')
    return Path(found).resolve()


def agy_setup_root():
    local_app_data = os.environ.get('LOCALAPPDATA')
    if not local_app_data:
        raise ProviderError('找不到 Windows 本機應用程式資料夾。')
    parent = (Path(local_app_data) / 'AI-Tutor').resolve()
    root = parent / 'agy-setup'
    if (root.resolve() != root or root.is_relative_to(PROJECT)
            or not root.is_relative_to(Path.home().resolve())):
        raise ProviderError('agy 專用設定需要位於 Windows 私有資料夾。')
    return root


def agy_onboarding():
    root = agy_setup_root()
    try:
        path = safe_path(root, 'profile/.gemini/antigravity-cli/cache/onboarding.json')
        if not path.is_file() or path.stat().st_size > 2048:
            raise ValueError('Unexpected onboarding file')
        value = json.loads(path.read_text(encoding='utf-8'))
        flags = {key: value[key] for key in (
            'onboardingComplete', 'consumerOnboardingComplete', 'enterpriseOnboardingComplete',
        ) if key in value and type(value[key]) is bool}
        if not any(flags.values()):
            raise ValueError('Setup incomplete')
        return flags
    except (OSError, ValueError, TypeError) as error:
        raise ProviderError('請先完成 AI Tutor 專用的 agy 首次設定。') from error


class AgyPrivateProfile:
    """Use the Windows login without importing the user's CLI files or API settings."""

    def __init__(self, *, setup=False):
        system_root = os.environ.get('SystemRoot')
        if os.name != 'nt' or not system_root:
            raise ProviderError('此 agy 訂閱接頭需要 Windows。')
        setup_root = agy_setup_root()
        if setup:
            self._directory = None
            self.root = setup_root
            self.root.mkdir(parents=True, exist_ok=True)
        else:
            parent = setup_root.parent / 'agy-temp'
            if (parent.resolve() != parent or parent.is_relative_to(PROJECT)
                    or any((p / '.git').exists() for p in (parent, *parent.parents))):
                raise ProviderError('agy 需要位於專案之外的私有暫存目錄。')
            parent.mkdir(parents=True, exist_ok=True)
            self._directory = tempfile.TemporaryDirectory(prefix='ai-tutor-agy-', dir=parent)
            self.root = Path(self._directory.name)
        profile = self.root / 'profile'
        roaming = self.root / 'roaming'
        local = self.root / 'local'
        temporary = self.root / 'temp'
        self.workspace = self.root / 'workspace'
        self.gemini = profile / '.gemini'
        self.app_data = self.gemini / 'antigravity-cli'
        try:
            for folder in (profile, roaming, local, temporary, self.workspace):
                if folder.resolve() != folder:
                    raise ProviderError('agy 專用設定不能使用連結資料夾。')
                folder.mkdir(exist_ok=setup)
        except BaseException:
            self.close()
            raise

        windows = Path(system_root)
        system32 = windows / 'System32'
        self.env = {key.upper(): value for key, value in os.environ.items() if key.upper() in WINDOWS_ENV}
        self.env.update(
            USERPROFILE=str(profile), APPDATA=str(roaming), LOCALAPPDATA=str(local),
            TEMP=str(temporary), TMP=str(temporary),
            PATH=os.pathsep.join(map(str, (system32, windows, system32 / 'WindowsPowerShell' / 'v1.0'))),
            AGY_CLI_DISABLE_AUTO_UPDATE='true',
        )

    def close(self):
        if self._directory is not None:
            directory, self._directory = self._directory, None
            import time
            for delay in (0, 0.05, 0.15, 0.35):
                if delay:
                    time.sleep(delay)
                try:
                    directory.cleanup()
                    return
                except PermissionError:
                    continue
            try:
                directory.cleanup()
            except OSError:
                pass
