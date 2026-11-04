"""Exercise the release runner with real local Git remotes and disposable external-service fixtures."""
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import shutil
import signal
import uuid
import tempfile

import update as u


def script(path, body, executable=False):
    path.write_text(body)
    if executable:
        path.chmod(0o700)


def fixture_services(directory):
    """Record GitHub operations and inject one failed CI run and one interrupted upload."""
    db = directory / "github.json"
    u.save(db, {"ci": "missing", "assets": {}, "release": False, "draft": True, "interrupt": True})
    script(directory / "gh", "#!" + sys.executable + "\n" + "db=" + repr(str(db)) + "\n" + """
import hashlib, json, pathlib, sys, subprocess
a=sys.argv[1:]
p=pathlib.Path(db)
d=json.loads(p.read_text())
def flush(): p.write_text(json.dumps(d))
def assets():
    return [{"name":k,"digest":v,"id":i+100,"state":"starter" if v is None else "uploaded",
             "size":d.get("starterSizes",{}).get(k,0) if v is None else 10} for i,(k,v) in enumerate(d["assets"].items())]
release_path = "repos/its-bash33r-here/openbot/releases"
embedded = [row for row in assets() if row["state"] != "starter"]
if a == ["api", release_path+"/41/assets?per_page=100"]:
    print(json.dumps(assets())); sys.exit()
if a[0]=="api" and a[1].startswith(release_path+"/assets/"):
    row=next(r for r in assets() if a[1]==release_path+"/assets/"+str(r["id"]))
    row.update(d.get("detailChange",{}))
    d["inspected"]=row; flush(); print(json.dumps(row)); sys.exit()
if a[:2]==["auth","token"]:
    assert a==["auth","token","--hostname","github.com"]
    print("fixture-credential"); sys.exit()
if a[:3]==["api","--method","DELETE"]:
    asset=next(row for row in assets() if a[3]=="repos/its-bash33r-here/openbot/releases/assets/"+str(row["id"]))
    assert d["draft"] and asset["state"]=="starter" and d.pop("inspected",None)==asset
    d.setdefault("deleted",[]).append(asset)
    del d["assets"][asset["name"]]; flush(); sys.exit()
if a[:2]==["api","--paginate"]:
    assert a[2]=="repos/its-bash33r-here/openbot/releases?per_page=100"
    print("remote-desktop-runtime-2026")
    if d.get("stable"): print(d["stable"])
elif a[0]=="api":
    assert a[1] in ("repos/its-bash33r-here/openbot/releases/latest","repos/its-bash33r-here/openbot/releases/41")
    print(json.dumps({"id":41,"draft":d["draft"],"prerelease":False,"tag_name":d["version"],"assets":embedded}))
elif a[:2]==["run","list"]:
    assert a[a.index("--branch")+1]=="fork/mac-release"
    sha=a[a.index("--commit")+1]
    print(json.dumps([] if d.get("checked")!=sha else [{"headSha":sha,"databaseId":12,"status":"completed","conclusion":d["ci"]}]))
elif a[:2]==["workflow","run"]:
    assert a[a.index("--ref")+1]=="fork/mac-release"
    d["checked"]=subprocess.check_output(["git","-C",str(p.parent/"fork.git"),"rev-parse","refs/heads/fork/mac-release"],text=True).strip()
    d["ci"]="failure" if d["ci"]=="missing" else "success"; flush()
elif a[:2]==["run","view"]:
    if a[-1]==".attempt": print(1)
    else:
        jobs=["Check","Tests (desktop 1/2)","Tests (desktop 2/2)","Browser smoke","Signal E2E","Team API and reverse actions E2E"]
        print(json.dumps({"headSha":d["checked"],"conclusion":"success","jobs":[{"name":n,"conclusion":"success"} for n in jobs]}))
elif a[:2]==["run","rerun"]: d["ci"]="success"; flush()
elif a[:2]==["release","view"]:
    if not d["release"]: sys.exit(1)
    print(json.dumps({"databaseId":41,"tagName":d["version"],"isDraft":d["draft"],"isPrerelease":False,"assets":embedded}))
elif a[:2]==["release","create"]:
    assert not d["release"] and "--draft" in a
    d.update(release=True,version=a[2]); flush()
elif a[:2]==["release","upload"]:
    path=pathlib.Path(a[3]); assert d["draft"] and "--clobber" not in a
    if path.name in d["assets"]: sys.exit(1)
    mode=d.get("faults",{}).pop(path.name,None)
    if mode:
        if mode=="starter": d["assets"][path.name]=None
        if mode=="public": d["draft"]=False
        if mode=="conflict": d["assets"][path.name]="sha256:other"
        if mode=="lost": d["assets"][path.name]="sha256:"+hashlib.sha256(path.read_bytes()).hexdigest()
        flush(); sys.exit(1)
    if len(d["assets"])==2 and d["interrupt"]: d["interrupt"]=False; flush(); sys.exit(1)
    d["assets"][path.name]="sha256:"+hashlib.sha256(path.read_bytes()).hexdigest(); flush()
elif a[:2]==["release","edit"]:
    assert len(d["assets"])==6 and d["draft"]
    d["draft"]=False; d["stable"]=d["version"]; flush()
else: raise ValueError(a)
""", True)
    script(directory / "bun", "#!" + sys.executable + """
import json,pathlib,sys
if "patch" in sys.argv:
    p=pathlib.Path("package.json"); d=json.loads(p.read_text()); a,b,c=map(int,d["version"].split("."))
    d["version"]=f"{a}.{b}.{c+1}"; p.write_text(json.dumps(d)); pathlib.Path("CHANGELOG.md").write_text(d["version"])
""", True)
    script(u.ROOT / "package_macos.py", "import hashlib,pathlib,sys,json\np=pathlib.Path(sys.argv[2]); p.mkdir()\n"
           "v=json.loads((pathlib.Path(sys.argv[1])/'package.json').read_text())['version']\n"
           "checks=[]\nfor pattern in " + repr(u.ASSETS[:-1]) + ":\n"
           "    name=pattern.format(v=v); (p/name).write_text(name); checks.append(hashlib.sha256(name.encode()).hexdigest()+'  '+name)\n"
           "(p/'SHA256SUMS-macos.txt').write_text('\\n'.join(checks)+'\\n'); (p/'mac-arm64/OpenBot.app').mkdir(parents=True)\n")
    script(u.ROOT / "install_macos.py", "import pathlib,sys\np=pathlib.Path(__file__).with_name('installs'); "
           "failed=p.with_name('fail-install');\nif '--prepare' not in sys.argv and failed.exists(): sys.exit(1)\n"
           "p.open('a').write(('prepare' if '--prepare' in sys.argv else 'install')+'\\n')")


