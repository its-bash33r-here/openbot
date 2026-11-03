"""Resolve Git index conflicts in the private candidate through the owner app."""
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid


class MergePaused(Exception):
    pass


# Use only complete diff3 hunks. Leave every byte outside each hunk as Git wrote it.
HUNK = re.compile(rb"^<<<<<<< [^\n]*\n(.*?)^\|\|\|\|\|\|\| [^\n]*\n(.*?)^=======\r?\n(.*?)^>>>>>>> [^\n]*(?:\n|\Z)", re.M | re.S)
MARKER = re.compile(rb"^(?:<{7}|>{7}|\|{7}|={7})(?: |\r?$)", re.M)


def ask(run, nonce, payload):
    request = {**payload, "nonce": nonce, "id": str(uuid.uuid4())}
    if len(json.dumps(request, ensure_ascii=False).encode()) > 8000:
        raise MergePaused("The merge request exceeds the app limit.")
    with tempfile.NamedTemporaryFile(mode="w", dir=run, delete=False) as f:
        json.dump(request, f, ensure_ascii=False)
    request_path = run / "merge-request.json"
    os.replace(f.name, request_path)
    response_path = run / "merge-response.json"
    try:
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            try:
                with response_path.open("rb") as f:
                    answer = json.loads(f.read(32769))
            except FileNotFoundError:
                answer = {}
            if answer.get("nonce") == nonce and answer.get("id") == request["id"]:
                if answer.get("action") == "later":
                    raise MergePaused("The merge is paused. Click Update this Mac to try again.")
                return answer
            try:
                os.kill(int(os.environ["OPENBOT_FORK_UPDATE_PID"]), 0)
            except ProcessLookupError:
                break
            time.sleep(0.35)
        raise MergePaused("The owner window did not answer. Reopen OpenBot and retry the update.")
    finally:
        request_path.unlink(missing_ok=True)
        response_path.unlink(missing_ok=True)


def choice_request(run, nonce, path, label, stages, hunk=None):
    sides = {str(k): v[1][:10] for k, v in stages.items()}
    return ask(run, nonce, {"path": path, "merge": label, "sides": sides, "hunk": hunk})


def resolve(work, git, entries, label):
    run = Path(os.environ["OPENBOT_FORK_UPDATE_RUN"])
    nonce = os.environ["OPENBOT_FORK_UPDATE_NONCE"]
    files = {}
    for row in entries.rstrip("\0").split("\0"):
        meta, path = row.split("\t", 1)
        mode, oid, stage = meta.split()
        if (Path(path).is_absolute() or ".." in Path(path).parts or
                any(ord(ch) < 32 or ord(ch) == 127 for ch in path) or len(path) > 1024):
            raise MergePaused("Git reported an unsupported conflict path.")
        files.setdefault(path, {})[int(stage)] = (mode, oid)
    for path, stages in files.items():
        target = work / path
        if target.parent.resolve() != target.parent:
            raise MergePaused("Git reported a conflict below a symbolic link.")
        # Binary files, structural edits and files without a complete diff3 representation use an owner choice.
        if ({2, 3} <= stages.keys() and len({mode for mode, _ in stages.values()}) == 1 and
                stages[2][0] in ("100644", "100755") and not target.is_symlink() and
                target.is_file() and target.stat().st_size < 2_000_000):
            data = target.read_bytes()
            matches = list(HUNK.finditer(data))
            if matches and len(MARKER.findall(data)) == 4 * len(matches) and b"\0" not in data:
                result = bytearray()
                start = 0
                for m in matches:
                    raw = {"current": m[1], "base": m[2], "incoming": m[3]}
                    try:
                        hunk = {k: v.decode("utf8") for k, v in raw.items()}
                    except UnicodeDecodeError:
                        break
                    if len(json.dumps(hunk, ensure_ascii=False).encode()) > 5600:
                        break
                    answer = choice_request(run, nonce, path, label, stages, hunk)
                    action = answer.get("action")
                    if action in ("current", "incoming"):
                        merged = raw[action]
                    elif action == "merged" and isinstance(answer.get("text"), str):
                        merged = answer["text"].encode()
                    else:
                        raise MergePaused("The app returned an invalid merge choice.")
                    if len(merged) > 16000 or b"\0" in merged or MARKER.search(merged):
                        raise MergePaused("The app returned invalid conflict text.")
                    if merged and not merged.endswith(b"\n") and m.end() < len(data):
                        raise MergePaused("The merge would join two separate lines.")
                    result.extend(data[start:m.start()] + merged)
                    start = m.end()
                else:
                    target.write_bytes(result + data[start:])
                    git(work, "add", "--", path)
                    continue
        answer = choice_request(run, nonce, path, label, stages)
        stage = {"current": 2, "incoming": 3}.get(answer.get("action"))
        if stage is None:
            raise MergePaused("The app did not select a file version.")
        if stage not in stages:
            git(work, "rm", "-f", "--", path)
        else:
            mode, oid = stages[stage]
            git(work, "update-index", "--add", "--cacheinfo", mode, oid, path)
            if mode != "160000":
                git(work, "checkout-index", "--force", "--", path)
