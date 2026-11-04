"""Exercise recovery verification, atomic exchange, and (when given a QA app) a synthetic profile."""
from contextlib import closing
import json
import os
import select
import socket
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from install_macos import atomic_json, clone, database_check, exchange, require, tree_digest
from verify_copy import check_chats, rehearse

def check_delivery_order(db, work):
    from copy import deepcopy
    recipients = ["first", "second"]
    message = {"author": "agent", "status": "completed", "text": "saved text", "attachments": [{"id": "file"}],
               "exchange": {"direction": "incoming", "recipientAgentIds": recipients,
                            "deliveries": [{"id": str(i), "recipientAgentId": r, "position": None, "error": None}
                                           for i, r in enumerate(recipients)]}}
    previous, current = work / "previous.db", work / "current.db"
    for file in (previous, current):
        with sqlite3.connect(file) as copy:
            db.backup(copy)
            copy.execute("UPDATE projection_thread_messages SET author='agent', message_json=?", (json.dumps(message),))
    reordered = deepcopy(message)
    for key in ("recipientAgentIds", "deliveries"):
        reordered["exchange"][key].reverse()
    with sqlite3.connect(current) as copy:
        copy.execute("UPDATE projection_thread_messages SET message_json=?", (json.dumps(reordered),))
        copy.commit()
        check_chats(previous, current)
        # Changes to content, delivery state, reference order, timestamps or chronological order must fail.
        broken = deepcopy(reordered)
        broken["exchange"]["deliveries"][0]["error"] = "new error"
        for sql, value in [
            ("message_json", json.dumps({**reordered, "text": "other"})),
            ("message_json", json.dumps({**reordered, "attachments": []})),
            ("message_json", json.dumps(broken)),
            ("message_json", json.dumps({**reordered, "exchange": {**reordered["exchange"], "recipientAgentIds": recipients}})),
            ("created_at", "2030"), ("ordinal", 1), ("author", "system"),
        ]:
            copy.execute(f"UPDATE projection_thread_messages SET {sql} = ?", (value,))
            try:
                # Compare against an independent SQLite file that contains the active change.
                copy.commit()
                check_chats(previous, current)
            except RuntimeError:
                pass
            else:
                raise RuntimeError("A changed message passed the recovery check.")
            copy.execute("UPDATE projection_thread_messages SET created_at='2020', ordinal=0, author='agent', message_json=?",
                         (json.dumps(reordered),))
            copy.commit()


