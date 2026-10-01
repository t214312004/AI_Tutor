"""One core per data directory, including across Windows login sessions."""
import os

class DataLock:
    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.file = (root / 'core.lock').open('a+b')
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('伴讀系統已在另一個視窗或 Windows 帳號執行，請先結束該次陪讀。') from None
    def close(self):
        self.file.close()
