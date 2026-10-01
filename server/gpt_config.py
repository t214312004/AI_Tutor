"""GPT-Live route choices and server-side validation.

The API catalog is deliberately small: /v1/models does not advertise effort or
image capabilities. CLI choices are discovered from the installed clients.
"""
import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx

from .providers import CodexClient, ProviderError

API_MODELS = {
    'gpt-6.1-sol': {'efforts': ['low', 'medium', 'high', 'xhigh', 'max'], 'image': True},
    'gpt-6-luna': {'efforts': ['none', 'low', 'medium', 'high', 'xhigh', 'max'], 'image': True},
}
LEGACY_GPT = {'version': 1, 'delegation': {'type': 'client', 'provider': 'legacy_failover'},
              'vision': {'provider': 'legacy_failover'}}


async def codex_models(workspace: Path):
    client = CodexClient(workspace)
    try:
        await client.start()
        models = []
        cursor = None
        while True:
            params = {'limit': 100, 'includeHidden': False}
            if cursor: params['cursor'] = cursor
            page = await client.request('model/list', params)
            for item in page.get('data', []):
                name = item.get('model') or item.get('id')
                if not name: continue
                efforts = [entry['reasoningEffort'] for entry in item.get('supportedReasoningEfforts', [])
                           if isinstance(entry, dict) and entry.get('reasoningEffort')]
                if efforts:
                    models.append({'id': name, 'label': item.get('displayName') or name,
                                   'efforts': efforts,
                                   'image': 'image' in item.get('inputModalities', ['text', 'image'])})
            cursor = page.get('nextCursor')
            if not cursor: break
        return models
    finally:
        await client.close()


async def agy_models():
    executable = shutil.which('agy.exe') or shutil.which('agy')
    if not executable: raise ProviderError('找不到 agy')
    process = await asyncio.create_subprocess_exec(executable, 'models',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        out, _ = await asyncio.wait_for(process.communicate(), 15)
    except TimeoutError:
        process.kill(); await process.wait()
        raise ProviderError('agy 模型清單逾時')
    if process.returncode: raise ProviderError('agy 無法列出可用模型')
    models = []
    for line in out.decode('utf-8', errors='replace').splitlines():
        match = re.match(r'^\s*([a-zA-Z][a-zA-Z0-9_.-]+)\s+(.+?)\s*$', line)
        if not match: continue
        model, label = match.groups()
        if '-' not in model: continue
        suffix = next((effort for effort in ('low', 'medium', 'high', 'max')
                       if model.endswith('-'+effort)), None)
        models.append({'id': model, 'label': label, 'efforts': [suffix] if suffix else ['low', 'medium', 'high', 'max'],
                       'image': True})
    if not models: raise ProviderError('agy 模型清單沒有可辨識的模型')
    return models


def api_models():
    return [{'id': name, 'label': name, **data} for name, data in API_MODELS.items()]


async def verify_openai_models(key, config):
    """Catch unavailable selected API models before starting a Live lesson."""
    models = set()
    if config['delegation']['type'] == 'responses':models.add(config['delegation']['model'])
    if config['vision']['provider'] == 'openai':models.add(config['vision']['model'])
    if not models:return
    try:
        async with httpx.AsyncClient(timeout=12) as http:
            for model in sorted(models):
                response = await http.get('https://api.openai.com/v1/models/'+model,
                                          headers={'Authorization': 'Bearer '+key})
                if response.status_code != 200:
                    raise ProviderError(f'OpenAI 模型 {model} 無法使用（HTTP {response.status_code}）')
    except httpx.HTTPError as error:
        raise ProviderError('無法檢查 OpenAI 模型權限') from error


async def route_options(workspace: Path, providers=None):
    if providers is None: providers = ('codex', 'agy')
    result = {'openai': {'models': api_models(), 'error': None}}
    for provider in providers:
        try:
            models = await (codex_models(workspace / 'codex') if provider == 'codex' else agy_models())
            result[provider] = {'models': models, 'error': None}
        except Exception as error:
            result[provider] = {'models': [], 'error': str(error)}
    return result


def validate_route(config, options):
    if not isinstance(config, dict) or config.get('version') != 1:
        raise ValueError('GPT Live 設定版本無效')
    delegation, vision = config.get('delegation'), config.get('vision')
    if not isinstance(delegation, dict) or not isinstance(vision, dict):
        raise ValueError('請設定教學委派與圖片辨識')
    mode = delegation.get('type')
    if mode not in ('responses', 'client'): raise ValueError('委派方式無效')
    if mode == 'responses':
        if delegation.get('provider') not in (None, 'openai'):
            raise ValueError('Responses 委派使用 OpenAI 模型')
        teaching_provider = 'openai'
    else:
        teaching_provider = delegation.get('provider')
        if teaching_provider not in ('codex', 'agy'): raise ValueError('Client 教學服務無效')
    vision_provider = vision.get('provider')
    if vision_provider not in ('openai', 'codex', 'agy'): raise ValueError('圖片辨識服務無效')
    for label, route, provider, require_image in (
        ('教學', delegation, teaching_provider, False),
        ('圖片辨識', vision, vision_provider, True),
    ):
        catalog = options.get(provider, {})
        if catalog.get('error'): raise ValueError(f'{label}服務無法使用：{catalog["error"]}')
        model = next((item for item in catalog.get('models', []) if item['id'] == route.get('model')), None)
        if not model: raise ValueError(f'{label}模型不可用：{route.get("model", "未選擇")}')
        if route.get('effort') not in model['efforts']:
            raise ValueError(f'{label}模型不支援 effort {route.get("effort", "未選擇")}')
        if require_image and not model['image']:
            raise ValueError(f'{label}模型不支援圖片輸入')
    return {'version': 1,
            'delegation': {'type': mode, **({'provider': teaching_provider} if mode == 'client' else {}),
                           'model': delegation['model'], 'effort': delegation['effort']},
            'vision': {'provider': vision_provider, 'model': vision['model'], 'effort': vision['effort']}}
