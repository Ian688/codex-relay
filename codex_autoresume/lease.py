"""One live controller per SQLite state file, shared by CLI and desktop UI."""
from pathlib import Path
import fcntl
import os

class MonitorLease:
    def __init__(self, db_path):
        self.path = Path(str(Path(db_path).expanduser().resolve()) + '.monitor.lock')
        self.handle = None

    def acquire(self):
        if self.handle is not None: return
        self.path.parent.mkdir(parents=True,exist_ok=True)
        handle = self.path.open('a+')
        try:
            os.fchmod(handle.fileno(),0o600)
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError('另一个 Relay 实例正在监控。请先在原窗口暂停，或在其终端按 Ctrl+C。')
        self.handle = handle

    def release(self):
        if self.handle is not None:
            fcntl.flock(self.handle,fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def busy(self):
        if self.handle is not None: return False
        probe = MonitorLease(str(self.path)[:-len('.monitor.lock')])
        try:
            probe.acquire()
            return False
        except RuntimeError:
            return True
        finally: probe.release()

    def __enter__(self): self.acquire(); return self
    def __exit__(self,*args): self.release()
