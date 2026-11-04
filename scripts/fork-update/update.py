"""Run one owner-initiated fork release. No network access in --status."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

# The installed owner command uses isolated Python. Load its pinned sibling only.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_assist

ROOT = Path.home() / "Library/Application Support/OpenBot Fork Updates"
FORK = "its-bash33r-here/openbot"
UPSTREAM = "nightly-labs/openbot"
BRANCH = "fork/mac-release"
APP = Path("/Applications/OpenBot.app")
SHA = r"[0-9a-f]{40}"
VERSION = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
ACTIVE = {"syncing", "checking", "signing", "publishing", "installing"}
ASSETS = ("OpenBot-{v}-arm64.zip", "OpenBot-{v}-arm64.dmg", "OpenBot-{v}-arm64.zip.blockmap",
          "OpenBot-{v}-arm64.dmg.blockmap", "latest-mac.yml", "SHA256SUMS-macos.txt")
QUIT_ENV = ("OPENBOT_FORK_UPDATE_RUN", "OPENBOT_FORK_UPDATE_PID", "OPENBOT_FORK_UPDATE_NONCE")


class Stop(Exception):
    def __init__(self, phase, reason):
        super().__init__(reason)
        self.phase = phase


def save(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, path)


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def state(phase, reason=""):
    save(ROOT / "status.json", {"phase": phase, "reason": reason})


def require(ok, message):
    if not ok:
        raise Stop("failed", message)


def checked(text, pattern, kind):
    require(isinstance(text, str) and re.fullmatch(pattern, text), "Invalid " + kind)
    return text


def command(*args, cwd=None, timeout=600, check=True, local=False):
    # Only the installer receives the per-click quit capability. Never print child output or arguments.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LOGNAME") if k in os.environ}
    env.update(LANG="en_US.UTF-8", GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
    if local:
        env.update({k: os.environ[k] for k in QUIT_ENV if k in os.environ})
    # git and gh are bounded text queries. Stream larger build/check output straight to /dev/null.
    capture = args[0] in ("git", "gh")
    p = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout)
    if check:
        require(p.returncode == 0, "Command failed: " + Path(args[0]).name + (" " + args[1] if capture else ""))
    return (p.stdout or "").strip() if check else p.returncode


def git(repo, *args, **kw):
    return command("git", *args, cwd=repo, **kw)


def gh(*args):
    return command("gh", *args)


def api(endpoint):
    return json.loads(gh("api", endpoint))


def repo_url(name):
    return "https://github.com/" + name + ".git"


def remote_ref(repo, ref):
    rows = git(ROOT, "ls-remote", "--refs", repo_url(repo), ref)
    if not rows:
        return None
    sha, found = rows.split()
    require(found == ref, "Unexpected remote ref")
    return checked(sha, SHA, "remote revision")


def semver(value):
    return tuple(map(int, checked(value, VERSION, "stable version").split(".")))


def installed():
    info = plistlib.loads((APP / "Contents/Info.plist").read_bytes())
    require(info["CFBundleIdentifier"] == "app.openbot.desktop", "Unexpected installed app")
    return info["CFBundleShortVersionString"]


def merge(work, ref, label, probe):
    if git(work, "merge-base", "--is-ancestor", ref, "HEAD", check=False) == 0:
        return False
    if not git(work, "-c", "merge.conflictStyle=diff3", "merge", "--no-edit", ref, check=False):
        return False
    entries = git(work, "ls-files", "--unmerged", "-z")
    if not entries:
        raise Stop("failed", "Git could not merge " + label + ".")
    if probe or os.environ.get("OPENBOT_FORK_UPDATE_MERGE") != "1":
        paths = [row.split("\t", 1)[-1] for row in entries.rstrip("\0").split("\0")]
        raise Stop("conflict", label + " conflicts in " + ", ".join(dict.fromkeys(paths))[:400] + ".")
    try:
        merge_assist.resolve(work, git, entries, label)
    except merge_assist.MergePaused as error:
        raise Stop("conflict", str(error)) from error
    require(not git(work, "ls-files", "--unmerged") and
            not git(work, "diff", "--cached", "--check", check=False), "The resolved merge did not pass Git checks.")
    command("bun", "install", "--frozen-lockfile", cwd=work)
    git(work, "commit", "--no-edit")
    return True


def candidate(config, probe=False):
    cache = ROOT / "cache.git"
    if not cache.exists():
        command("git", "init", "--bare", cache)
    for key, value in (("user.name", "Syed Basheer Ahamed"), ("user.email", "45915098+its-bash33r-here@users.noreply.github.com"),
                       ("core.hooksPath", ".githooks")):
        git(cache, "config", key, value)
    seed = checked(config["seed"], SHA, "configured source")
    if git(cache, "cat-file", "-e", seed + "^{commit}", check=False):
        git(cache, "fetch", "--no-tags", config["source"], seed + ":refs/pinned/" + seed)
    for url, ref, dest in ((repo_url(FORK), "refs/heads/main", "fork"),
                           (repo_url(UPSTREAM), "refs/heads/main", "upstream")):
        git(cache, "fetch", "--no-tags", url, "+" + ref + ":refs/input/" + dest)
    up = git(cache, "rev-parse", "refs/input/upstream^{commit}")
    fork = git(cache, "rev-parse", "refs/input/fork^{commit}")
    tip = remote_ref(FORK, "refs/heads/" + BRANCH)
    last = read(ROOT / "completed.json")
    if tip:
        git(cache, "fetch", "--no-tags", repo_url(FORK), tip)
        base = checked(git(cache, "show", "-s", "--format=%(trailers:key=Fork-Source,valueonly)", tip), SHA, "fork release source")
        require(git(cache, "merge-base", "--is-ancestor", base, tip, check=False) == 0, "Invalid release history")
        # Only generated release metadata may be omitted when carrying forward the clean source.
        require(git(cache, "diff", "--quiet", base, tip, "--", ".", ":(exclude)CHANGELOG.md",
                    ":(exclude)changelog.d/**", ":(exclude)package.json", check=False) == 0,
                "The release branch contains additional code. Merge those changes into fork main first")
        manifest = json.loads(git(cache, "show", tip + ":package.json"))
        previous_manifest = json.loads(git(cache, "show", base + ":package.json"))
        manifest.pop("version")
        previous_manifest.pop("version")
        require(manifest == previous_manifest, "The release branch contains unpublished package changes")
    else:
        base = seed
    batch = Path(tempfile.mkdtemp(dir=ROOT, prefix="candidate-"))
    work = batch / "source"
    git(cache, "worktree", "add", "--detach", work, fork)
    keep = False
    try:
        # Reproduce GitHub's Update branch on the fork. Release-only changes stay on the Mac branch.
        # The pinned owner source can already contain a reviewed merge of these exact two heads.
        # Keep its resolution private until the final candidate passes CI and copied-profile checks.
        merges = git(work, "rev-list", "--merges", "--parents", seed, "--not", fork, up).splitlines()
        reviewed = next((row.split()[0] for row in merges if row.split()[1:] == [fork, up]), None)
        if reviewed:
            git(work, "merge", "--ff-only", reviewed)
            assisted = True
        else:
            assisted = merge(work, up, "upstream main and fork main", probe)
        synced = git(work, "rev-parse", "HEAD")
        git(work, "switch", "--detach", base)
        assisted = merge(work, seed, "the saved Mac changes", probe) or assisted
        assisted = merge(work, synced, "the Mac changes and synced fork", probe) or assisted
        source = git(work, "rev-parse", "HEAD")
        if probe:
            state("idle", "Fork main and upstream main " + up[:12] + " merge with this Mac's changes.")
            return None
        if synced != fork and not assisted:
            # A concurrent push to main must survive. A normal push refuses a non-fast-forward.
            if git(work, "push", repo_url(FORK), synced + ":refs/heads/main", check=False):
                raise Stop("failed", "Could not sync fork main. Check Git push access or retry with the new branch tip.")
        if tip and last and tip == last["commit"] and base == source and installed() == last["version"]:
            state("upToDate")
            return None
        if tip and base == source:
            raise Stop("held", "An existing fork release needs recovery on this Mac.")
        state("checking")
        command("bun", "install", "--frozen-lockfile", cwd=work)
        if tip:
            git(work, "merge", "--no-ff", "-s", "ours", "--no-edit", tip)
            history = git(work, "show", tip + ":CHANGELOG.md")
            changelog = work / "CHANGELOG.md"
            current = changelog.read_text()
            sections = re.split(r"(?=^## \[)", history, flags=re.M)
            fork_sections = [section for section in sections if re.match(r"## \[\d+", section) and
                             section.split("]", 1)[0] + "]" not in current]
            first = re.search(r"^## \[\d", current, re.M)
            if first and fork_sections:
                changelog.write_text(current[:first.start()] + "".join(s.rstrip() + "\n\n" for s in fork_sections) + current[first.start():])
            for old in git(work, "diff", "--diff-filter=D", "--name-only", base, tip, "--", "changelog.d").splitlines():
                if (work / old).is_file() and not git(work, "diff", "--quiet", base, source, "--", old, check=False):
                    (work / old).unlink()
        package = work / "package.json"
        versions = [json.loads(package.read_text())["version"], installed()]
        if tip:
            versions.append(json.loads(git(work, "show", tip + ":package.json"))["version"])
        baseline = max(versions, key=semver)
        package.write_text(re.sub(r'("version"\s*:\s*")[^"]+', lambda m: m[1] + baseline, package.read_text(), count=1))
        fragment = work / "changelog.d/fork-macos-sync.md"
        fragment.write_text("### Changed\n\n- Update the Mac app with the changes from upstream main at " + up[:12] + ".\n")
        command("bun", "scripts/prepare-release.ts", "patch", cwd=work)
        version = checked(json.loads(package.read_text())["version"], VERSION, "new version")
        git(work, "add", "--", "package.json", "CHANGELOG.md", "changelog.d")
        git(work, "commit", "-m", "Release OpenBot for Mac " + version, "-m", "Fork-Source: " + source)
        tx = {"commit": git(work, "rev-parse", "HEAD"), "source": source, "upstream": up,
              "version": version, "batch": batch.name, "previous": tip, "seed": seed,
              "forkMain": synced, "priorMain": fork if assisted else synced}
        save(ROOT / "pending.json", tx)
        keep = True
        return tx
    finally:
        # Remove only this run's temporary checkout. Retain interrupted release transactions.
        if not keep:
            git(cache, "worktree", "remove", "--force", work)
            batch.rmdir()


def persist(tx, **fields):
    tx.update(fields)
    save(ROOT / "pending.json", tx)


def ci_branch_name(commit):
    return "fork/mac-ci/" + checked(commit, SHA, "CI revision")


def cleanup_completed_ci(receipt):
    if not receipt:
        return []
    # Older receipts record the installed commit but have no cleanup field.
    pending = receipt.get("ciCleanupPending", [receipt["commit"]])
    remaining = []
    for commit in pending:
        ref = "refs/heads/" + ci_branch_name(commit)
        try:
            if remote_ref(FORK, ref) == commit:
                # The lease also protects a branch moved between the lookup and the deletion.
                git(ROOT / "cache.git", "push", "--force-with-lease=" + ref + ":" + commit,
                    repo_url(FORK), ":" + ref, timeout=30)
        except (Stop, OSError, subprocess.SubprocessError):
            remaining.append(commit)
    if receipt.get("ciCleanupPending") != remaining:
        save(ROOT / "completed.json", {**receipt, "ciCleanupPending": remaining})
    return remaining


def hosted_checks(tx, work):
    commit = tx["commit"]
    ci_branch = ci_branch_name(commit)
    target = "refs/heads/" + ci_branch
    remote = remote_ref(FORK, target)
    if remote != commit:
        require(remote is None, "The candidate branch points to another commit.")
        git(work, "push", repo_url(FORK), commit + ":" + target)
    def runs():
        return json.loads(gh("run", "list", "-R", FORK, "--workflow", "ci.yml", "--branch", ci_branch, "--commit",
                             commit, "--event", "workflow_dispatch", "--json", "databaseId,headSha,status,conclusion"))
    deadline = time.monotonic() + 7200
    if not runs():
        gh("workflow", "run", "ci.yml", "-R", FORK, "--ref", ci_branch)
    retry_attempt = None
    while time.monotonic() < deadline:
        found = [r for r in runs() if r["headSha"] == commit]
        if found:
            run = found[0]
            if run["status"] == "completed":
                if run["conclusion"] == "success":
                    result = json.loads(gh("run", "view", str(run["databaseId"]), "-R", FORK,
                                           "--json", "headSha,conclusion,jobs"))
                    passed = {job["name"] for job in result["jobs"] if job["conclusion"] == "success"}
                    required = {"Check", "Tests (desktop 1/2)", "Tests (desktop 2/2)", "Browser smoke",
                                "Signal E2E", "Team API and reverse actions E2E"}
                    require(result["headSha"] == commit and result["conclusion"] == "success" and required <= passed,
                            "Required CI jobs did not pass for this commit")
                    return
                if retry_attempt is not None:
                    attempt = int(gh("run", "view", str(run["databaseId"]), "-R", FORK, "--json", "attempt", "--jq", ".attempt"))
                    if attempt > retry_attempt:
                        raise Stop("failed", "The retried hosted checks failed.")
                    time.sleep(10)
                    continue
                if tx.get("failedRun") == run["databaseId"]:
                    retry_attempt = int(gh("run", "view", str(run["databaseId"]), "-R", FORK, "--json", "attempt", "--jq", ".attempt"))
                    gh("run", "rerun", str(run["databaseId"]), "--failed", "-R", FORK)
                else:
                    persist(tx, failedRun=run["databaseId"])
                    raise Stop("failed", "Hosted checks failed. Inspect the CI run before retrying.")
        time.sleep(10)
    raise Stop("failed", "Hosted checks timed out. Click Update to resume.")


def assets(directory, version):
    result = {}
    for pattern in ASSETS:
        name = pattern.format(v=version)
        path = directory / name
        require(path.is_file() and not path.is_symlink(), "Missing release file")
        with path.open("rb") as f:
            result[name] = "sha256:" + hashlib.file_digest(f, "sha256").hexdigest()
    manifest = (directory / "SHA256SUMS-macos.txt").read_text().splitlines()
    expected = {value[7:] + "  " + name for name, value in result.items() if name != "SHA256SUMS-macos.txt"}
    require(len(manifest) == len(expected) and set(manifest) == expected, "Staged release file changed")
    return result


def public_manifest(digest):
    url = "https://github.com/" + FORK + "/releases/latest/download/latest-mac.yml"
    with urllib.request.urlopen(url, timeout=45) as reply:
        data = reply.read(65537)
    require(len(data) <= 65536 and "sha256:" + hashlib.sha256(data).hexdigest() == digest,
            "The public Mac update feed does not match the signed package")


def publish(tx, work, directory, digests):
    version = tx["version"]
    branch = "refs/heads/" + BRANCH
    if tx.get("priorMain", tx["forkMain"]) != tx["forkMain"]:
        current = remote_ref(FORK, "refs/heads/main")
        if current == tx["priorMain"]:
            git(work, "push", repo_url(FORK), tx["forkMain"] + ":refs/heads/main")
        elif current != tx["forkMain"]:
            # Resume a published transaction if main has since advanced and still contains the resolution.
            git(work, "fetch", "--no-tags", repo_url(FORK), "refs/heads/main")
            require(not git(work, "merge-base", "--is-ancestor", tx["forkMain"], "FETCH_HEAD", check=False),
                    "Fork main changed before publication. Retry with its new changes.")
    previous = remote_ref(FORK, branch)
    require(previous in (tx["previous"], tx["commit"]), "The release branch changed before publication")
    if previous != tx["commit"]:
        git(work, "push", repo_url(FORK), tx["commit"] + ":" + branch)
    ref = "refs/tags/" + version
    tag = remote_ref(FORK, ref)
    if not tag:
        git(work, "push", repo_url(FORK), tx["commit"] + ":" + ref)
    require(remote_ref(FORK, ref) == tx["commit"], "Release tag already names different code")
    # The authenticated CLI sees drafts. Only a confirmed absence permits creating a new draft.
    def info():
        return json.loads(gh("release", "view", version, "-R", FORK, "--json", "isDraft,isPrerelease,assets"))
    if command("gh", "release", "view", version, "-R", FORK, check=False):
        notes = directory.parent / "notes.md"
        notes.write_text("OpenBot " + version + " for Apple silicon.\n\nUpstream source commit: " + tx["upstream"] + ".\n")
        gh("release", "create", version, "-R", FORK, "--draft", "--verify-tag", "--title", "OpenBot " + version, "--notes-file", notes)
    record = info()
    require(not record["isPrerelease"], "Unexpected release channel")
    present = {a["name"]: a["digest"] for a in record["assets"]}
    require(all(digests.get(k) == v for k, v in present.items()), "An uploaded asset differs from the signed files")
    for name in digests.keys() - present.keys():
        require(record["isDraft"], "An existing public release is incomplete")
        gh("release", "upload", version, str(directory / name), "-R", FORK)
    require({a["name"]: a["digest"] for a in info()["assets"]} == digests, "Uploaded release hashes differ")
    if record["isDraft"]:
        gh("release", "edit", version, "-R", FORK, "--draft=false", "--latest")
    public = api("repos/" + FORK + "/releases/latest")
    require(public["tag_name"] == version and not public["draft"] and not public["prerelease"] and
            {a["name"]: a["digest"] for a in public["assets"]} == digests, "The stable feed does not match the signed release")
    public_manifest(digests["latest-mac.yml"])


def update(config, *, probe=False):
    state("syncing")
    outstanding = [] if probe else cleanup_completed_ci(read(ROOT / "completed.json"))
    tx = None if probe else read(ROOT / "pending.json")
    if tx:
        stale_main = remote_ref(FORK, "refs/heads/main") not in (tx.get("priorMain"), tx["forkMain"])
        changed = not tx.get("digests") and (config["seed"] != tx["seed"] or
                  remote_ref(UPSTREAM, "refs/heads/main") != tx["upstream"])
        if (stale_main or changed) and remote_ref(FORK, "refs/tags/" + tx["version"]) is None:
            os.replace(ROOT / "pending.json", ROOT / tx["batch"] / "superseded.json")
            tx = None
    if tx is None:
        tx = candidate(config, probe)
    if tx is None:
        return
    batch = ROOT / checked(tx["batch"], r"candidate-[\w-]+", "candidate directory")
    work, output = batch / "source", batch / "release"
    require(git(work, "rev-parse", "HEAD") == tx["commit"] and not git(work, "status", "--porcelain"),
            "The prepared release checkout changed")
    state("checking")
    hosted_checks(tx, work)
    state("signing")
    if not output.exists():
        command(sys.executable, ROOT / "package_macos.py", work, output, timeout=10800)
    digests = assets(output, tx["version"])
    if "digests" in tx:
        require(digests == tx["digests"], "The sealed release changed")
    else:
        persist(tx, digests=digests)
    app = output / "mac-arm64/OpenBot.app"
    installer = ROOT / "install_macos.py"
    try:
        command(sys.executable, installer, "--prepare", app, local=True, timeout=90000)
        state("publishing")
        publish(tx, work, output, digests)
        state("installing")
        command(sys.executable, installer, app, local=True, timeout=3600)
    finally:
        # If the owner app acknowledged a graceful quit, restore its window even after a failed upload.
        run = os.environ.get("OPENBOT_FORK_UPDATE_RUN")
        if run and (Path(run) / "quit-ack.json").is_file():
            command("/usr/bin/open", "-a", APP, check=False)
    tx["ciCleanupPending"] = list(dict.fromkeys([*outstanding, tx["commit"]]))
    save(ROOT / "completed.json", tx)
    (ROOT / "pending.json").unlink()
    state("complete")
    cleanup_completed_ci(tx)
    # Git history and release receipts remain. The installed app and recovery copy no longer
    # require another full checkout, dependency tree, and several large archives per release.
    if not git(ROOT / "cache.git", "worktree", "remove", "--force", work, check=False):
        shutil.rmtree(output, ignore_errors=True)


def main():
    os.umask(0o077)
    with (ROOT / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = False
        except BlockingIOError:
            busy = True
        if sys.argv[1:] == ["--status"]:
            phase = read(ROOT / "status.json", {"phase": "idle"})["phase"]
            print(json.dumps({"phase": "failed" if not busy and phase in ACTIVE else phase}))
            return 0
        if busy:
            return 0
        try:
            require(sys.argv[1:] in ([], ["--probe"]), "Unknown command")
            config = read(ROOT / "config.json")
            os.environ["PATH"] = os.pathsep.join(config["path"])
            update(config, probe=bool(sys.argv[1:]))
        except Stop as exc:
            state(exc.phase, str(exc))
            return 1
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
            state("failed", "Could not finish the update: " + type(error).__name__)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
