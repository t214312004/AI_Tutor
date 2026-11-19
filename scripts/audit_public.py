"""Audit/export an explicit UTF-8 allowlist; never print matching secret values."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from tempfile import TemporaryFile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'scripts' / 'public-files.json'
MANIFEST_NAME = 'scripts/public-files.json'
REGULAR_MODES = {'100644', '100755'}
DENIED_PARTS = {'.git', '.local', '.runtime', '.venv', 'app-data', 'node_modules', 'dist', 'test-results', 'data'}
DENIED_SUFFIXES = {'.sqlite3', '.db', '.log', '.pem', '.key', '.bak', '.backup', '.zip', '.pdf', '.png', '.jpg', '.mp3', '.wav'}
PATTERNS = {
    'provider_secret': re.compile(r'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gsk_[A-Za-z0-9]{24,}|AIza[A-Za-z0-9_-]{30,})'),
    'github_secret': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'machine_path': re.compile(r'(?i)[A-Z]:[\\/](?:Users|ai_projects)[\\/]'),
    'url_credential': re.compile(r'https?://[^\s/:@]+:[^\s/@]+@'),
}


def manifest_paths(content):
    manifest = json.loads(content)
    names = manifest['files']
    retired = manifest.get('retired', [])
    paths = names + retired
    if len(paths) != len({name.casefold() for name in paths}):
        raise ValueError('Duplicate allowlist paths')
    for name in paths:
        relative = PurePosixPath(name)
        if (relative.as_posix() != name or relative.is_absolute() or '\\' in name or ':' in name
                or not relative.parts or '..' in relative.parts or any(ord(c) < 32 for c in name)):
            raise ValueError('Invalid allowlist path')
        if any(part.lower() in DENIED_PARTS for part in relative.parts) or relative.suffix.lower() in DENIED_SUFFIXES:
            raise ValueError('Private/binary path in allowlist')
        if relative.name.lower() in {'school.json', 'auth.json', 'credentials.machine'}:
            raise ValueError('Private configuration in allowlist')
    if MANIFEST_NAME not in names:
        raise ValueError('Manifest missing from allowlist')
    return set(names), set(retired)


def working_files(manifest, names, retired):
    for name in retired:
        file = ROOT / name
        if file.exists() or file.is_symlink() or file.is_junction():
            raise ValueError('Retired file is still in the working tree')
    files = {}
    for name in names:
        file = ROOT / name
        for candidate in (file, *file.parents):
            if candidate == ROOT.parent:
                break
            if candidate.is_symlink() or candidate.is_junction():
                raise ValueError('Linked path in allowlist')
        if not file.is_file() or not file.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError('Missing or escaped allowlist file')
        files[name] = manifest if name == MANIFEST_NAME else file.read_bytes()
    return files


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


def git_batch_blobs(oids):
    """Read many git blobs/types through a single git cat-file --batch process."""
    if not oids:
        return
    command = ['git', '-C', str(ROOT), 'cat-file', '--batch']
    # Git can fill its stdout pipe before it consumes all of stdin on Windows.
    with TemporaryFile() as requests:
        requests.write(('\n'.join(oids) + '\n').encode('ascii'))
        requests.seek(0)
        with subprocess.Popen(command, stdin=requests, stdout=subprocess.PIPE) as proc:
            try:
                for oid in oids:
                    header = proc.stdout.readline().decode('ascii').split()
                    if len(header) != 3 or header[:2] != [oid, 'blob']:
                        raise ValueError('Missing or invalid Git blob')
                    size = int(header[2])
                    content = proc.stdout.read(size)
                    if len(content) != size or proc.stdout.read(1) != b'\n':
                        raise ValueError('Truncated Git blob')
                    yield oid, content
                if proc.wait():
                    raise subprocess.CalledProcessError(proc.returncode, command)
            finally:
                if proc.poll() is None:
                    proc.kill()


def git_entries(*args, index=False):
    for entry in git(*args).decode('utf-8').split('\0'):
        if entry:
            metadata, name = entry.split('\t', 1)
            mode, first, second = metadata.split()
            if index and second != '0':
                raise ValueError('Unmerged Git index')
            yield mode, first if index else second, name


def audit(private_terms=(), staged=False, history=False):
    failures = []
    has_git = (ROOT / '.git').exists() and shutil.which('git') is not None
    if (staged or history) and not has_git:
        raise ValueError('Git repository and git executable required for staged/history audit')
    manifest = git('show', ':' + MANIFEST_NAME) if staged else MANIFEST.read_bytes()
    names, retired = manifest_paths(manifest)
    files = {} if staged else working_files(manifest, names, retired)
    if has_git:
        tracked = {}
        for mode, oid, name in git_entries('ls-files', '--stage', '-z', index=True):
            if name not in names and (staged or name not in retired):
                failures.append({'path': name, 'category': 'tracked_outside_allowlist'})
            elif mode not in REGULAR_MODES:
                failures.append({'path': name, 'category': 'non_regular_index_entry'})
            else:
                tracked[name] = oid
        if staged:
            for name in sorted(names - tracked.keys()):
                failures.append({'path': name, 'category': 'missing_from_index'})
            blobs = dict(git_batch_blobs(list(dict.fromkeys(tracked.values()))))
            files = {name: blobs[oid] for name, oid in tracked.items()}
        if history:
            trees = dict.fromkeys(git('rev-list', '--all', '--no-commit-header', '--format=%T').decode().splitlines())
            allowed = names | retired
            blob_paths = {}
            seen = set()
            for tree in trees:
                for mode, oid, name in git_entries('ls-tree', '-rz', tree):
                    entry = mode, oid, name
                    if entry in seen:
                        continue
                    seen.add(entry)
                    if name not in allowed:
                        failures.append({'path': name, 'category': 'history_outside_allowlist'})
                    elif mode not in REGULAR_MODES:
                        failures.append({'path': name, 'category': 'non_regular_history_entry'})
                    else:
                        blob_paths.setdefault(oid, name)
            for oid, content in git_batch_blobs(list(blob_paths)):
                failures.extend(scan(blob_paths[oid], content, private_terms))
    for name, content in files.items():
        failures.extend(scan(name, content, private_terms))
    return files, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-terms', type=Path)
    parser.add_argument('--staged', action='store_true')
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--export', type=Path)
    args = parser.parse_args()
    terms = args.private_terms.read_text(encoding='utf-8').splitlines() if args.private_terms else []
    files, failures = audit(terms, args.staged, args.history)
    if failures:
        print(json.dumps({'passed': False, 'findings': failures}, ensure_ascii=True))
        raise SystemExit(1)
    if args.export:
        target = args.export.resolve()
        if not args.export.is_absolute() or target.exists():
            raise ValueError('Export requires a new absolute directory')
        for parent in args.export.parents:
            if parent.exists() and (parent.is_symlink() or parent.is_junction()):
                raise ValueError('Export parent is a link')
        target.mkdir(parents=True)
        for name, content in files.items():
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b'\0' + files[name])
    print(json.dumps({'passed': True, 'files': len(files), 'content_hash': digest.hexdigest(),
                      'staged': args.staged, 'history': args.history, 'exported': bool(args.export)}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        # Category only: exception messages may contain private paths or values.
        print(json.dumps({'passed': False, 'error_type': type(error).__name__}))
        raise SystemExit(1)
