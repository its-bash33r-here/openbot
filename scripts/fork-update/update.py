"""Run one owner-initiated fork release. No network access in --status."""
import ctypes
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
import upload_route
from install_macos import STEP_EXIT

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


# These keys also identify the step in the local Settings catalog. Never send child output to the renderer.
PHASE_STEP = {"syncing": "sync", "checking": "checks", "signing": "sign", "publishing": "publish",
              "installing": "install"}
STEP_REASON = {
    "sync": "The fork and upstream source could not be synchronized. Check access and retry.",
    "checks": "GitHub checks could not finish successfully. Retry the update.",
    "sign": "OpenBot could not build or verify its signed Mac package. Check signing access and retry.",
    "quit": "OpenBot could not finish the idle quit. Let running work finish and retry.",
    "quitHelperTimeout": "OpenBot quit, but a background process did not close in time. Retry the update.",
    "backup": "OpenBot could not verify the recovery copy. Check free disk space and retry.",
    "verifyCopy": "The upgraded copy of the profile failed verification. Retry the update.",
    "publish": "OpenBot could not verify publication to the fork. Check GitHub access and retry.",
    "install": "OpenBot could not install the verified package. Retry the update.",
    "launch": "The installed app did not confirm startup. Keep the recovery copy and retry.",
}


def release_crash_reporter():
    # Crashpad sets these task exception ports; detached children inherit them on macOS. Clearing only
    # our own handlers before spawning any children lets the old app's reporter exit on its own.
    lib = ctypes.CDLL(None)
    task = ctypes.c_uint.in_dll(lib, "mach_task_self_").value
    mask = (1 << 10) | (1 << 11)  # EXC_CRASH | EXC_RESOURCE
    if int(os.uname().release.split(".", 1)[0]) < 22:
        mask |= 1 << 12  # Crashpad also sets EXC_GUARD before macOS 13.
    if lib.task_set_exception_ports(task, mask, 0, 1, 0) != 0:  # MACH_PORT_NULL, EXCEPTION_DEFAULT
        raise Stop("failed", STEP_REASON["quit"], "quit")


class Stop(Exception):
    def __init__(self, phase, reason, failed_step=None):
        super().__init__(reason)
        self.phase = phase
        self.failed_step = failed_step


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


def state(phase, reason="", *, failed_step=None):
    record = {"phase": phase, "reason": reason}
    if failed_step:
        record["failedStep"] = failed_step
    save(ROOT / "status.json", record)


def failed(error, phase="failed", step=None):
    current = read(ROOT / "status.json", {}).get("phase")
    step = step or PHASE_STEP.get(current, "sync")
    reason = str(error) if isinstance(error, Stop) and not str(error).startswith("Command failed:") else STEP_REASON[step]
    state(phase, reason, failed_step=step)


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
    installer_step = "backup" if "--prepare" in args else "install"
    try:
        p = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        if local:
            raise Stop("failed", STEP_REASON[installer_step], installer_step) from error
        raise
    if check:
        if local and p.returncode:
            step = next((name for name, code in STEP_EXIT.items() if code == p.returncode), installer_step)
            raise Stop("failed", STEP_REASON[step], step)
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


def published_commit():
    # The fork also publishes runtime assets. Only stable numeric tags with a Mac update feed count.
    names = gh("api", "--paginate", "repos/" + FORK + "/releases?per_page=100", "--jq",
               '.[] | select(.draft == false and .prerelease == false and any(.assets[]?; .name == "latest-mac.yml")) | .tag_name')
    versions = [name for name in names.splitlines() if re.fullmatch(VERSION, name)]
    if not versions:
        return None
    tag = "refs/tags/" + max(versions, key=semver)
    commit = remote_ref(FORK, tag)
    require(commit, "The published release is missing its version tag.")
    return commit


