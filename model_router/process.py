"""Bounded subprocess I/O. Kill the process tree on cancellation/timeouts."""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path


def spawn(argv, cwd=None, stdin=True):
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", bufsize=1,
                            shell=False, creationflags=flags, start_new_session=os.name != "nt")


def kill_tree(proc):
    if os.name == "nt":
        # PID is an integer from Popen, never shell-interpolated user input.
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


class LineProcess:
    def __init__(self, argv, cwd=None):
        self.proc = spawn(argv, cwd)
        self.queue = queue.Queue(maxsize=256)
        self.closed = threading.Event()
        self.readers = []
        for source, stream in (("stdout", self.proc.stdout), ("stderr", self.proc.stderr)):
            thread = threading.Thread(target=self._reader, args=(source, stream), daemon=True)
            thread.start()
            self.readers.append(thread)

    def _reader(self, source, stream):
        try:
            while not self.closed.is_set():
                line = stream.readline(262145)
                if not line:
                    break
                item = (source if len(line) <= 262144 else "overflow", line[:262144])
                while not self.closed.is_set():
                    try:
                        self.queue.put(item, timeout=.1)
                        break
                    except queue.Full:
                        pass
        finally:
            stream.close()

    def send(self, value):
        self.proc.stdin.write(json.dumps(value) + "\n")
        self.proc.stdin.flush()

    def write_input(self, text):
        def writer():
            try:
                self.proc.stdin.write(text)
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass  # Exit code / missing completion is handled by the supervisor.
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        return thread

    def poll(self, timeout=.1):
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def finished(self):
        return self.proc.poll() is not None and all(not t.is_alive() for t in self.readers) and self.queue.empty()

    def close(self):
        self.closed.set()
        kill_tree(self.proc)
        if self.proc.stdin:
            self.proc.stdin.close()
        for thread in self.readers:
            thread.join(timeout=1)


def run_command(argv, cwd: Path, timeout: float, max_output=64000):
    start = time.monotonic()
    child = LineProcess(argv, cwd)
    chunks, size = [], 0
    reason = None
    try:
        child.proc.stdin.close()
        while not child.finished():
            if time.monotonic() - start >= timeout:
                reason = "timeout"
                break
            item = child.poll()
            if item:
                source, line = item
                size += len(line.encode("utf-8"))
                if size > max_output or source == "overflow":
                    reason = "output_limit"
                    break
                chunks.append(line)
        return {"argv": argv, "returncode": child.proc.poll() if not reason else -1,
                "output": "".join(chunks)[-max_output:], "error": reason,
                "runtime": time.monotonic() - start}
    finally:
        child.close()
