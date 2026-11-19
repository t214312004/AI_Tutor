"""Executable discovery and shared errors for subscription adapters."""
import os
from pathlib import Path
import shutil


class ProviderError(RuntimeError):
    pass


def codex_command():
    override=os.environ.get('TUTOR_CODEX_EXECUTABLE')
    if override:return [override]
    script=Path(__file__).resolve().parents[1]/'node_modules/@openai/codex/bin/codex.js'
    node=shutil.which('node')
    if node and script.exists():return [node,str(script)]
    exe=shutil.which('codex.exe') or shutil.which('codex')
    if exe and not exe.lower().endswith(('.cmd','.ps1','.bat')):return [exe]
    raise ProviderError('找不到可直接執行的 Codex；請設定 TUTOR_CODEX_EXECUTABLE')
