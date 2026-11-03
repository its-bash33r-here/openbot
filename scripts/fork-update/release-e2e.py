"""Exercise the release runner with real local Git remotes and disposable external-service fixtures."""
import json
import os
from pathlib import Path
import sys
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
import hashlib, json, pathlib, sys
a=sys.argv[1:]
p=pathlib.Path(db)
d=json.loads(p.read_text())
def flush(): p.write_text(json.dumps(d))
def assets(): return [{"name":k,"digest":v} for k,v in d["assets"].items()]
if a[0]=="api":
    if "nightly-labs" in a[1]: print(json.dumps({"draft":False,"prerelease":False,"tag_name":"v1.0.0"}))
    else: print(json.dumps({"draft":d["draft"],"prerelease":False,"tag_name":d["version"],"assets":assets()}))
elif a[:2]==["run","list"]:
    sha=a[a.index("--commit")+1]; d["checked"]=sha; flush()
    print(json.dumps([] if d["ci"]=="missing" else [{"headSha":sha,"databaseId":12,"status":"completed","conclusion":d["ci"]}]))
elif a[:2]==["workflow","run"]: d["ci"]="failure"; flush()
elif a[:2]==["run","view"]:
    if a[-1]==".attempt": print(1)
    else:
        jobs=["Check","Tests (desktop 1/2)","Tests (desktop 2/2)","Browser smoke","Signal E2E","Team API and reverse actions E2E"]
        print(json.dumps({"headSha":d["checked"],"conclusion":"success","jobs":[{"name":n,"conclusion":"success"} for n in jobs]}))
elif a[:2]==["run","rerun"]: d["ci"]="success"; flush()
elif a[:2]==["release","view"]:
    if not d["release"]: sys.exit(1)
    print(json.dumps({"isDraft":d["draft"],"isPrerelease":False,"assets":assets()}))
elif a[:2]==["release","create"]:
    assert not d["release"] and "--draft" in a
    d.update(release=True,version=a[2]); flush()
elif a[:2]==["release","upload"]:
    path=pathlib.Path(a[3]); assert path.name not in d["assets"]
    if len(d["assets"])==2 and d["interrupt"]: d["interrupt"]=False; flush(); sys.exit(1)
    d["assets"][path.name]="sha256:"+hashlib.sha256(path.read_bytes()).hexdigest(); flush()
elif a[:2]==["release","edit"]:
    assert len(d["assets"])==6 and d["draft"]
    d["draft"]=False; flush()
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
           "p.open('a').write(('prepare' if '--prepare' in sys.argv else 'install')+'\\n')")


def main():
    os.umask(0o077)
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
        guard = u.release_guard
        def held(*_):
            raise u.Stop("held","Fixture upstream hold")
        u.release_guard = held
        def expect(phase):
            try:
                u.update(cfg)
            except u.Stop as e:
                assert e.phase == phase, (e.phase, str(e))
                return
            raise AssertionError("Expected " + phase)
        expect("held")
        assert u.remote_ref(u.FORK,"refs/heads/"+u.BRANCH) is None and not (u.ROOT/"pending.json").exists()
        u.release_guard = lambda *_: None
        expect("failed")  # Hosted CI fails before signing or tagging.
        tx=u.read(u.ROOT/"pending.json")
        assert not (u.ROOT/tx["batch"]/"release").exists() and not u.remote_ref(u.FORK,"refs/tags/1.0.1")
        expect("failed")  # Two assets reached the draft. The next click must finish that draft.
        assert len(u.read(folder/"github.json")["assets"])==2 and u.read(folder/"github.json")["draft"]
        assert "install" not in (u.ROOT/"installs").read_text()
        u.update(cfg)
        assert u.read(u.ROOT/"status.json")["phase"]=="complete" and not u.read(folder/"github.json")["draft"]
        assert tx["commit"]==u.remote_ref(u.FORK,"refs/tags/1.0.1")==u.remote_ref(u.FORK,"refs/heads/"+u.BRANCH)
        assert (u.ROOT/"installs").read_text().splitlines()==["prepare","prepare","install"]
        u.update(cfg)
        assert u.read(u.ROOT/"status.json")["phase"]=="upToDate"
        # Advance fork main and verify that release history fast-forwards on a second signed version.
        u.git(src, "switch", "--detach", "v1.0.0")
        (src / "next").write_text("another fork change")
        u.git(src, "add", "next")
        u.git(src, "commit", "-m", "next change")
        u.git(src, "push", str(origin), "HEAD:main")
        # The service fixture now serves the new tag and commit's CI and release records.
        service = u.read(folder / "github.json")
        service.update(assets={}, release=False, draft=True)
        u.save(folder / "github.json", service)
        u.update(cfg)
        second = u.read(u.ROOT / "completed.json")
        assert second["version"] == "1.0.2" and second["previous"] == tx["commit"]
        assert u.git(origin, "merge-base", "--is-ancestor", tx["commit"], second["commit"], check=False) == 0
        assert u.git(origin, "show", second["commit"] + ":file") == "fork"
        assert u.git(origin, "show", second["commit"] + ":next") == "another fork change"
        # An upstream/fork conflict cannot change the public branch or install a third build.
        (src / "file").write_text("conflicting main")
        u.git(src,"commit","-am","incompatible change"); u.git(src,"push",str(origin),"HEAD:main")
        expect("conflict")
        assert second["commit"]==u.remote_ref(u.FORK,"refs/heads/"+u.BRANCH)
        # Exercise the real WhatsApp evidence gate on a tagged fixture with no review.
        (src/"packages/whatsapp/src/signal").mkdir(parents=True)
        (src/"packages/whatsapp/src/signal/crypto").write_text("fixture")
        u.git(src,"add","."); u.git(src,"commit","-m","WhatsApp without release evidence")
        try:
            guard(src, "HEAD", "v1.0.0")
        except u.Stop as e:
            assert e.phase == "held"
        else:
            raise AssertionError("Missing WhatsApp review allowed")
        u.git(src, "fetch", str(origin), "refs/heads/" + u.BRANCH)
        u.git(src, "switch", "--detach", "FETCH_HEAD")
        (src / "unmerged-code").write_text("keep this manual fix")
        u.git(src, "add", "unmerged-code")
        u.git(src, "commit", "-m", "Manual fork release fix", "-m", "Fork-Source: " + second["source"])
        u.git(src, "push", str(origin), "HEAD:refs/heads/" + u.BRANCH)
        expect("failed")
        assert u.git(origin, "show", u.BRANCH + ":unmerged-code") == "keep this manual fix"
        out.parent.mkdir(parents=True,exist_ok=True)
        u.save(out,{"realGit":True,"services":"local fixtures","cases":["upstream hold","CI failure","partial upload",
               "resume same commit","install after published checksums","up to date","second release preserves both fork changes","merge conflict","missing WhatsApp evidence","manual release branch change preserved"]})
    print(out)


if __name__ == "__main__":
    main()
