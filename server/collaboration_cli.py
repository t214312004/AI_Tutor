"""File-backed role tools. Payloads are free-form text; envelopes are core metadata.

Copied into each private role workspace and executed by its CLI shell tool.
No network, credentials or application imports are needed.
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path
import time
from uuid import uuid4


def safe_path(root, relative):
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('檔案不在本回合工作區')
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('不接受連結檔案')
    return path


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(text, encoding='utf-8')
        for attempt,delay in enumerate((.05,.15,.35,0)):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as error:
                if getattr(error,'winerror',None) not in (32,33) or attempt==3:raise
                time.sleep(delay)
    except OSError as error:
        logging.getLogger('tutor.transport').error('filesystem_failed operation=atomic_write file=%s errno=%s winerror=%s type=%s',
            path.name,getattr(error,'errno',None),getattr(error,'winerror',None),type(error).__name__)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def ask(root, token, source, fresh=False, timeout=150):
    active = json.loads(safe_path(root, 'internal/active-turn.json').read_text(encoding='utf-8'))
    if active.get('token') != token or active.get('role') != 'teacher':
        raise ValueError('這個協作回合已失效')
    turn = safe_path(root, 'drafts/' + token)
    message_path = safe_path(turn, source)
    message = message_path.read_text(encoding='utf-8')
    if not 1 <= len(message.strip()) <= 6000:
        raise ValueError('請留下簡短而具體的自然語言問題')
    request = turn / 'requests' / uuid4().hex
    request.mkdir(parents=True)
    atomic_text(request / 'question.md', message)
    atomic_text(request / 'ready.json', json.dumps({'token': token, 'fresh': fresh}))
    deadline = min(time.monotonic() + timeout, active.get('deadline',float('inf')))
    while time.monotonic() < deadline:
        try:
            current = json.loads(safe_path(root, 'internal/active-turn.json').read_text(encoding='utf-8'))
        except (PermissionError, json.JSONDecodeError):
            time.sleep(.05)
            continue
        if current.get('token') != token:
            raise ValueError('課堂已改變，這個協作回合已取消')
        answer = request / 'answer.md'
        if answer.exists():
            return safe_path(turn, str(answer.relative_to(turn))).read_text(encoding='utf-8')
        time.sleep(.15)
    raise TimeoutError('照片核對尚未完成；不要猜測')


def read_answer(root, token, source):
    active=json.loads(safe_path(root,'internal/active-turn.json').read_text(encoding='utf-8'))
    if active.get('token')!=token or active.get('role')!='teacher':raise ValueError('這個協作回合已失效')
    turn=safe_path(root,'drafts/'+token)
    question=safe_path(turn,source).read_text(encoding='utf-8')
    answers=[]
    for path in safe_path(turn,'requests').glob('*/question.md'):
        path=safe_path(turn,str(path.relative_to(turn)))
        answer=path.with_name('answer.md')
        if path.read_text(encoding='utf-8')==question and answer.exists():answers.append(answer)
    if not answers:raise ValueError('原問題尚未取得答案；不要重複提出相同核對。')
    answer=max(answers,key=lambda p:p.stat().st_mtime_ns)
    return safe_path(turn,str(answer.relative_to(turn))).read_text(encoding='utf-8')


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='strict')
    parser = argparse.ArgumentParser()
    parser.add_argument('--turn', required=True)
    parser.add_argument('action', choices=['ask-vision','read-answer'])
    parser.add_argument('--file', required=True, help='相對於本回合 drafts 目錄的 Markdown 檔')
    parser.add_argument('--fresh', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    try:
        print(read_answer(root,args.turn,args.file) if args.action=='read-answer' else
              ask(root, args.turn, args.file, args.fresh))
    except (OSError, ValueError, TimeoutError) as error:
        print(str(error))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
