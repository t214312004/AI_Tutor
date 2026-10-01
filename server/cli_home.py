"""Project-local Codex state, with credentials isolated by Windows SID."""
import os
from pathlib import Path
import shutil
import subprocess
from .paths import tutor_home

PROJECT = Path(__file__).resolve().parents[1]

def codex_home():
    if os.name == 'nt':
        result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        import csv
        sid = next(csv.reader([result.stdout.strip()]))[1]
        if not sid.startswith('S-1-'): raise RuntimeError('無法辨識 Windows 帳號')
    else:
        sid = str(os.getuid())
    target = tutor_home() / 'accounts' / sid / 'codex'
    target.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        # Personal CLI credentials must not inherit the shared student-data ACL.
        subprocess.run(['icacls', str(target.parent), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F'], capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    source = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'auth.json'
    destination = target / 'auth.json'
    # Import only this login's auth, never its agents, skills, settings or sessions.
    if source.exists() and source.resolve() != destination.resolve():
        if not destination.exists() or source.stat().st_mtime > destination.stat().st_mtime:
            shutil.copyfile(source, destination)
    (target / 'config.toml').write_text('cli_auth_credentials_store = "file"\n', encoding='utf-8')
    return target

if __name__ == '__main__':
    print(codex_home())