def hidden_zip_starter(folder, sealed, batch):
    # Real 0.60.3 metadata shape. A sparse file keeps the rehearsal small on disk.
    db = folder / "github.json"
    original = u.read(db)
    release = folder / "hidden-starter"
    release.mkdir()
    name = u.ASSETS[0].format(v=sealed["version"])
    size = 324597882
    with (release / name).open("w+b") as file:
        file.truncate(size)
        digest = "sha256:" + hashlib.file_digest(file, "sha256").hexdigest()
    digests = {**sealed["digests"], name: digest}
    def rehearse():
        u.publish(sealed, batch / "source", release, digests)
    try:
        record = u.read(db)
        record.update(draft=True, starterSizes={name:size}, deleted=[])
        record["assets"][name] = None
        for change in ({"id":999}, {"state":"uploaded"}, {"name":"wrong.zip"},
                       {"size":size-1}, {"digest":"sha256:other"}):
            u.save(db, {**record, "detailChange":change})
            try:
                rehearse()
            except u.Stop as exc:
                assert "changed before deletion" in str(exc)
            else:
                raise AssertionError("Changed starter accepted")
            assert u.read(db)["deleted"] == [] and u.read(db)["draft"]
        u.save(db, record)
        rehearse()
        result = u.read(db)
        removed, = result["deleted"]
        assert removed["name"] == name and removed["size"] == size and removed["digest"] is None
        assert result["assets"][name] == digest and not result["draft"]
    finally:
        u.save(db, original)


