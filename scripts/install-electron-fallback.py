"""Official Electron download fallback when Node's downloader stalls on Windows.
Checks the npm package's bundled SHA-256 before extracting to its own dist folder.
"""
import hashlib
import argparse
import json
import zipfile
from pathlib import Path
import httpx

root=Path(__file__).resolve().parents[1]
package=root/'node_modules/electron'
version=json.loads((package/'package.json').read_text())['version']
name=f'electron-v{version}-win32-x64.zip'
checksum=json.loads((package/'checksums.json').read_text())[name]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--archive',type=Path,help='Optional offline official archive; checksum is always verified')
args=parser.parse_args()
target=args.archive or root/'.local'/name
target.parent.mkdir(exist_ok=True)
digest=hashlib.sha256();size=0
if args.archive:
    with target.open('rb') as file:
        for chunk in iter(lambda:file.read(1024*1024),b''):digest.update(chunk)
else:
    with httpx.stream('GET',f'https://github.com/electron/electron/releases/download/v{version}/{name}',follow_redirects=True,timeout=60) as response:
        response.raise_for_status()
        with target.open('wb') as file:
            for chunk in response.iter_bytes(1024*1024):
                file.write(chunk);digest.update(chunk);size+=len(chunk)
                if size//(20*1024*1024)!=(size-len(chunk))//(20*1024*1024):print(f'Downloaded {size//1024//1024} MiB',flush=True)
if digest.hexdigest()!=checksum:raise RuntimeError('Electron checksum mismatch')
destination=(package/'dist').resolve();destination.mkdir(exist_ok=True)
with zipfile.ZipFile(target) as archive:
    for member in archive.infolist():
        resolved=(destination/member.filename).resolve()
        if not resolved.is_relative_to(destination):raise RuntimeError('Archive path escapes destination')
    archive.extractall(destination)
(package/'path.txt').write_text('electron.exe',encoding='utf-8')
print(f'Installed verified Electron {version}',flush=True)
