"""Prepare a verified recovery copy before publishing, then install that same notarized app."""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

APP = Path("/Applications/OpenBot.app")
SUPPORT = Path.home() / "Library/Application Support"
RECOVERY = SUPPORT / "OpenBot Fork Update Recovery"
PROFILE = SUPPORT / "OpenBot"
WORKSPACES = Path.home() / "OpenBot"
BUNDLE_ID = "app.openbot.desktop"
TEAM = "QC5HNMH72Y"
DEV_ID = (f'identifier "{BUNDLE_ID}" and anchor apple generic and '
          f'certificate leaf[subject.OU] = "{TEAM}" and certificate leaf[field.1.2.840.113635.100.6.1.13] exists')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True)


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
        json.dump(value, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp.name, path)


def private_dir(path):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
            "The update directory must be private and owned by this user.")


def signing(app, candidate=False):
    require(app.is_dir() and not app.is_symlink(), "Expected an application bundle.")
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", app)
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    require(info.get("CFBundleIdentifier") == BUNDLE_ID, "Wrong OpenBot bundle identifier.")
    signed = run("/usr/bin/codesign", "-dvvv", app).stderr.splitlines()
    cdhash = next((s[7:] for s in signed if s.startswith("CDHash=")), "")
    require(re.fullmatch(r"[0-9a-f]{40}", cdhash), "Cannot identify the signed bundle.")
    version = info.get("CFBundleShortVersionString", "")
    require(re.fullmatch(r"\d+\.\d+\.\d+", version), "Expected a stable desktop version.")
    if candidate or any(s.startswith("Authority=Developer ID Application:") for s in signed):
        run("/usr/bin/codesign", "--verify", "--strict", "-R=" + DEV_ID, app)
    else:
        # Only the known previous manual install may cross from a development certificate.
        legacy = json.loads((SUPPORT / "OpenBot Manual Updates/current.json").read_text())
        require(legacy.get("installedCDHash") == "CDHash=" + cdhash and
                any(s.startswith("Authority=Apple Development:") for s in signed),
                "Unrecognized manual installation.")
        run("/usr/bin/codesign", "--verify", "--strict", "-R=anchor apple generic", app)
    if candidate:
        run("/usr/bin/xcrun", "stapler", "validate", app)
        run("/usr/sbin/spctl", "--assess", "--type", "execute", app)
        feed = (app / "Contents/Resources/app-update.yml").read_text().splitlines()
        require(sorted(feed) == sorted(["provider: github", "owner: its-bash33r-here", "repo: openbot",
                                        "updaterCacheDirName: openbot-fork-updater"]), "Unexpected update feed.")
    return {"cdhash": cdhash, "version": version}


def processes():
    # Include child servers, Electron helpers, and other login sessions using the installed bundle.
    procs = {}
    for row in run("/bin/ps", "-axww", "-o", "pid=,uid=,comm=").stdout.splitlines():
        fields = row.strip().split(None, 2)
        if len(fields) == 3 and fields[2].startswith(str(APP) + "/"):
            require(int(fields[1]) == os.getuid(), "OpenBot is running in another Mac login session.")
            procs[int(fields[0])] = fields[2]
    return procs


def stopped():
    require(not processes(), "OpenBot is running. Wait for all work to finish and quit the installed app.")


def prepare_shutdown():
    active = processes()
    values = [os.environ.get("OPENBOT_FORK_UPDATE_" + n) for n in ("RUN", "PID", "NONCE")]
    if not any(values):
        stopped()
        return
    require(all(values), "Incomplete update request.")
    folder, pid, nonce = Path(values[0]), int(values[1]), values[2]
    private_dir(folder)
    require(folder.is_absolute() and folder.resolve() == folder and str(uuid.UUID(nonce)) == nonce,
            "Invalid update request.")
    request = {"pid": pid, "nonce": nonce, "bundle": str(APP)}
    ack = folder / "quit-ack.json"
    # A retry after the acknowledgement may already have a closed app.
    if active or not ack.exists():
        require(active.get(pid) == str(APP / "Contents/MacOS/OpenBot"), "The originating desktop session has ended.")
        atomic_json(folder / "quit-request.json", request)
    print("Waiting for OpenBot to finish its work and quit.", flush=True)
    deadline = time.monotonic() + 24 * 3600
    acknowledged = None
    while time.monotonic() < deadline:
        active = processes()
        if ack.exists():
            require(json.loads(ack.read_text()) == request, "The quit acknowledgement does not match this run.")
            if not active:
                return
            acknowledged = acknowledged or time.monotonic()
            require(time.monotonic() - acknowledged < 90, "Another OpenBot process is still running after quit.")
        elif pid not in active:
            raise RuntimeError("OpenBot exited without confirming that work finished.")
        time.sleep(1)
    raise RuntimeError("OpenBot has not finished quitting.")