def assisted_release(folder, bun):
    root = folder / "assisted"
    root.mkdir()
    u.ROOT = root / "owner"
    u.ROOT.mkdir()
    fixture_services(root)
    os.environ["PATH"] = str(root) + os.pathsep + os.environ["PATH"]
    fork, upstream, src = (root / name for name in ("fork.git", "up.git", "src"))
    for repo in (fork, upstream):
        u.command("git", "init", "--bare", repo)
    u.repo_url = lambda name: str(fork if name == u.FORK else upstream)
    u.command("git", "init", "-b", "main", src)
    u.git(src, "config", "user.name", "Fixture")
    u.git(src, "config", "user.email", "test@example.invalid")
    (src / "package.json").write_text('{"version":"1.0.0"}')
    (src / "changelog.d").mkdir()
    (src / "changelog.d/fixture.md").write_text("notes")
    (src / "CHANGELOG.md").write_text("base")
    (src / "guide.md").write_text("heading\nend\n")
    u.git(src, "add", ".")
    u.git(src, "commit", "-m", "base")
    seed = u.git(src, "rev-parse", "HEAD")
    cfg = {"seed": seed, "source": str(src)}
    for repo, edit in ((fork, "Fork setting"), (upstream, "Upstream setting")):
        u.git(src, "switch", "--detach", seed)
        (src / "guide.md").write_text("heading\n" + edit + "\nend\n")
        u.git(src, "commit", "-am", edit)
        u.git(src, "push", str(repo), "HEAD:refs/heads/main")
    original = u.remote_ref(u.FORK, "refs/heads/main")
    run = u.ROOT / "run"
    run.mkdir(mode=0o700)
    nonce = str(uuid.uuid4())
    os.environ.update(OPENBOT_FORK_UPDATE_RUN=str(run), OPENBOT_FORK_UPDATE_NONCE=nonce,
                      OPENBOT_FORK_UPDATE_MERGE="1", OPENBOT_FORK_UPDATE_PID=str(os.getpid()))
    def expect(phase, probe=False):
        try:
            u.update(cfg, probe=probe)
        except u.Stop as exc:
            assert exc.phase == phase, str(exc)
        else:
            raise AssertionError("Expected " + phase)
    events = run / "fixture-events"
    expect("conflict", probe=True)
    assert not (run / "merge-request.json").exists()
    assert u.remote_ref(u.FORK, "refs/heads/main") == original
    bridge = [bun, Path(__file__).with_name("merge-fixture.ts"), str(run), nonce]
    synthetic = {"nonce": str(uuid.uuid4()), "id": str(uuid.uuid4()), "path": "guide.md",
                 "merge": "the saved Mac changes", "sides": {"2": "1234567890", "3": "0987654321"},
                 "hunk": {"base": "", "current": "example", "incoming": "second example"}}
    u.save(run / "merge-request.json", synthetic)
    subprocess.run([*bridge, "once"], check=True)
    assert not events.exists() and not (run / "merge-response.json").exists()
    synthetic["nonce"] = nonce
    synthetic["hunk"]["current"] = "password=example-pass-1234"
    u.save(run / "merge-request.json", synthetic)
    (run / "fixture-mode").write_text("ask")
    subprocess.run([*bridge, "once"], check=True)
    assert events.read_text().splitlines() == ["window guide.md"]
    assert u.read(run / "merge-response.json")["action"] == "later"
    synthetic.update(id=str(uuid.uuid4()), hunk={"base": "Show one faint star.\n",
                     "current": "Show two faint star.\n", "incoming": "Show one bright stars.\n"})
    u.save(run / "merge-request.json", synthetic)
    (run / "fixture-mode").write_text("words")
    subprocess.run([*bridge, "once"], check=True)
    assert u.read(run / "merge-response.json").get("text") == "Show two bright stars.\n"
    assert events.read_text().splitlines() == ["window guide.md", "model"]
    for path in ("merge-request.json", "merge-response.json", "fixture-events"):
        (run / path).unlink()
    worker = subprocess.Popen(bridge)
    def timed_out(*_args):
        raise TimeoutError("The app merge fixture did not answer in time.")
    previous_handler = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(90)
    try:
        for mode in ("invent", "ask", "auto"):
            (run / "fixture-mode").write_text(mode)
            expect("failed" if mode == "auto" else "conflict")
            assert u.remote_ref(u.FORK, "refs/heads/main") == original
            assert not u.remote_ref(u.FORK, "refs/tags/1.0.1") and not (u.ROOT / "installs").exists()
        assert events.read_text().splitlines() == ["model", "window guide.md", "model", "window guide.md", "model"]
        tx = u.read(u.ROOT / "pending.json")
        assert tx["priorMain"] == original and tx["forkMain"] != original
        assert u.git(fork, "show", tx["commit"] + ":guide.md").splitlines() == [
            "heading", "Fork setting", "Upstream setting", "end"]
        # Retry the exact checked commit. A complete dry-run install is permitted only after CI passes.
        service = u.read(root / "github.json")
        service["interrupt"] = False
        u.save(root / "github.json", service)
        u.update(cfg)
        assert u.remote_ref(u.FORK, "refs/heads/main") == tx["forkMain"]
        assert u.remote_ref(u.FORK, "refs/tags/1.0.1") == tx["commit"]
        assert (u.ROOT / "installs").read_text().splitlines() == ["prepare", "install"]
        assert events.read_text().splitlines().count("model") == 3
        # A delete/edit conflict needs a whole-file decision and never calls the model.
        u.git(src, "fetch", str(fork), "refs/heads/main")
        u.git(src, "switch", "--detach", "FETCH_HEAD")
        u.git(src, "rm", "guide.md")
        u.git(src, "commit", "-m", "delete guide in fork")
        u.git(src, "push", str(fork), "HEAD:main")
        u.git(src, "fetch", str(upstream), "refs/heads/main")
        u.git(src, "switch", "--detach", "FETCH_HEAD")
        (src / "guide.md").write_text("new upstream guide\n")
        u.git(src, "commit", "-am", "edit guide upstream")
        u.git(src, "push", str(upstream), "HEAD:main")
        (run / "fixture-mode").write_text("choose")
        updated = u.candidate(cfg)
        assert u.git(u.ROOT / updated["batch"] / "source", "show", "HEAD:guide.md") == "new upstream guide"
        assert events.read_text().splitlines()[-1] == "window guide.md"
        assert events.read_text().splitlines().count("model") == 3
        # The old app can bootstrap when its pinned source already carries this precise reviewed merge.
        fork_tip = u.remote_ref(u.FORK, "refs/heads/main")
        candidate_cache = u.ROOT / "cache.git"
        u.git(src, "fetch", str(candidate_cache), updated["forkMain"])
        checked_cfg = {"seed": updated["forkMain"], "source": str(src)}
        os.environ.pop("OPENBOT_FORK_UPDATE_MERGE")
        u.candidate(checked_cfg, probe=True)
        assert u.read(u.ROOT / "status.json")["phase"] == "idle"
        assert u.remote_ref(u.FORK, "refs/heads/main") == fork_tip
        replay = u.candidate(checked_cfg)
        assert replay["priorMain"] == fork_tip and replay["forkMain"] == updated["forkMain"]
        assert events.read_text().splitlines().count("model") == 3

    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        worker.terminate()
        worker.wait(timeout=5)
        for key in (*u.QUIT_ENV, "OPENBOT_FORK_UPDATE_MERGE"):
            os.environ.pop(key, None)