def candidate(config, probe=False, *, replacing=None, pin_inputs=False):
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
    staging = remote_ref(FORK, "refs/heads/" + BRANCH)
    tip = published_commit()
    prior = replacing or read(ROOT / "pending.json") or {}
    require(staging is not None or tip is None, "The Mac release branch is missing from published history.")
    require(staging == tip or (prior and staging in (prior["commit"], prior.get("stageBase", prior["previous"]))),
            "The Mac release branch contains changes outside this update. Review its history before retrying.")
    if replacing:
        require(replacing["previous"] == tip and remote_ref(FORK, "refs/tags/" + replacing["version"]) is None,
                "The previous candidate was tagged or its published baseline changed. Resume its release instead.")
        require(read(ROOT / "pending.json") == replacing, "The saved update changed during preparation.")
    if pin_inputs:
        require(replacing and fork in (replacing.get("priorMain"), replacing["forkMain"]),
                "Fork main changed after this update was prepared.")
        up, fork = replacing["upstream"], replacing["forkMain"]
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
        git(work, "switch", "--detach", replacing["source"] if pin_inputs else base)
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
        if not replacing and tip and last and tip == last["commit"] and base == source and installed() == last["version"]:
            state("upToDate")
            return None
        if tip and base == source:
            raise Stop("held", "An existing fork release needs recovery on this Mac.")
        state("checking")
        command("bun", "install", "--frozen-lockfile", cwd=work)
        # Carry forward the Git history of an unpublished candidate without treating its version
        # or generated changelog as published. The permanent branch can then fast-forward.
        parent = replacing["commit"] if replacing else tip
        if parent:
            git(work, "merge", "--no-ff", "-s", "ours", "--no-edit", parent)
        if tip:
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
              "stageBase": staging, "ciBranch": BRANCH, "ciCleanupPending": [], "forkMain": synced,
              "priorMain": replacing.get("priorMain", fork) if pin_inputs else (fork if assisted else synced)}
        if replacing:
            if pin_inputs:
                require(version == replacing["version"], "The rebuilt candidate no longer matches the pending version.")
                tx["pinnedInputs"] = True
            tx["ciCleanupPending"] = legacy_branches(replacing)
            save(ROOT / checked(replacing["batch"], r"candidate-[\w-]+", "previous directory") / "superseded.json", replacing)
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


def legacy_branches(receipt):
    return receipt.get("ciCleanupPending", [] if receipt.get("ciBranch") == BRANCH else [receipt["commit"]])


def cleanup_legacy_ci(commits, anchor):
    remaining = []
    cache = ROOT / "cache.git"
    for commit in commits:
        ref = "refs/heads/fork/mac-ci/" + checked(commit, SHA, "old CI revision")
        try:
            if remote_ref(FORK, ref) == commit:
                require(git(cache, "merge-base", "--is-ancestor", commit, anchor, check=False) == 0,
                        "The old CI commit is not in the release history.")
                git(cache, "push", "--force-with-lease=" + ref + ":" + commit, repo_url(FORK), ":" + ref, timeout=30)
        except (Stop, OSError, subprocess.SubprocessError):
            remaining.append(commit)
    return remaining


def cleanup_completed_ci(receipt):
    if not receipt:
        return []
    pending = legacy_branches(receipt)
    if not pending:
        return []
    # A completed receipt must still be anchored by its immutable public tag.
    try:
        tag = remote_ref(FORK, "refs/tags/" + checked(receipt["version"], VERSION, "completed version"))
    except (Stop, OSError, subprocess.SubprocessError):
        return pending
    if tag != receipt["commit"]:
        return pending
    remaining = cleanup_legacy_ci(pending, tag)
    if receipt.get("ciCleanupPending") != remaining:
        save(ROOT / "completed.json", {**receipt, "ciCleanupPending": remaining})
    return remaining


