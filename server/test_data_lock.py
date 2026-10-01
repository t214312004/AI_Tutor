import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from .data_lock import DataLock

class DataLockTests(unittest.TestCase):
    def test_competing_process_cannot_open_and_exit_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            lock = DataLock(root)
            script = 'from server.data_lock import DataLock; from pathlib import Path; import sys; lock=DataLock(Path(sys.argv[1])); lock.close()'
            blocked = subprocess.run([sys.executable, '-c', script, folder], capture_output=True)
            self.assertNotEqual(blocked.returncode, 0)
            lock.close()
            available = subprocess.run([sys.executable, '-c', script, folder], capture_output=True)
            self.assertEqual(available.returncode, 0, available.stderr)
