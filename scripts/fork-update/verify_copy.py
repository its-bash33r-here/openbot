"""Run the exact app twice with private copies of the profile and workspaces."""
import filecmp
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading

from install_macos import clone, require


def contains_saved(old, new):
    if isinstance(old, dict):
        return isinstance(new, dict) and all(k in new and contains_saved(v, new[k]) for k, v in old.items())
    if isinstance(old, list):
        return isinstance(new, list) and len(old) == len(new) and all(contains_saved(a, b) for a, b in zip(old, new))
    return type(old) is type(new) and old == new


def saved_message(old, new):
    if contains_saved(old, new):
        return True
    # MailboxStore rebuilds both arrays from the same deliveries on boot. SQLite loads those by
    # creation time and delivery ID. Two deliveries created together can exchange positions.
    def ordered_view(message):
        exchange = message.get("exchange")
        if (message.get("author") != "agent" or message.get("status") != "completed"
                or not isinstance(exchange, dict) or exchange.get("direction") != "incoming"):
            return None
        deliveries = exchange.get("deliveries")
        if not isinstance(deliveries, list) or not all(isinstance(d, dict) and isinstance(d.get("id"), str)
                                                       and isinstance(d.get("recipientAgentId"), str) for d in deliveries):
            return None
        if (exchange.get("recipientAgentIds") != [d["recipientAgentId"] for d in deliveries]
                or len({d["id"] for d in deliveries}) != len(deliveries)):
            return None
        rest = {k: v for k, v in exchange.items() if k not in ("recipientAgentIds", "deliveries")}
        return {**message, "exchange": rest}, sorted(json.dumps(d, sort_keys=True) for d in deliveries)
    left, right = ordered_view(old), ordered_view(new)
    return left is not None and right is not None and left[1] == right[1] and contains_saved(left[0], right[0])


def check_workspaces(before, after):
    # An update may create new files. Existing files, links and directories must survive.
    if not before.exists():
        return 0
    def visit(source, target):
        mode = source.lstat().st_mode
        require(stat.S_IFMT(mode) == stat.S_IFMT(target.lstat().st_mode), "A workspace entry changed type.")
        if stat.S_ISDIR(mode):
            return sum(visit(child, target / child.name) for child in source.iterdir())
        if stat.S_ISLNK(mode):
            require(source.readlink() == target.readlink(), "An existing workspace link changed.")
        else:
            require(stat.S_ISREG(mode) and filecmp.cmp(source, target, shallow=False), "A workspace file changed.")
        return 1
    return visit(before, after)


def check_settings(before, after):
    patterns = ["*-preference*.json", "openbot-setup-v*.json"]
    names = {p.name for pattern in patterns for p in before.glob(pattern)}
    names.update(name for name in [
        "openbot-busy-message-mode-v1.json", "openbot-agent-defaults-v1.json", "openbot-agent-memory-limit-v1.json",
        "openbot-memory-review-v1.json", "openbot-server-timezone-v1.json", "openbot-prompt-cache-ttl-v1.json",
        "openbot-model-visibility-v1.json", "openbot-remote-network-route-v1.json",
    ] if (before / name).exists())
    for name in names:
        path = after / name
        require(path.is_file() and contains_saved(json.loads((before / name).read_text()), json.loads(path.read_text())),
                "An existing OpenBot setting changed.")
    return len(names)


def check_chats(before, after):
    """Old rows must still exist. A migration can add fields to a saved message."""
    require(after.is_file(), "The upgraded profile database is missing.")
    new = sqlite3.connect(str(after))
    # Check a private working copy of the baseline so SQLite never rewrites the recovery shm.
    old = sqlite3.connect(str(before))
    try:
        require(new.execute("PRAGMA quick_check").fetchall() == [("ok",)], "The upgraded database is damaged.")
        counts = {}
        for table, keys, fields in [
            ("projection_agents", ["agent_id"], ["thread_id"]),
            ("projection_threads", ["thread_id"], ["agent_id", "title"]),
            ("projection_thread_messages", ["thread_id", "message_id"],
             ["created_at", "ordinal", "turn_id", "author", "status", "item_type", "message_json"]),
        ]:
            count = 0
            for row in old.execute(f"SELECT {', '.join(keys + fields)} FROM {table}"):
                saved = row[len(keys):]
                current = new.execute(f"SELECT {', '.join(fields)} FROM {table} WHERE " +
                                      " AND ".join(f"{k} = ?" for k in keys), row[:len(keys)]).fetchone()
                require(current is not None, f"The upgrade lost rows from {table}.")
                if fields[-1] == "message_json":
                    require(saved[:-1] == current[:-1] and saved_message(json.loads(saved[-1]), json.loads(current[-1])),
                            "An existing chat message changed.")
                else:
                    require(saved == current, f"Existing data changed in {table}.")
                count += 1
            counts[table] = count
        return counts
    finally:
        old.close()
        new.close()


