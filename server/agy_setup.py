"""Open Antigravity's first-run wizard in the app's dedicated Windows profile."""

import json
import subprocess

from .agy_profile import AgyPrivateProfile, agy_executable
from .providers import ProviderError


def main():
    try:
        profile = AgyPrivateProfile(setup=True)
        executable = agy_executable()
    except ProviderError as error:
        print(str(error), flush=True)
        return 1
    settings = json.dumps({
        'modelProvider': '', 'trustedWorkspaces': [str(profile.workspace)], 'toolPermission': 'strict',
        'permissions': {'deny': ['command(*)', 'read_file(*)', 'write_file(*)', 'read_url(*)',
                                 'execute_url(*)', 'mcp(*)']},
    })
    print('請在 agy 完成 AI Tutor 的專用首次設定。到達可輸入問題的主畫面後，輸入 /exit 離開。', flush=True)
    return subprocess.call([str(executable), '--sandbox', '--settings', settings],
                           cwd=profile.workspace, env=profile.env)


if __name__ == '__main__':
    raise SystemExit(main())
