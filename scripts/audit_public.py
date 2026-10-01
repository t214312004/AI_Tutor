"""Audit/export an explicit UTF-8 allowlist; never print matching secret values."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'scripts' / 'public-files.json'
DENIED_PARTS = {'.git', '.local', '.runtime', '.venv', 'app-data', 'node_modules', 'dist', 'test-results', 'data'}
DENIED_SUFFIXES = {'.sqlite3', '.db', '.log', '.pem', '.key', '.bak', '.backup', '.zip', '.pdf', '.png', '.jpg', '.mp3', '.wav'}
PATTERNS = {
    'provider_secret': re.compile(r'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gsk_[A-Za-z0-9]{24,}|AIza[A-Za-z0-9_-]{30,})'),
    'github_secret': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'machine_path': re.compile(r'(?i)[A-Z]:[\\/](?:Users|ai_projects)[\\/]'),
    'url_credential': re.compile(r'https?://[^\s/:@]+:[^\s/@]+@'),
}


def approved_files():
    names = json.loads(MANIFEST.read_text(encoding='utf-8'))['files']
    if len(names) != len(set(names)):
        raise ValueError('Duplicate allowlist paths')
    for name in names:
        relative = PurePosixPath(name)
        if relative.is_absolute() or '\\' in name or ':' in name or '..' in relative.parts:
            raise ValueError('Invalid allowlist path')
        if any(part in DENIED_PARTS for part in relative.parts) or relative.suffix in DENIED_SUFFIXES:
            raise ValueError('Private/binary path in allowlist')
        if relative.name in {'school.json', 'auth.json', 'credentials.machine'}:
            raise ValueError('Private configuration in allowlist')
        file = ROOT / name
        for candidate in (file, *file.parents):
            if candidate == ROOT.parent:
                break
            if candidate.is_symlink() or candidate.is_junction():
                raise ValueError('Linked path in allowlist')
        if not file.is_file() or not file.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError('Missing or escaped allowlist file')
    return names


def scan(name, content, private_terms):
    failures = []
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        return [{'path': name, 'category': 'non_utf8_binary'}]
    for category, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            failures.append({'path': name, 'line': text.count('\n', 0, match.start()) + 1, 'category': category})
    for term in private_terms:
        if term and term in text:
            failures.append({'path': name, 'category': 'private_term'})
    return failures


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def audit(private_terms=(), staged=False, history=False):
    names = approved_files()
    failures = []
    for name in names:
        failures.extend(scan(name, (ROOT / name).read_bytes(), private_terms))
    if (ROOT / '.git').exists():
        tracked = git('ls-files', '-z').decode().strip('\0').split('\0')
        for name in filter(None, tracked):
            if name not in names:
                failures.append({'path': name, 'category': 'tracked_outside_allowlist'})
        if staged:
            for name in filter(None, git('ls-files', '-z').decode().split('\0')):
                if name in names:
                    failures.extend(scan(name, git('show', ':' + name), private_terms))
        if history:
            objects = git('rev-list', '--objects', '--all').decode().splitlines()
            for entry in objects:
                oid, _, name = entry.partition(' ')
                if not name or git('cat-file', '-t', oid).strip() != b'blob':
                    continue
                if name not in names:
                    failures.append({'path': name, 'category': 'history_outside_allowlist'})
                failures.extend(scan(name, git('cat-file', 'blob', oid), private_terms))
    elif staged or history:
        raise ValueError('Git repository required for staged/history audit')
    return names, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-terms', type=Path)
    parser.add_argument('--staged', action='store_true')
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--export', type=Path)
    args = parser.parse_args()
    terms = args.private_terms.read_text(encoding='utf-8').splitlines() if args.private_terms else []
    names, failures = audit(terms, args.staged, args.history)
    if failures:
        print(json.dumps({'passed': False, 'findings': failures}, ensure_ascii=False))
        raise SystemExit(1)
    if args.export:
        target = args.export.resolve()
        if not args.export.is_absolute() or target.exists():
            raise ValueError('Export requires a new absolute directory')
        for parent in args.export.parents:
            if parent.exists() and (parent.is_symlink() or parent.is_junction()):
                raise ValueError('Export parent is a link')
        target.mkdir(parents=True)
        for name in names:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
    digest = hashlib.sha256()
    for name in sorted(names):
        digest.update(name.encode() + b'\0' + (ROOT / name).read_bytes())
    print(json.dumps({'passed': True, 'files': len(names), 'content_hash': digest.hexdigest(),
                      'staged': args.staged, 'history': args.history, 'exported': bool(args.export)}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        # Category only: exception messages may contain private paths or values.
        print(json.dumps({'passed': False, 'error_type': type(error).__name__}))
        raise SystemExit(1)