class LocalAccount(BaseHTTPRequestHandler):
    """Prove the app can read its saved credential. No requests leave the Mac."""
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health/live":
            body = {"status": "ok"}
        elif self.path == "/v1/me" and self.headers.get("Authorization", "").startswith("Bearer "):
            value = self.headers["Authorization"][7:]
            if not value:
                return self.send_error(403)
            digest = hashlib.sha256(value.encode()).digest()
            if self.server.credential_digest not in (None, digest):
                return self.send_error(403)
            self.server.credential_digest = digest
            self.server.saw_session = True
            body = {"id": "copy-check", "email": "copy@example.invalid", "name": None, "avatarUrl": None}
        else:
            return self.send_error(403)
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def isolation(test, bundle, backup, port):
    q = lambda p: json.dumps(str(p))
    # The disposable app can write only to its own home and temporary files. Block provider tools,
    # startup scripts and child servers from running out of the copied profile.
    executable = bundle / "Contents/MacOS/OpenBot"
    # Chromium ignores TMPDIR for the SingletonSocket and requests this Darwin temp prefix.
    darwin_tmp = Path(subprocess.check_output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()).resolve()
    socket_dir = "^" + re.escape(str(darwin_tmp)) + "/(scoped_dir|[.]QC5HNMH72Y[.]app[.]openbot[.]desktop[.])[A-Za-z0-9]+(/|$)"
    rules = ["(version 1)", "(allow default)", '(deny file-write* (subpath "/"))',
             '(allow file-write* (literal "/dev/null") (literal "/dev/zero") (literal "/dev/random"))',
             f"(allow file-write* (subpath {q(test)}) (regex #{q(socket_dir)}))",
             f"(deny file-read* (subpath {q(backup)}) (subpath {q(Path.home() / 'OpenBot')}) (subpath {q(Path.home() / 'Library/Application Support/OpenBot')}))",
             '(deny network-outbound (remote ip "*:*"))', f'(allow network-outbound (remote ip "localhost:{port}"))',
             "(deny process-exec*)", f'(allow process-exec* (literal {q(executable)}) (literal "/bin/zsh"))']
    # Electron's sandboxed helpers install their own seatbelt profiles.
    for suffix in ("", " (GPU)", " (Renderer)"):
        name = "OpenBot Helper" + suffix
        helper = bundle / "Contents/Frameworks" / (name + ".app") / "Contents/MacOS" / name
        rules.append(f"(allow process-exec* (with no-sandbox) (literal {q(helper)}))")
    # Crashpad inherits the read-only policy.
    crashpad = bundle / "Contents/Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler"
    rules.append(f"(allow process-exec* (literal {q(crashpad.resolve())}))")
    path = test / "isolation.sb"
    path.write_text("\n".join(rules) + "\n")
    return path


def rehearse(bundle, backup, *, mock_keychain=False):
    profile = backup / "Profile"
    require((profile / "openbot.db").is_file(), "Copied-profile QA requires an initialized OpenBot profile.")
    signed_in = (profile / "openbot-central-auth-v1.bin").exists()
    # Chromium creates a Unix socket below TMPDIR. Keep its path below the macOS limit.
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="ob-qa-") as work:
        test = Path(work).resolve()
        home = test / "home"
        (home / "Library").mkdir(parents=True)
        (test / "tmp").mkdir()
        # The existing login Keychain alone can decrypt an existing Electron safeStorage session.
        if not mock_keychain:
            (home / "Library/Keychains").symlink_to(Path.home() / "Library/Keychains")
        clone(profile, test / "profile")
        if (backup / "Workspaces").exists():
            clone(backup / "Workspaces", home / "OpenBot")
        baseline = test / "baseline.db"
        for suffix in ("", "-wal", "-journal"):
            source = profile / ("openbot.db" + suffix)
            if source.exists():
                clone(source, Path(str(baseline) + suffix))

        with ThreadingHTTPServer(("127.0.0.1", 0), LocalAccount) as server:
            server.credential_digest = None
            server.saw_session = False
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_port
                policy = isolation(test, bundle, backup, port)
                bun = shutil.which("bun")
                require(bun, "Copied-profile QA needs the project's Bun runtime.")
                env = {"HOME": str(home), "CFFIXED_USER_HOME": str(home), "TMPDIR": str(test / "tmp") + "/",
                       "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "ZDOTDIR": str(home),
                       "OPENBOT_AUTH_API_URL": f"http://127.0.0.1:{port}"}
                starts = []
                for _ in range(2):
                    server.saw_session = False
                    args = ["/usr/bin/sandbox-exec", "-f", policy, bundle / "Contents/MacOS/OpenBot",
                            "--user-data-dir=" + str(test / "profile"), "--proxy-server=http://127.0.0.1:1",
                            "--disable-background-networking", "--remote-debugging-address=127.0.0.1",
                            "--remote-debugging-port=0"]
                    if mock_keychain:
                        args.append("--use-mock-keychain")
                    child = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL,
                                             stderr=None if mock_keychain else subprocess.DEVNULL)
                    try:
                        first_load = subprocess.run([bun, Path(__file__).with_name("probe-copy.mjs"), str(child.pid),
                                        test / "profile", "signed_in" if signed_in else "signed_out"],
                                       timeout=100, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                        observed = json.loads(first_load.stdout or b"{}")
                        if first_load.returncode:
                            step = observed.get("waitingFor", "window")
                            require(step in ("window", "session", "workspace", "navigation", "probe"), "Invalid QA result.")
                            raise RuntimeError("Copied-profile startup did not pass the first-load check: " + step)
                        starts.append(observed)
                        child.wait(timeout=45)
                        require(child.returncode == 0 and server.saw_session == signed_in,
                                "Copied-profile session QA failed.")
                    finally:
                        if child.poll() is None:
                            child.terminate()
                            child.wait(timeout=40)
                    counts = check_chats(baseline, test / "profile/openbot.db")
                    counts["settingsFiles"] = check_settings(profile, test / "profile")
                    counts["workspaceFiles"] = check_workspaces(backup / "Workspaces", home / "OpenBot")
            finally:
                server.shutdown()
    return {"launches": len(starts), "firstLoads": starts, "storedSession": signed_in, "preserved": counts}
