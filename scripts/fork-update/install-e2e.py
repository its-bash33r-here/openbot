"""Exercise recovery verification, atomic exchange, and (when given a QA app) a synthetic profile."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from install_macos import atomic_json, clone, database_check, exchange, require, tree_digest
from verify_copy import rehearse

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
            (profile / "openbot-appearance-preference-v1.json").write_text('{"version":1,"appearance":"dark"}')
            report["sample"] = rehearse(Path(sys.argv[1]).resolve(strict=True), snapshot, mock_keychain=True)
    report["passed"] = True
finally:
    atomic_json(out / "report.json", report)
print(json.dumps(report))
