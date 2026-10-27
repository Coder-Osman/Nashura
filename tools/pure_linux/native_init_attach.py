"""Attach GDB only to the new child created here, after its SIGSTOP marker."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    specification = json.loads(Path(sys.argv[1]).read_text())
    child = subprocess.Popen(specification["inferior"], env=specification["environment"])
    try:
        deadline = time.monotonic() + 15
        while True:
            process_id, status = os.waitpid(child.pid, os.WUNTRACED | os.WNOHANG)
            if process_id:
                if not os.WIFSTOPPED(status) or os.WSTOPSIG(status) != signal.SIGSTOP:
                    raise RuntimeError("owned child did not reach SIGSTOP marker")
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("owned child marker timeout")
            time.sleep(0.01)
        environment = os.environ.copy()
        environment["NASHURA_GDB_OWNED_PID"] = str(child.pid)
        os.execvpe("gdb", ["gdb", "-nx", "-nh", "--batch", "-p", str(child.pid),
                          "-ex", "source " + specification["script"]], environment)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


if __name__ == "__main__":
    main()
