"""Install the fixed owner commands for the local OpenBot Settings button."""
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

SCRIPTS = Path(__file__).resolve().parent
SOURCE = SCRIPTS.parents[1]
ROOT = Path.home() / "Library/Application Support/OpenBot Fork Updates"
FILES = ("update.py", "package_macos.py", "install_macos.py", "verify_copy.py", "probe-copy.mjs")


def write(name, data, mode=0o600):
    with tempfile.NamedTemporaryFile(dir=ROOT, delete=False) as f:
        f.write(data)
        os.fchmod(f.fileno(), mode)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, ROOT / name)


def main():
    if sys.platform != "darwin" or sys.version_info < (3, 11) or os.getuid() == 0:
        sys.exit("Use Python 3.11 or later as the signed-in Mac owner.")
    os.umask(0o077)
    root = subprocess.check_output(["git", "-C", SOURCE, "rev-parse", "--show-toplevel"], text=True).strip()
    if Path(root).resolve() != SOURCE:
        sys.exit("Run configure.py from a committed OpenBot checkout.")
    seed = subprocess.check_output(["git", "-C", SOURCE, "rev-parse", "HEAD"], text=True).strip()
    contents = {name: subprocess.check_output(["git", "-C", SOURCE, "show", seed + ":scripts/fork-update/" + name]) for name in FILES}
    if any((SCRIPTS / k).read_bytes() != v for k, v in contents.items()):
        sys.exit("Commit the owner commands before configuring them.")
    ROOT.mkdir(mode=0o700, exist_ok=True)
    st = ROOT.lstat()
    if ROOT.resolve() != ROOT or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700 or not stat.S_ISDIR(st.st_mode):
        sys.exit("The owner command directory must be a private directory owned by this user.")
    paths = [str(Path.home() / ".local/share/mise/installs/node/24/bin"), str(Path.home() / ".bun/bin"),
             "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    for tool in ("node", "bun", "gh", "git", "xcodebuild", "codesign"):
        if not shutil.which(tool, path=os.pathsep.join(paths)):
            sys.exit("Install the required tool: " + tool)
    with (ROOT / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Pin the complete seed in owner storage. Removing the development worktree later is safe.
        cache = ROOT / "cache.git"
        if not cache.exists():
            subprocess.run(["git", "init", "--bare", cache], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", cache, "fetch", "--no-tags", SOURCE, seed + ":refs/pinned/" + seed], check=True)
        for name, data in contents.items():
            write(name, data)
        write("config.json", json.dumps({"source": str(SOURCE), "seed": seed, "path": paths}).encode())
        # The renderer cannot supply an executable, shell text, branch, repository, or filesystem path.
        line = "exec " + shlex.quote(sys.executable) + " -I " + shlex.quote(str(ROOT / "update.py")) + ' "$@"\n'
        write("update", ("#!/bin/sh\n" + line).encode(), 0o700)
    print("Configured the owner-only updater at", ROOT)


if __name__ == "__main__":
    main()
