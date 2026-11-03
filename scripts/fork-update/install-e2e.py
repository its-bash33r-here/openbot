"""Exercise recovery verification, atomic exchange, and (when given a QA app) a synthetic profile."""
import json
import os
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


root = Path(__file__).resolve().parents[2]
out = root / ".openbot-build/fork-update-install-e2e"
out.mkdir(parents=True, exist_ok=True)
report = {"passed": False}
try:
    with tempfile.TemporaryDirectory(dir=out) as folder:
        work = Path(folder)
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
