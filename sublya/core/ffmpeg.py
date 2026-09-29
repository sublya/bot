import os
import shutil
import subprocess

STDERR_TAIL = 2000


class FFmpegError(RuntimeError):
    def __init__(self, cmd: list[str], stderr: str):
        self.cmd = cmd
        self.stderr = stderr[-STDERR_TAIL:]
        super().__init__(f"{os.path.basename(cmd[0])} failed: {self.stderr.strip()[-300:]}")


def tool(name: str) -> str:
    """SUBLYA_FFMPEG/SUBLYA_FFPROBE win over PATH: Homebrew's ffmpeg lacks libass."""
    path = os.environ.get(f"SUBLYA_{name.upper()}") or shutil.which(name)
    if not path:
        raise FileNotFoundError(f"{name} not found, set SUBLYA_{name.upper()}")
    return path


def run(*cmd: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        raise FFmpegError(list(cmd), proc.stderr)
    return proc


def has_filter(name: str) -> bool:
    try:
        out = run(tool("ffmpeg"), "-hide_banner", "-filters").stdout
    except (FileNotFoundError, FFmpegError):
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines())
