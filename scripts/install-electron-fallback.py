"""Official Electron download fallback when Node's downloader stalls on Windows.
Checks the npm package's bundled SHA-256 before extracting to its own dist folder.
"""
import argparse
import hashlib
import json
import os
import zipfile
from pathlib import Path
from uuid import uuid4
import httpx

root = Path(__file__).resolve().parents[1]
package = root / 'node_modules/electron'
version = json.loads((package / 'package.json').read_text(encoding='utf-8'))['version']
name = f'electron-v{version}-win32-x64.zip'
checksum = json.loads((package / 'checksums.json').read_text(encoding='utf-8'))[name]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--archive', type=Path, help='Optional offline official archive; checksum is always verified')
args = parser.parse_args()
target = args.archive or root / '.local' / name
target.parent.mkdir(parents=True, exist_ok=True)
def verified(file_path):
    if not file_path.is_file():
        return False
    digest = hashlib.sha256()
    with file_path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest() == checksum


if not verified(target):
    if args.archive:
        raise RuntimeError('Electron checksum mismatch')
    digest = hashlib.sha256()
    size = 0
    temp = target.with_name(f'.{name}.{uuid4().hex}.tmp')
    try:
        with httpx.stream('GET', f'https://github.com/electron/electron/releases/download/v{version}/{name}',
                          follow_redirects=True, timeout=60) as response:
            response.raise_for_status()
            with temp.open('wb') as file:
                for chunk in response.iter_bytes(1024 * 1024):
                    file.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if size // (20 * 1024 * 1024) != (size - len(chunk)) // (20 * 1024 * 1024):
                        print(f'Downloaded {size // 1024 // 1024} MiB', flush=True)
        if digest.hexdigest() != checksum:
            raise RuntimeError('Electron checksum mismatch')
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
destination = (package / 'dist').resolve()
destination.mkdir(exist_ok=True)
with zipfile.ZipFile(target) as archive:
    for member in archive.infolist():
        resolved = (destination / member.filename).resolve()
        if not resolved.is_relative_to(destination):
            raise RuntimeError('Archive path escapes destination')
    archive.extractall(destination)
(package / 'path.txt').write_text('electron.exe', encoding='utf-8')
print(f'Installed verified Electron {version}', flush=True)
