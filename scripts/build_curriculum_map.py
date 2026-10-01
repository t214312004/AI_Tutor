"""Offline, deterministic rebuild of source-backed curriculum code catalog."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.curriculum_map import build

if __name__ == '__main__':
    print(json.dumps(build(), ensure_ascii=False))
