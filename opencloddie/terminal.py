"""PTY and tmux helpers used by the client agent and tests."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def tmux_has_session(name: str) -> bool:
    if not name:
        return False
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={name}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def tmux_kill_session(name: str) -> bool:
    if not name:
        return False
    result = subprocess.run(
        ["tmux", "kill-session", "-t", f"={name}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def tmux_new_session(name: str, command: Sequence[str], cwd: str, env: dict[str, str]) -> None:
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", cwd, "--", *command],
        check=True,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def tmux_list_sessions() -> list[str]:
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def set_winsize(fd: int, cols: int, rows: int) -> None:
    cols = max(2, min(int(cols), 1000))
    rows = max(2, min(int(rows), 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except (BlockingIOError, InterruptedError):
            continue
        if written <= 0:
            raise OSError("short write to pty")
        view = view[written:]


@dataclass
class PtyProcess:
    master_fd: int
    process: subprocess.Popen[bytes]
    _closed_lock: threading.Lock = field(default_factory=threading.Lock)
    _closed: bool = False

    @property
    def pid(self) -> int:
        return self.process.pid

    def resize(self, cols: int, rows: int) -> None:
        try:
            set_winsize(self.master_fd, cols, rows)
        except OSError:
            pass

    def write(self, data: bytes) -> None:
        try:
            write_all(self.master_fd, data)
        except OSError:
            pass

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self, *, force: bool = False) -> None:
        with self._closed_lock:
            if self._closed:
                return
            self._closed = True
        try:
            if self.process.poll() is None:
                if force:
                    self.process.kill()
                else:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            try:
                os.close(self.master_fd)
            except OSError:
                pass


def spawn_pty(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    cols: int = 120,
    rows: int = 32,
) -> PtyProcess:
    """Start argv on a fresh PTY.  The child gets its own session but the PTY
    slave is a real tty, which is enough for tmux attach and Claude Code."""
    master_fd, slave_fd = pty.openpty()
    try:
        set_winsize(slave_fd, cols, rows)
        child_env = dict(os.environ if env is None else env)
        child_env.setdefault("TERM", "xterm-256color")
        process = subprocess.Popen(
            list(argv),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=child_env,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    os.close(slave_fd)
    return PtyProcess(master_fd=master_fd, process=process)


def pump_pty(
    proc: PtyProcess,
    on_data: Callable[[bytes], None],
    stop_event: threading.Event,
    *,
    on_exit: Callable[[int | None], None] | None = None,
) -> None:
    """Read from the PTY until EOF/exit.  Intended for a dedicated thread."""
    try:
        while not stop_event.is_set():
            try:
                ready, _, _ = select.select([proc.master_fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(proc.master_fd, 65536)
            except (OSError, BlockingIOError):
                break
            if not data:
                break
            try:
                on_data(data)
            except Exception:  # noqa: BLE001 - a dead viewer must not kill the pump
                break
    finally:
        code = proc.poll()
        if code is None:
            time.sleep(0.05)
            code = proc.poll()
        if on_exit:
            try:
                on_exit(code)
            except Exception:  # noqa: BLE001
                pass


def terminate_process_group(pid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass
