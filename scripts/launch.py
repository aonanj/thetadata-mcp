"""Bootstrap an isolated local runtime; all setup output goes to stderr."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent.parent
PROBE = "from mcp.server.fastmcp import FastMCP; from thetadata import ThetaClient"


def ready(python):
    return subprocess.run([str(python), "-c", PROBE], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def main():
    if sys.version_info < (3, 12):
        sys.exit("ThetaData Options requires Python 3.12+. Set THETADATA_PYTHON to that interpreter's path.")
    # A source checkout can use a pre-provisioned project environment.
    local_python = ROOT / ".venv/bin/python"
    if local_python.exists() and ready(local_python):
        python = local_python
    else:
        import fcntl
        digest = hashlib.sha256((ROOT / "requirements.txt").read_bytes() +
                                f"{sys.version_info[:2]}:{sys.executable}".encode()).hexdigest()[:16]
        cache = Path(os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "thetadata-options"
        cache.mkdir(parents=True, exist_ok=True)
        environment = cache / digest
        python = environment / "bin/python"
        with (cache / "setup.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not python.exists() or not ready(python):
                print("Preparing the ThetaData Options Python environment...", file=sys.stderr)
                venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
                subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                                "-r", str(ROOT / "requirements.txt")], check=True,
                               stdout=sys.stderr, stderr=sys.stderr)
                if not ready(python):
                    sys.exit("ThetaData Options dependency setup failed. See the installation output.")
    if "--setup-only" in sys.argv:
        print("ThetaData Options runtime is ready.", file=sys.stderr)
        return
    os.execv(str(python), [str(python), str(ROOT / "server/main.py")])


if __name__ == "__main__":
    main()