def hosted_checks(tx, work):
    commit = checked(tx["commit"], SHA, "candidate revision")
    target = "refs/heads/" + BRANCH
    remote = remote_ref(FORK, target)
    if remote != commit:
        require(remote == tx.get("stageBase", tx["previous"]), "The Mac release branch changed before CI.")
        require(remote is None or git(work, "merge-base", "--is-ancestor", remote, commit, check=False) == 0,
                "The candidate would overwrite the Mac release branch.")
        # Compare-and-swap includes a branch concurrently created, rewound, or deleted.
        git(work, "push", "--force-with-lease=" + target + ":" + (remote or ""), repo_url(FORK), commit + ":" + target)
    legacy = legacy_branches(tx)
    remaining = cleanup_legacy_ci(legacy, commit)
    if tx.get("ciBranch") != BRANCH or remaining != legacy:
        persist(tx, ciBranch=BRANCH, ciCleanupPending=remaining)
    def runs():
        return json.loads(gh("run", "list", "-R", FORK, "--workflow", "ci.yml", "--branch", BRANCH, "--commit",
                             commit, "--event", "workflow_dispatch", "--json", "databaseId,headSha,status,conclusion"))
    deadline = time.monotonic() + 7200
    if not runs():
        gh("workflow", "run", "ci.yml", "-R", FORK, "--ref", BRANCH)
    retry_attempt = None
    while time.monotonic() < deadline:
        found = [r for r in runs() if r["headSha"] == commit]
        if not found:
            require(remote_ref(FORK, target) == commit, "The Mac release branch moved before its CI run started.")
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
                    require(remote_ref(FORK, target) == commit, "The Mac release branch moved during CI.")
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
    require(published_commit() in (tx["previous"], tx["commit"]),
            "A different stable Mac release was published before this update.")
    require(remote_ref(FORK, branch) == tx["commit"], "The tested Mac release branch moved before publication.")
    if tx.get("priorMain", tx["forkMain"]) != tx["forkMain"]:
        current = remote_ref(FORK, "refs/heads/main")
        if current == tx["priorMain"]:
            git(work, "push", repo_url(FORK), tx["forkMain"] + ":refs/heads/main")
        elif current != tx["forkMain"]:
            # Resume a published transaction if main has since advanced and still contains the resolution.
            git(work, "fetch", "--no-tags", repo_url(FORK), "refs/heads/main")
            require(not git(work, "merge-base", "--is-ancestor", tx["forkMain"], "FETCH_HEAD", check=False),
                    "Fork main changed before publication. Retry with its new changes.")
    ref = "refs/tags/" + version
    tag = remote_ref(FORK, ref)
    if not tag:
        require(remote_ref(FORK, branch) == tx["commit"], "The tested Mac release branch moved before tagging.")
        git(work, "push", repo_url(FORK), tx["commit"] + ":" + ref)
    require(remote_ref(FORK, ref) == tx["commit"], "Release tag already names different code")
    # The authenticated CLI sees drafts. Only a confirmed absence permits creating a new draft.
    if command("gh", "release", "view", version, "-R", FORK, check=False):
        notes = directory.parent / "notes.md"
        notes.write_text("OpenBot " + version + " for Apple silicon.\n\nUpstream source commit: " + tx["upstream"] + ".\n")
        gh("release", "create", version, "-R", FORK, "--draft", "--verify-tag", "--title", "OpenBot " + version, "--notes-file", notes)
    identity = json.loads(gh("release", "view", version, "-R", FORK, "--json", "databaseId,tagName"))
    release_id = identity["databaseId"]
    require(type(release_id) is int and release_id > 0 and identity["tagName"] == version, "Unexpected release")
    endpoint = f"repos/{FORK}/releases/{release_id}"

    def is_starter(asset, name):
        return (asset["name"] == name and asset["state"] == "starter" and asset["digest"] in (None, "")
                and type(asset["id"]) is int and asset["id"] > 0 and type(asset["size"]) is int
                and asset["size"] in (0, (directory / name).stat().st_size))

    def snapshot(*, draft=False):
        record = api(endpoint)
        require(record["id"] == release_id and record["tag_name"] == version and not record["prerelease"],
                "Release identity changed")
        require(not draft or record["draft"] is True, "The release is no longer a draft")
        present = {}
        # The release object can omit failed uploads. A release with over 100 assets
        # already exceeds our six allowed names, so the first page suffices to fail closed.
        for asset in api(endpoint + "/assets?per_page=100"):
            name = asset["name"]
            require(name in digests and name not in present, "Unexpected release asset")
            require((record["draft"] is True and is_starter(asset, name))
                    or (asset["state"] == "uploaded" and asset["digest"] == digests[name]),
                    "An uploaded asset differs from the signed files")
            present[name] = asset
        return record["draft"], present

    def pending(name):
        require(remote_ref(FORK, ref) == tx["commit"], "The release tag moved during upload")
        return snapshot(draft=True)[1].get(name)

    for name, digest in digests.items():
        draft, present = snapshot()
        asset = present.get(name)
        if asset and asset["state"] == "uploaded":
            continue
        require(draft is True, "An existing public release is incomplete")
        path = directory / name
        try:
            if not command("gh", "release", "upload", version, str(path), "-R", FORK, timeout=7200, check=False):
                continue
        except subprocess.TimeoutExpired:
            pass
        asset = pending(name)
        if asset and asset["state"] == "uploaded":
            continue
        address = upload_route.alternate_address()
        token = gh("auth", "token", "--hostname", "github.com")
        fresh = pending(name)
        if fresh and fresh["state"] == "uploaded":
            continue
        if fresh:
            require(asset and fresh["id"] == asset["id"], "The failed upload placeholder changed")
            asset_endpoint = f"repos/{FORK}/releases/assets/{fresh['id']}"
            detail = api(asset_endpoint)
            require(detail["id"] == fresh["id"] and is_starter(detail, name),
                    "The failed upload placeholder changed before deletion")
            gh("api", "--method", "DELETE", asset_endpoint)
            require(pending(name) is None, "Upload target changed after placeholder removal")
        receipt = upload_route.upload(FORK, release_id, path, address, token)
        if receipt is not None:
            require(receipt["name"] == name and receipt["state"] == "uploaded" and receipt["digest"] == digest,
                    "Alternate upload receipt differs from the signed file")
        verified = pending(name)
        require(verified and verified["state"] == "uploaded", "Alternate upload could not be verified")
    draft, present = snapshot()
    require(set(present) == set(digests) and all(a["state"] == "uploaded" for a in present.values()),
            "Uploaded release hashes differ")
    if draft:
        require(remote_ref(FORK, ref) == tx["commit"], "The release tag moved before publication")
        gh("release", "edit", version, "-R", FORK, "--draft=false", "--latest")
    public = api("repos/" + FORK + "/releases/latest")
    public_draft, public_assets = snapshot()
    require(public["id"] == release_id and public["tag_name"] == version and not public["draft"] and not public["prerelease"] and
            not public_draft and public_assets.keys() == digests.keys(), "The stable feed does not match the signed release")
    public_manifest(digests["latest-mac.yml"])