def main():
    os.umask(0o077)
    bun = shutil.which("bun")
    assert bun, "The native merge fixture requires Bun."
    out = Path(__file__).resolve().parents[2] / ".openbot-build/fork-release-e2e/report.json"
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp).resolve()
        u.ROOT = folder / "owner"
        u.ROOT.mkdir()
        fixture_services(folder)
        os.environ["PATH"] = str(folder) + os.pathsep + os.environ["PATH"]
        origin = folder / "fork.git"
        upstream = folder / "up.git"
        src = folder / "seed"
        for repo in (origin, upstream):
            u.command("git", "init", "--bare", repo)
        u.command("git", "init", "-b", "main", src)
        for k,v in (("user.name","Fixture"), ("user.email","test@example.invalid")):
            u.git(src, "config", k, v)
        (src / "package.json").write_text('{"version":"1.0.0"}')
        (src / "changelog.d").mkdir()
        (src / "changelog.d/base.md").write_text("base")
        (src / "CHANGELOG.md").write_text("base")
        (src / "file").write_text("base")
        u.git(src, "add", "."); u.git(src, "commit", "-m", "base")
        u.git(src, "tag", "v1.0.0"); u.git(src,"push",str(upstream),"main","v1.0.0")
        u.git(src,"push",str(origin),"main")
        (src / "file").write_text("fork")
        u.git(src, "commit","-am","fork change")
        seed = u.git(src,"rev-parse","HEAD")
        cfg = {"source": str(src), "seed": seed}
        u.repo_url = lambda name: str(origin if name == u.FORK else upstream)
        u.public_manifest = lambda digest: None  # No public network in this fixture.
        u.installed = lambda: u.read(u.ROOT / "completed.json", {"version": "1.0.0"})["version"]
        def unexpected_health_check(*args, **kwargs):
            raise AssertionError("The release runner contacted an external clearance service")
        u.urllib.request.urlopen = unexpected_health_check

        # Exercise the actual resolver selection and upload request, offline.
        host = u.upload_route.HOST
        address = "140.82.114.13"
        fallback_calls = []
        fallback_mode = "fail"
        u.upload_route.normal_addresses = lambda: {"140.82.113.13"}
        def fake_curl(*args, timeout, headers=None):
            url = args[-1]
            if url.startswith("https://dns.google/"):
                assert headers is None and url == ("https://dns.google/resolve?name=uploads.github.com"
                                                   "&type=A&edns_client_subnet=8.8.8.0/24")
                data = {"Status":0, "Answer":[{"name":host, "type":1, "data":ip}
                                              for ip in ("8.8.8.8", "140.82.113.13", address)]}
            elif url == "https://api.github.com/meta":
                assert headers is None
                data = {"web":["140.82.112.0/20"]}
            else:
                path = Path(args[args.index("--upload-file")+1])
                assert url == f"https://{host}/repos/{u.FORK}/releases/41/assets?name={path.name}"
                assert args[args.index("--resolve")+1] == f"{host}:443:{address}"
                assert headers == b"Authorization: Bearer fixture-credential\n" and "fixture-credential" not in str(args)
                db = folder / "github.json"
                service = u.read(db)
                assert service["draft"] and path.name not in service["assets"]
                fallback_calls.append(path.name)
                if fallback_mode == "fail":
                    return subprocess.CompletedProcess(args, 28, b"")
                with path.open("rb") as file:
                    digest = "sha256:" + hashlib.file_digest(file, "sha256").hexdigest()
                service["assets"][path.name] = digest
                u.save(db, service)
                if path.name.endswith(".dmg.blockmap"):
                    return subprocess.CompletedProcess(args, 52, b"")
                if fallback_mode == "truncated":
                    return subprocess.CompletedProcess(args, 0, b'{"name":\n201')
                receipt = {"name":path.name, "state":"uploaded", "digest":digest}
                return subprocess.CompletedProcess(args, 0, json.dumps(receipt).encode()+b"\n201")
            return subprocess.CompletedProcess(args, 0, json.dumps(data).encode())
        u.upload_route.curl = fake_curl

        u.git(src, "switch", "--detach", "v1.0.0")
        (src / "fork-only").write_text("preserve the fork")
        u.git(src, "add", ".")
        u.git(src, "commit", "-m", "public fork fix")
        fork_head = u.git(src, "rev-parse", "HEAD")
        u.git(src, "push", str(origin), "HEAD:main")
        u.git(src, "switch", "--detach", "v1.0.0")
        for path in ("packages/whatsapp/src/signal/crypto", "remote/api/new-protocol"):
            (src / path).parent.mkdir(parents=True)
            (src / path).write_text("untagged upstream change")
        u.git(src, "add", ".")
        u.git(src, "commit", "-m", "upstream changes after latest release")
        upstream_head = u.git(src, "rev-parse", "HEAD")
        u.git(src, "push", str(upstream), "HEAD:main")
        u.update(cfg, probe=True)
        assert u.read(u.ROOT / "status.json")["phase"] == "idle"
        assert u.remote_ref(u.FORK, "refs/heads/main") == fork_head and not (u.ROOT / "pending.json").exists()
        def expect(phase):
            try:
                u.update(cfg)
            except u.Stop as e:
                assert e.phase == phase, (e.phase, str(e))
                return
            raise AssertionError("Expected " + phase)
        expect("failed")  # Hosted CI fails before signing or tagging.
        tx=u.read(u.ROOT/"pending.json")
        release_ref = "refs/heads/" + u.BRANCH
        def only_two_branches():
            assert set(u.git(origin, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines()) == {
                "refs/heads/main", release_ref}
        assert u.remote_ref(u.FORK, release_ref) == tx["commit"]
        only_two_branches()
        synced = u.remote_ref(u.FORK, "refs/heads/main")
        assert u.git(origin, "show", "-s", "--format=%P", synced).split() == [fork_head, upstream_head]
        assert tx["forkMain"] == synced and tx["upstream"] == upstream_head
        assert u.git(origin, "show", tx["commit"] + ":remote/api/new-protocol") == "untagged upstream change"
        assert u.git(origin, "show", tx["commit"] + ":fork-only") == "preserve the fork"
        assert not (u.ROOT/tx["batch"]/"release").exists() and not u.remote_ref(u.FORK,"refs/tags/1.0.1")
        # A concurrent owner change to the permanent branch cannot be overwritten or tagged.
        u.git(origin, "update-ref", release_ref, fork_head, tx["commit"])
        expect("failed")
        try:
            u.publish(tx, u.ROOT / tx["batch"] / "source", folder, {})
        except u.Stop as exc:
            assert "moved before publication" in str(exc)
        else:
            raise AssertionError("A displaced candidate was published")
        assert u.remote_ref(u.FORK, release_ref) == fork_head and not u.remote_ref(u.FORK, "refs/tags/1.0.1")
        u.git(origin, "update-ref", release_ref, tx["commit"], fork_head)
        # Supersede a staged, failed first candidate. There is still no public version tag.
        first_attempt = tx["commit"]
        tx = u.candidate(cfg, replacing=tx)
        assert tx["stageBase"] == first_attempt and tx["previous"] is None and tx["version"] == "1.0.1"
        assert u.remote_ref(u.FORK, release_ref) == first_attempt
        expect("failed")  # Two assets reached the draft. The next click must finish that draft.
        assert len(u.read(folder/"github.json")["assets"])==2 and u.read(folder/"github.json")["draft"]
        assert "install" not in (u.ROOT/"installs").read_text()
        assert u.remote_ref(u.FORK, release_ref) == tx["commit"]
        only_two_branches()
        assert len(fallback_calls) == 1
        db = folder / "github.json"
        service = u.read(db)
        missing = [p.format(v=tx["version"]) for p in u.ASSETS if p.format(v=tx["version"]) not in service["assets"]]
        service["faults"] = {missing[0]:"conflict"}
        u.save(db, service)
        expect("failed")
        service = u.read(db)
        assert service["draft"] and service["assets"][missing[0]] == "sha256:other" and len(fallback_calls) == 1
        assert "install" not in (u.ROOT/"installs").read_text()
        # Publication by another owner during the CLI attempt must prevent all recovery writes.
        del service["assets"][missing[0]]
        service["faults"] = {missing[0]:"public"}
        u.save(db, service)
        sealed = u.read(u.ROOT / "pending.json")
        batch = u.ROOT / sealed["batch"]
        try:
            u.publish(sealed, batch / "source", batch / "release", sealed["digests"])
        except u.Stop as exc:
            assert "no longer a draft" in str(exc)
        else:
            raise AssertionError("Public release accepted for upload recovery")
        service = u.read(db)
        assert not service["draft"] and missing[0] not in service["assets"] and len(fallback_calls) == 1
        service["draft"] = True
        # Restore only fixture data, then exercise both lost responses and a verified 201.
        service["faults"] = dict(zip(missing, ["starter","fail","lost","fail"]))
        u.save(db, service)
        fallback_mode = "success"
        command = u.command
        def lose_primary_response(*args, **kwargs):
            result = command(*args, **kwargs)
            if args[:3] == ("gh", "release", "upload") and Path(args[4]).name == missing[2] and result:
                raise subprocess.TimeoutExpired(args[:3], 7200)
            return result
        u.command = lose_primary_response
        (u.ROOT / "fail-install").touch()
        expect("failed")  # Publication succeeded, but the Mac installation failed.
        u.command = command
        assert fallback_calls[1:] == [missing[0], missing[1], missing[3]]
        hidden_zip_starter(folder, sealed, batch)
        assert not u.read(folder / "github.json")["draft"]
        assert u.remote_ref(u.FORK, release_ref) == tx["commit"]
        only_two_branches()
        (u.ROOT / "fail-install").unlink()
        u.update(cfg)
        assert u.remote_ref(u.FORK, release_ref) == tx["commit"]
        only_two_branches()
        assert u.remote_ref(u.FORK, "refs/heads/main") == synced
        assert u.read(u.ROOT/"status.json")["phase"]=="complete" and not u.read(folder/"github.json")["draft"]
        assert tx["commit"]==u.remote_ref(u.FORK,"refs/tags/1.0.1")==u.remote_ref(u.FORK,"refs/heads/"+u.BRANCH)
        assert (u.ROOT/"installs").read_text().splitlines()==["prepare","prepare","prepare","prepare","install"]
        u.update(cfg)
        assert u.read(u.ROOT/"status.json")["phase"]=="upToDate"
        # Advance fork main and verify that release history fast-forwards on a second signed version.
        u.git(src, "fetch", str(origin), "refs/heads/main")
        u.git(src, "switch", "--detach", "FETCH_HEAD")
        (src / "next").write_text("another fork change")
        u.git(src, "add", "next")
        u.git(src, "commit", "-m", "next change")
        u.git(src, "push", str(origin), "HEAD:main")
        u.git(src, "switch", "--detach", upstream_head)
        (src / "next-upstream").write_text("another upstream change")
        u.git(src, "add", "next-upstream")
        u.git(src, "commit", "-m", "advance upstream main again")
        next_upstream = u.git(src, "rev-parse", "HEAD")
        u.git(src, "push", str(upstream), "HEAD:main")
        # The service fixture now serves the new tag and commit's CI and release records.
        service = u.read(folder / "github.json")
        service.update(assets={}, release=False, draft=True)
        u.save(folder / "github.json", service)
        # Simulate a sealed, unpublished candidate from the old updater and a changed desktop seed.
        old = u.candidate(cfg)
        work = u.ROOT / old["batch"] / "source"
        legacy_ref = "refs/heads/fork/mac-ci/" + old["commit"]
        u.git(work, "push", str(origin), old["commit"] + ":" + legacy_ref)
        for key in ("ciBranch", "stageBase", "ciCleanupPending"):
            old.pop(key)
        old["digests"] = {"old-package": "sealed"}
        u.save(u.ROOT / "pending.json", old)
        u.git(src, "switch", "--detach", seed)
        (src / "retry-ui").write_text("new Mac retry screen")
        u.git(src, "add", "retry-ui")
        u.git(src, "commit", "-m", "new Mac retry UI")
        cfg["seed"] = u.git(src, "rev-parse", "HEAD")
        rebuilt = u.candidate(cfg, replacing=old, pin_inputs=True)
        assert rebuilt["version"] == old["version"] and rebuilt["commit"] != old["commit"]
        assert rebuilt["forkMain"] == old["forkMain"] and rebuilt["upstream"] == old["upstream"]
        assert "digests" not in rebuilt and u.read(u.ROOT / old["batch"] / "superseded.json") == old
        assert u.git(u.ROOT / rebuilt["batch"] / "source", "show", "HEAD:retry-ui") == "new Mac retry screen"
        # The old candidate is retained until the release branch contains it. A probe cannot delete it.
        u.update(cfg, probe=True)
        assert u.remote_ref(u.FORK, legacy_ref) == old["commit"]
        # A later upstream change must not change the rebuilt version under test.
        u.git(src, "switch", "--detach", next_upstream)
        (src / "future-upstream").write_text("next button press")
        u.git(src, "add", "future-upstream")
        u.git(src, "commit", "-m", "new source for a later release")
        u.git(src, "push", str(upstream), "HEAD:main")
        u.git(origin, "config", "receive.denyDeletes", "true")
        # One update must publish and install even when the primary fails and the alternate reply is truncated.
        disk_image = f"OpenBot-{rebuilt['version']}-arm64.dmg"
        service = u.read(db)
        service["faults"] = {disk_image: "fail"}
        u.save(db, service)
        fallback_mode = "truncated"
        previous_calls = len(fallback_calls)
        previous_installs = (u.ROOT / "installs").read_text().splitlines()
        u.update(cfg)
        assert fallback_calls[previous_calls:] == [disk_image]
        assert not u.read(db)["draft"] and not (u.ROOT / "pending.json").exists()
        assert (u.ROOT / "installs").read_text().splitlines() == previous_installs + ["prepare", "install"]
        second = u.read(u.ROOT / "completed.json")
        assert u.read(u.ROOT / "status.json")["phase"] == "complete"
        assert second["commit"] == rebuilt["commit"] and second["upstream"] == next_upstream
        u.git(upstream, "update-ref", "refs/heads/main", next_upstream)
        assert second["ciCleanupPending"] == [old["commit"]]
        assert u.remote_ref(u.FORK, release_ref) == second["commit"] and u.remote_ref(u.FORK, legacy_ref) == old["commit"]
        assert u.git(origin, "merge-base", "--is-ancestor", old["commit"], second["commit"], check=False) == 0
        u.git(origin, "config", "receive.denyDeletes", "false")
        u.update(cfg)
        only_two_branches()
        assert u.read(u.ROOT / "completed.json")["ciCleanupPending"] == []
        # A legacy receipt may refer to a CI branch that another owner has repurposed.
        second_ref = "refs/heads/fork/mac-ci/" + second["commit"]
        u.git(origin, "update-ref", second_ref, fork_head)
        u.git(origin, "update-ref", "refs/heads/fork/mac-ci/" + fork_head, fork_head)
        u.save(u.ROOT / "completed.json", {k: v for k, v in second.items() if k not in ("ciCleanupPending", "ciBranch")})
        u.update(cfg)
        assert u.remote_ref(u.FORK, second_ref) == fork_head
        assert u.remote_ref(u.FORK, "refs/heads/fork/mac-ci/" + fork_head) == fork_head
        assert u.remote_ref(u.FORK, release_ref) == second["commit"]
        assert second["version"] == "1.0.2" and second["previous"] == tx["commit"]
        assert u.git(origin, "merge-base", "--is-ancestor", tx["commit"], second["commit"], check=False) == 0
        assert u.git(origin, "show", second["commit"] + ":file") == "fork"
        assert u.git(origin, "show", second["commit"] + ":next") == "another fork change"
        assert u.git(origin, "show", second["commit"] + ":next-upstream") == "another upstream change"
        assert second["upstream"] == next_upstream
        u.git(src, "fetch", str(origin), "refs/heads/main")
        u.git(src, "switch", "--detach", "FETCH_HEAD")
        # An upstream/fork conflict cannot change the public branch or install a third build.
        (src / "file").write_text("conflicting main")
        u.git(src,"commit","-am","incompatible change"); u.git(src,"push",str(origin),"HEAD:main")
        expect("conflict")
        assert second["commit"]==u.remote_ref(u.FORK,"refs/heads/"+u.BRANCH)
        fork_with_conflict = u.remote_ref(u.FORK, "refs/heads/main")
        u.git(src, "switch", "--detach", next_upstream)
        (src / "file").write_text("incompatible upstream change")
        u.git(src, "commit", "-am", "conflict with fork main")
        u.git(src, "push", str(upstream), "HEAD:main")
        try:
            u.update(cfg)
        except u.Stop as exc:
            assert exc.phase == "conflict" and "upstream main and fork main" in str(exc) and "file" in str(exc)
        else:
            raise AssertionError("Conflicting upstream change accepted")
        assert u.remote_ref(u.FORK, "refs/heads/main") == fork_with_conflict
        assert u.remote_ref(u.FORK, "refs/heads/" + u.BRANCH) == second["commit"]
        assert (u.ROOT / "installs").read_text().splitlines().count("install") == 2
        u.git(src, "fetch", str(origin), "refs/heads/" + u.BRANCH)
        u.git(src, "switch", "--detach", "FETCH_HEAD")
        (src / "unmerged-code").write_text("keep this manual fix")
        u.git(src, "add", "unmerged-code")
        u.git(src, "commit", "-m", "Manual fork release fix", "-m", "Fork-Source: " + second["source"])
        u.git(src, "push", str(origin), "HEAD:refs/heads/" + u.BRANCH)
        expect("failed")
        assert u.git(origin, "show", u.BRANCH + ":unmerged-code") == "keep this manual fix"
        assisted_release(folder, bun)
        out.parent.mkdir(parents=True,exist_ok=True)
        u.save(out,{"realGit":True,"services":"local fixtures","cases":["two permanent branches", "public tag stays fixed during staging", "same SHA retry", "moved staging head blocks CI and publication", "unrelated runtime release ignored",
               "superseded failed first build fast-forwards", "sealed legacy candidate rebuilt with its input revisions",
               "old branch protected by permanent history before deletion", "denied legacy cleanup retried on click",
               "moved and unrelated branches retained", "no-push probe","untagged upstream main synced to fork main","CI failure","both upload routes fail safely","conflicting digest retained","public release blocks recovery","zero-size starter recovery","hidden full-size ZIP starter","individual starter changed before delete","lost primary and alternate receipts","same-run alternate upload",
               "one update publishes and installs after fallback with truncated receipt","resume same commit","install after published checksums","up to date","second release preserves both fork changes","merge conflict","fork and upstream conflict preserves branches","no WhatsApp or Signal clearance","manual release branch change preserved",
               "app model combines two edits", "ambiguous and invalid model replies use owner window",
               "failed checks retain public main", "retry retains approved resolution", "owner chooses file for delete/edit conflict", "wrong click nonce refused", "credential-like hunk stays local", "independent word edits combine on one line"]})
    print(out)


if __name__ == "__main__":
    main()