def check_crashpad_handoff(work):
    # Start Electron with its own real Crashpad database. Keep a detached updater child alive across quit.
    entry = work / "crash-owner.cjs"
    entry.write_text("""
const {app, crashReporter} = require("electron");
const {spawn} = require("node:child_process");
const path = require("node:path");
app.dock.hide();
app.setPath("crashDumps", path.join(__dirname, "crashes"));
crashReporter.start({uploadToServer: false});
app.whenReady().then(() => {
  const child = spawn(process.env.FIXTURE_PYTHON, ["-I", "-c", process.env.FIXTURE_SCRIPT], {
    detached: true, stdio: ["ignore", "ignore", "ignore", Number(process.env.FIXTURE_FD)],
    env: {...process.env, OPENBOT_FORK_UPDATE_PID: String(process.pid)}
  });
  child.unref();
  process.stdin.once("data", () => app.quit());
});
""")
    child_script = (
        "import os,socket,sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
        "import update\n"
        "s=socket.socket(fileno=3);s.settimeout(30);s.sendall(b'R')\n"
        "assert s.recv(1)==b'D'; sys.argv[:]=['update']; assert update.main()==1; s.sendall(b'D')\n"
        "assert s.recv(1)==b'Q'\n"
    )
    binary = root / "node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
    # The invalid seed stops the real runner after startup, before a fetch, push, build or install.
    private_home = work / "crash-test-home"
    owner_folder = private_home / "Library/Application Support/OpenBot Fork Updates"
    owner_folder.mkdir(parents=True, mode=0o700)
    atomic_json(owner_folder / "config.json", {"path": ["/usr/bin", "/bin"], "seed": "invalid-fixture-seed"})
    ours, inherited = socket.socketpair()
    with ours, inherited, closing(select.kqueue()) as exits:
        ours.settimeout(20)
        env = dict(os.environ, HOME=str(private_home), FIXTURE_PYTHON=sys.executable, FIXTURE_SCRIPT=child_script,
                   FIXTURE_FD=str(inherited.fileno()))
        with subprocess.Popen([binary, f"--user-data-dir={work / 'crash-fixture-profile'}", entry], env=env,
                              pass_fds=(inherited.fileno(),), stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as owner:
            try:
                require(ours.recv(1) == b"R", "Detached fixture did not start.")
                rows = subprocess.check_output(["/bin/ps", "-axww", "-o", "pid=,args="], text=True).splitlines()
                pids = [int(row.split()[0]) for row in rows
                        if "chrome_crashpad_handler " in row and f"--database={work / 'crashes'} " in row]
                require(len(pids) == 1, "Expected exactly one reporter for the disposable Electron app.")
                watch = select.kevent(pids[0], filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD,
                                      fflags=select.KQ_NOTE_EXIT)
                exits.control([watch], 0, 0)
                owner.stdin.write(b"Q"); owner.stdin.flush()
                owner.wait(timeout=15)
                require(not exits.control(None, 1, 0), "The detached runner did not retain the old crash reporter.")
                ours.sendall(b"D")
                require(ours.recv(1) == b"D", "The updater could not release its inherited handlers.")
                require(exits.control(None, 1, 10), "The old reporter still blocks the upgrade.")
                ours.sendall(b"Q")
                require(json.loads((owner_folder / "status.json").read_text())["failedStep"] == "sync",
                        "The disposable updater did not reach the source step.")
            finally:
                if owner.poll() is None:
                    owner.terminate()


def check_failure_report(work):
    # A malformed candidate exercises the actual installer exit code, runner mapping and on-disk receipt.
    private_home = work / "diagnostic-test"
    support = private_home / "Library/Application Support"
    support.mkdir(parents=True)
    runner = support / "OpenBot Fork Updates"
    runner.mkdir(mode=0o700)
    isolated = """
import json,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import update as u
u.state("installing")
try:
    u.command(sys.executable, Path(sys.argv[1])/"install_macos.py", "--prepare", sys.argv[2], local=True)
except u.Stop as error:
    u.failed(error, error.phase, error.failed_step)
else:
    raise RuntimeError("A malformed candidate passed the installer.")
"""
    scripts = str(Path(__file__).resolve().parent)
    malformed = work / "private-name.app"
    malformed.mkdir()
    subprocess.run([sys.executable, "-I", "-c", isolated, scripts, str(malformed)],
                   env=dict(os.environ, HOME=str(private_home)), check=True)
    receipt = json.loads((runner / "status.json").read_text())
    require(receipt["phase"] == "failed" and receipt["failedStep"] == "sign", "The signature failure changed steps.")
    require("private-name" not in receipt["reason"] and "python" not in receipt["reason"], "Raw failure leaked.")


root = Path(__file__).resolve().parents[2]
out = root / ".openbot-build/fork-update-install-e2e"
out.mkdir(parents=True, exist_ok=True)
report = {"passed": False}
try:
    with tempfile.TemporaryDirectory(dir=out) as folder:
        work = Path(folder)
        check_crashpad_handoff(work)
        report["oldReporterExitedWithRunnerAlive"] = True
        check_failure_report(work)
        report["safeInstallerFailureStep"] = True
        src, backup = work / "Profile", work / "backup"
        src.mkdir()
        (src / "marker").write_bytes(b"private text")
        (src / "link").symlink_to("marker")
        with sqlite3.connect(src / "openbot.db") as db:
            db.execute("CREATE TABLE fixture (value)")
            db.execute("INSERT INTO fixture VALUES ('private chat')")
        clone(src, backup)
        require(tree_digest(src) == tree_digest(backup), "Clone differs.")
        database_check(backup, work)
        (backup / "marker").write_bytes(b"different")
        require(tree_digest(src) != tree_digest(backup), "Corruption was missed.")
        exchange(src, backup)
        require((src / "marker").read_bytes() == b"different" and (backup / "marker").read_bytes() == b"private text",
                "Atomic exchange failed.")
        report["backupAndExchange"] = True
        if len(sys.argv) > 1:
            snapshot = work / "synthetic"
            profile = snapshot / "Profile"
            profile.mkdir(parents=True)
            (snapshot / "Workspaces/Shared").mkdir(parents=True)
            (snapshot / "Workspaces/Shared/user-note").write_text("Preserved workspace content")
            subprocess.run(["bun", "-e", """
                import {Effect} from 'effect';
                import {OpenBotDatabase} from './src/backend/openbot-database';
                const db = new OpenBotDatabase(process.env.FIXTURE_PROFILE);
                await Effect.runPromise(db.initialize()); db.close();
            """], cwd=root, env=dict(os.environ, FIXTURE_PROFILE=str(profile)), check=True)
            with sqlite3.connect(profile / "openbot.db") as db:
                db.execute("INSERT INTO projection_threads VALUES ('fixture-thread','fixture-agent','Sample',NULL,'2020','2020',0)")
                db.execute("INSERT INTO projection_thread_messages(thread_id,message_id,turn_id,author,status,item_type,created_at,ordinal,message_json,last_event_sequence) VALUES ('fixture-thread','fixture-message',NULL,"
                           "'user','completed','text','2020',0,?,0)", (json.dumps({"text": "saved history"}),))
            check_delivery_order(db, work)
            report["messageOrderAndContent"] = True
            (profile / "openbot-appearance-preference-v1.json").write_text('{"version":1,"appearance":"dark"}')
            report["sample"] = rehearse(Path(sys.argv[1]).resolve(strict=True), snapshot, mock_keychain=True)
    report["passed"] = True
finally:
    atomic_json(out / "report.json", report)
print(json.dumps(report))