def update(config, *, probe=False):
    state("syncing")
    outstanding = [] if probe else cleanup_completed_ci(read(ROOT / "completed.json"))
    tx = None if probe else read(ROOT / "pending.json")
    if tx:
        current_main = remote_ref(FORK, "refs/heads/main")
        stale_main = current_main not in (tx.get("priorMain"), tx["forkMain"])
        if tx.get("pinnedInputs") or tx.get("digests"):
            if stale_main:
                cache = ROOT / "cache.git"
                git(cache, "fetch", "--no-tags", repo_url(FORK), "refs/heads/main")
                require(not git(cache, "merge-base", "--is-ancestor", tx["forkMain"], "FETCH_HEAD", check=False),
                        "Fork main no longer contains this sealed or pinned update. Review the branch before retrying.")
        elif stale_main or config["seed"] != tx["seed"] or remote_ref(UPSTREAM, "refs/heads/main") != tx["upstream"]:
            if remote_ref(FORK, "refs/tags/" + tx["version"]) is None:
                tx = candidate(config, replacing=tx)
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
        state("installing")
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
    tx["ciCleanupPending"] = list(dict.fromkeys([*outstanding, *legacy_branches(tx)]))
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
            record = read(ROOT / "status.json", {"phase": "idle"})
            phase = record["phase"]
            if not busy and phase in ACTIVE:
                print(json.dumps({"phase": "failed", "failedStep": PHASE_STEP[phase]}))
            else:
                result = {"phase": phase}
                if record.get("failedStep") in STEP_REASON:
                    result["failedStep"] = record["failedStep"]
                print(json.dumps(result))
            return 0
        if busy:
            return 0
        try:
            require(sys.argv[1:] in ([], ["--probe"]), "Unknown command")
            config = read(ROOT / "config.json")
            os.environ["PATH"] = os.pathsep.join(config["path"])
            if not sys.argv[1:] and sys.platform == "darwin" and os.environ.get("OPENBOT_FORK_UPDATE_PID"):
                release_crash_reporter()
            update(config, probe=bool(sys.argv[1:]))
        except Stop as exc:
            failed(exc, exc.phase, exc.failed_step)
            return 1
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
            failed(error)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