def clone(source, target):
    require(not os.path.lexists(target), "Recovery target exists.")
    run("/bin/cp", "-cRp", source, target)


def tree_digest(root):
    """Compare every entry, file byte, mode and symlink target without following workspace links."""
    if not root.exists() and not root.is_symlink():
        return None
    require(root.is_dir() and not root.is_symlink(), "The profile and workspace roots must be real directories.")
    digest = hashlib.sha256()

    def visit(path):
        mode = path.lstat().st_mode
        digest.update(os.fsencode(path.relative_to(root)) + b"\0" + str(mode).encode() + b"\0")
        if stat.S_ISLNK(mode):
            digest.update(os.fsencode(os.readlink(path)) + b"\0")
        elif stat.S_ISDIR(mode):
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(mode):
            file_hash = hashlib.sha256()
            with path.open("rb") as file:
                for block in iter(lambda: file.read(1024 * 1024), b""):
                    file_hash.update(block)
            digest.update(file_hash.digest())
        else:
            raise RuntimeError("The backup contains an unsupported file type.")
    visit(root)
    return digest.hexdigest()


def database_check(profile, temp):
    dbs = [profile / "openbot.db", *(profile / "local-servers").glob("*/openbot.db")]
    for db in dbs:
        if not db.exists() and not profile.exists():
            continue
        require(db.is_file() and not db.is_symlink(), "Missing profile database.")
        # SQLite may change its shm file, even for a read-only connection. Use a disposable copy.
        with tempfile.TemporaryDirectory(dir=temp) as folder:
            path = Path(folder) / "check.db"
            for suffix in ("", "-wal", "-journal"):
                src = Path(str(db) + suffix)
                if src.exists():
                    clone(src, Path(str(path) + suffix))
            con = sqlite3.connect(str(path))
            try:
                require(con.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite integrity check failed.")
            finally:
                con.close()


def data_hashes(profile, workspaces):
    return {"profile": tree_digest(profile), "workspaces": tree_digest(workspaces)}


def snapshot(candidate, old, new, pointer):
    from verify_copy import rehearse
    folder = Path(tempfile.mkdtemp(dir=RECOVERY, prefix="recovery-"))
    print("Preparing recovery copy at", folder, flush=True)
    clone(APP, folder / "OpenBot.app")
    for src, name in ((PROFILE, "Profile"), (WORKSPACES, "Workspaces")):
        if os.path.lexists(src):
            require(src.is_dir() and not src.is_symlink(), "Unexpected source root.")
            clone(src, folder / name)
    remembered = data_hashes(folder / "Profile", folder / "Workspaces")
    require(remembered == data_hashes(PROFILE, WORKSPACES) and signing(folder / "OpenBot.app") == old,
            "The recovery copy differs from the installed data or app.")
    database_check(folder / "Profile", folder)
    stopped()
    qa = rehearse(candidate, folder)
    stopped()
    require(signing(candidate, True) == new and data_hashes(PROFILE, WORKSPACES) == remembered,
            "The package or live data changed during copied-profile QA.")
    record = {"directory": folder.name, "old": old, "new": new, "data": remembered, "qa": qa}
    atomic_json(folder / "verified.json", record)
    atomic_json(pointer, record)
    return record


def verify_record(record, old, new, live=True):
    require(record["old"] == old and record["new"] == new, "The prepared app changed.")
    name = record["directory"]
    require(re.fullmatch(r"recovery-[\w-]+", name), "Invalid recovery location.")
    folder = RECOVERY / name
    private_dir(folder)
    require(json.loads((folder / "verified.json").read_text()) == record and
            signing(folder / "OpenBot.app") == old and
            data_hashes(folder / "Profile", folder / "Workspaces") == record["data"],
            "Recovery verification failed.")
    return not live or data_hashes(PROFILE, WORKSPACES) == record["data"]


def exchange(current, staged):
    # renamex_np(RENAME_SWAP) either exchanges both complete bundles or leaves them as they were.
    lib = ctypes.CDLL(None, use_errno=True)
    if lib.renamex_np(os.fsencode(current), os.fsencode(staged), 2) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def open_verified():
    trace = PROFILE / "logs/trace.ndjson"
    initial = trace.stat() if trace.is_file() else None
    run("/usr/bin/open", "-a", APP)
    # Use the same renderer-to-main signal as verify-macos-package.ts, from this launch only.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if trace.is_file() and str(APP / "Contents/MacOS/OpenBot") in processes().values():
            with trace.open("rb") as log:
                now = os.fstat(log.fileno())
                if initial and now.st_ino == initial.st_ino and now.st_size >= initial.st_size:
                    log.seek(initial.st_size)
                if b'"kind":"ipc"' in log.read():
                    return
        time.sleep(0.5)
    raise RuntimeError("Installed app did not confirm renderer startup. Keep the recovery copy.")


def install(candidate, prepare_only):
    new = signing(candidate, True)
    old = signing(APP)
    pointer = RECOVERY / (new["cdhash"] + ".json")
    record = json.loads(pointer.read_text()) if pointer.exists() else None
    started = RECOVERY / (new["cdhash"] + ".started.json")
    if old == new:
        require(record and record["new"] == new, "This build is already installed without a prepared recovery record.")
        verify_record(record, record["old"], new, live=False)
        if not prepare_only:
            if processes():
                require(started.is_file() and json.loads(started.read_text()) == record["directory"],
                        "This installation is running, but its startup check is incomplete. Retry after a normal quit.")
            else:
                open_verified()
                atomic_json(started, record["directory"])
        print("The tested app is installed. Recovery copy:", RECOVERY / record["directory"])
        return
    require(tuple(map(int, new["version"].split("."))) > tuple(map(int, old["version"].split("."))),
            "The candidate must be newer than the installed version.")
    if prepare_only:
        prepare_shutdown()
    stopped()
    if not record or not verify_record(record, old, new):
        record = snapshot(candidate, old, new, pointer)
    if prepare_only:
        print("Backup and copied-profile QA passed. Keep OpenBot closed until installation.")
        return

    staging = Path(tempfile.mkdtemp(dir=APP.parent, prefix=".openbot-update-"))
    staged = staging / "OpenBot.app"
    exchanged = False
    try:
        run("/usr/bin/ditto", candidate, staged)
        require(signing(staged, True) == new, "The staged app differs from the tested candidate.")
        require(verify_record(record, old, new) and signing(APP) == old, "The installation changed after backup.")
        stopped()
        exchange(APP, staged)
        exchanged = True
        # A launch can migrate the profile immediately. Never restore a lone older binary here.
        require(signing(APP, True) == new and signing(staged) == old,
                f"Unexpected bundle after exchange. Keep both apps and the backup. Previous app: {staged}")
    except BaseException:
        # Preserve an unverified displaced app even if another installer raced the final exchange.
        if not exchanged:
            shutil.rmtree(staging)
        raise
    shutil.rmtree(staging)
    open_verified()
    atomic_json(started, record["directory"])
    print("Installed OpenBot", new["version"], "| Recovery copy:", RECOVERY / record["directory"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="Back up, rehearse, and leave the app closed for publication.")
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    require(sys.platform == "darwin" and os.getuid() != 0, "Run as the signed-in Mac user.")
    os.umask(0o077)
    RECOVERY.mkdir(mode=0o700, exist_ok=True)
    private_dir(RECOVERY)
    with (RECOVERY / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install(args.candidate.resolve(strict=True), args.prepare)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        sys.exit(str(error))
