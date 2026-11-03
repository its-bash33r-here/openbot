#!/usr/bin/env python3
"""Build, notarize and verify a fork release. This command never publishes to GitHub."""

import argparse
import base64
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import sys
import tempfile
import time

TEAM = "QC5HNMH72Y"
VENDOR_TEAM = "YCK386LBJ7"
DRIVER = Path("Contents/Resources/cua-driver/darwin/arm64/cua-driver")
BUNDLE_ID = "app.openbot.desktop"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def file_digest(path, algorithm):
    with path.open("rb") as stream:
        digest = hashlib.new(algorithm)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest


def signing_identity():
    identities = run("security", "find-identity", "-v", "-p", "codesigning",
                     capture_output=True, text=True).stdout
    for fingerprint, name in re.findall(r'\b([0-9A-F]{40}) "([^"]+)"', identities):
        if name.startswith("Developer ID Application: ") and name.endswith(f"({TEAM})"):
            return fingerprint, name
    raise ValueError(f"Install a Developer ID Application identity for team {TEAM}.")


def signed_hash(path, team):
    detail = run("codesign", "-dvvv", path, capture_output=True, text=True).stderr
    fields = dict(line.split("=", 1) for line in detail.splitlines() if "=" in line)
    if (fields.get("TeamIdentifier") != team or not fields.get("CDHash")
            or "Authority=Developer ID Application: " not in detail):
        raise ValueError(f"Unexpected signature on {path.name}.")
    return fields["CDHash"]


def verify_ticket(app, hashes):
    run("codesign", "--verify", "--deep", "--strict", app)
    run("xcrun", "stapler", "validate", app)
    verdict = run("spctl", "--assess", "--type", "execute", "--verbose=4", app,
                  capture_output=True, text=True)
    if "source=Notarized Developer ID" not in verdict.stderr:
        raise ValueError("Gatekeeper did not recognize the notarized app.")
    if (signed_hash(app, TEAM), signed_hash(app / DRIVER, VENDOR_TEAM)) != hashes:
        raise ValueError("The application or the vendor driver changed during packaging.")


def notarize_with_xcode(work, shipment, version, fingerprint, identity):
    # Xcode re-signs nested apps on export. A ZIP inside a signed enclosure lets
    # Apple issue a ticket for the original app, including the vendor-signed driver.
    name = "OpenBot Update Package"
    archive = work / "Submission.xcarchive"
    app = archive / "Products/Applications" / f"{name}.app"
    executable = app / "Contents/MacOS/OpenBotUpdatePackage"
    executable.parent.mkdir(parents=True)
    resources = app / "Contents/Resources"
    resources.mkdir()
    # Same volume, private attempt directory. The finished ZIP is sealed into the enclosure.
    os.link(shipment, resources / shipment.name)
    main = work / "enclosure.c"
    main.write_text("int main(void) { return 0; }\n")
    run("xcrun", "clang", "-arch", "arm64", "-mmacosx-version-min=13.0", main, "-o", executable)
    bundle_id = f"{BUNDLE_ID}.notarization"
    numeric_version = re.split(r"[-+]", version)[0]
    info = {
        "CFBundleIdentifier": bundle_id, "CFBundleName": name,
        "CFBundleExecutable": executable.name, "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0", "CFBundleShortVersionString": numeric_version,
        "CFBundleVersion": "1", "LSMinimumSystemVersion": "13.0", "DTPlatformName": "macosx",
        "CFBundleSupportedPlatforms": ["MacOSX"], "LSBackgroundOnly": True,
    }
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    run("codesign", "--sign", fingerprint, "--timestamp", "--options", "runtime", app)
    archive_info = {
        "ArchiveVersion": 2, "CreationDate": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
        "Name": name, "SchemeName": name, "ApplicationProperties": {
            "ApplicationPath": f"Applications/{name}.app", "CFBundleIdentifier": bundle_id,
            "CFBundleShortVersionString": numeric_version, "CFBundleVersion": "1",
            "Architectures": ["arm64"], "SigningIdentity": identity, "Team": TEAM,
        },
    }
    (archive / "Info.plist").write_bytes(plistlib.dumps(archive_info))
    options = work / "export-options.plist"
    options.write_bytes(plistlib.dumps({
        "method": "developer-id", "destination": "upload", "teamID": TEAM,
        "signingStyle": "manual", "signingCertificate": fingerprint, "manageAppVersionAndBuildNumber": False,
    }))
    # Xcode prints account and submission information. Keep its full output out of release logs.
    with tempfile.TemporaryFile() as log:
        run("xcodebuild", "-exportArchive", "-archivePath", archive, "-exportOptionsPlist", options,
            "-exportPath", work / "upload", stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 30 * 60
    while True:
        check = subprocess.run(["xcodebuild", "-exportNotarizedApp", "-archivePath", archive,
                                "-exportPath", work / "approved"], capture_output=True, text=True)
        if check.returncode == 0:
            return
        if "processing" not in (check.stdout + check.stderr).lower():
            raise ValueError("Xcode could not export the notarized enclosure. Check the submission in Xcode.")
        if time.monotonic() >= deadline:
            raise ValueError("Apple is still processing the submission. Check its status in Xcode.")
        time.sleep(20)


@contextlib.contextmanager
def mounted_image(image, mount):
    mount.mkdir()
    run("hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, image)
    try:
        yield mount
    finally:
        run("hdiutil", "detach", mount)


def read_yaml(source, path):
    # Use the same YAML parser as the repository's artifact verifier.
    reader = "console.log(JSON.stringify(require('yaml').parse(await Bun.file(Bun.argv[1]).text())))"
    return json.loads(run("bun", "-e", reader, path, cwd=source, capture_output=True, text=True).stdout)


def verify_manifest(source, dist, version, archives):
    manifest = read_yaml(source, dist / "latest-mac.yml")
    if manifest.get("version") != version or manifest.get("path") != archives[0].name:
        raise ValueError("Update manifest has the wrong version or primary ZIP.")
    files = {item["url"]: item for item in manifest["files"]}
    if set(files) != {path.name for path in archives}:
        raise ValueError("Update manifest must describe this ZIP and DMG.")
    for path in archives:
        if files[path.name]["size"] != path.stat().st_size or not Path(f"{path}.blockmap").stat().st_size:
            raise ValueError(f"Incorrect size or empty blockmap for {path.name}.")
        digest = file_digest(path, "sha512").digest()
        if files[path.name]["sha512"] != base64.b64encode(digest).decode():
            raise ValueError(f"Manifest checksum mismatch for {path.name}.")


def package(source, output):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("Packaging requires a native Apple silicon Mac.")
    version = json.loads((source / "package.json").read_text())["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version):
        raise ValueError("Expected a release version in package.json.")
    if any(directory in (output.resolve(), *output.resolve().parents)
           for directory in (Path("/Applications"), Path.home() / "Applications")):
        raise ValueError("Choose a staging directory outside Applications.")
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("The output directory must be absent or empty.")
    fingerprint, identity = signing_identity()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fork-package-", dir=output.parent) as attempt:
        work = Path(attempt)
        dist = work / "dist"
        signed = work / "signed"
        app = dist / "mac-arm64/OpenBot.app"
        stem = f"OpenBot-{version}-arm64"
        zip_file = dist / f"{stem}.zip"
        dmg_file = dist / f"{stem}.dmg"
        run("bun", "install", "--frozen-lockfile", cwd=source)
        for task in ("voice:prepare-runtime", "prepare:remote-desktop-runtime",
                     "verify:remote-desktop-runtime", "prepare:cua-driver", "build"):
            run("bun", "run", task, cwd=source)
        builder = source / "node_modules/.bin/electron-builder"
        common = ["--arm64", "--publish", "never", f"-c.mac.identity={fingerprint}",
                  "-c.mac.notarize=false", "-c.forceCodeSigning=true"]
        run(builder, "--mac", "zip", *common, f"-c.directories.output={signed}", cwd=source)
        signed_app = signed / "mac-arm64/OpenBot.app"
        plist = plistlib.loads((signed_app / "Contents/Info.plist").read_bytes())
        if (plist.get("CFBundleIdentifier"), plist.get("CFBundleShortVersionString"),
                plist.get("ElectronTeamID")) != (BUNDLE_ID, version, TEAM):
            raise ValueError("The bundle does not match the fork, version and signing team.")
        config = read_yaml(source, signed_app / "Contents/Resources/app-update.yml")
        if (config.get("provider"), config.get("owner"), config.get("repo"), config.get("updaterCacheDirName")) != (
                "github", "its-bash33r-here", "openbot", "openbot-fork-updater"):
            raise ValueError("The signed app points to an unexpected update feed or cache.")
        entitlements = plistlib.loads(run("codesign", "--display", "--entitlements", "-", "--xml",
                                          signed_app, capture_output=True).stdout)
        expected = plistlib.loads((source / "build/entitlements.mac.plist").read_bytes())
        if (any(entitlements.get(key) != value for key, value in expected.items())
                or entitlements.get("com.apple.developer.associated-domains")
                or entitlements.get("com.apple.security.get-task-allow")):
            raise ValueError("The signed app's entitlements do not match this fork.")
        hashes = signed_hash(signed_app, TEAM), signed_hash(signed_app / DRIVER, VENDOR_TEAM)
        if hashes[1] != signed_hash(source / "build/cua-driver/darwin/arm64/cua-driver", VENDOR_TEAM):
            raise ValueError("The packager changed the vendor-signed driver.")
        run("codesign", "--verify", "--deep", "--strict", signed_app)
        shipment = signed / zip_file.name
        profile = os.environ.get("OPENBOT_NOTARY_PROFILE")
        if profile:
            reply = run("xcrun", "notarytool", "submit", shipment, "--keychain-profile", profile,
                        "--wait", "--timeout", "30m", "--output-format", "json", capture_output=True, text=True)
            if json.loads(reply.stdout).get("status") != "Accepted":
                raise ValueError("Apple did not accept the signed ZIP.")
        else:
            notarize_with_xcode(work, shipment, version, fingerprint, identity)
        # Keep the verified original. Xcode may change signatures on the enclosure it exports.
        app.parent.mkdir(parents=True)
        signed_app.rename(app)
        run("xcrun", "stapler", "staple", app)
        verify_ticket(app, hashes)
        run(builder, "--mac", "zip", "dmg", *common, "--prepackaged", app,
            f"-c.directories.output={dist}", "-c.dmg.sign=true", cwd=source)
        # The repository's verifier reads ./dist. Run it in our private staging root.
        run("bun", source / "scripts/verify-update-artifacts.ts", "macos", cwd=work)
        verify_manifest(source, dist, version, (zip_file, dmg_file))
        unpack = work / "unpacked"
        run("ditto", "-x", "-k", zip_file, unpack)
        verify_ticket(unpack / "OpenBot.app", hashes)
        with mounted_image(dmg_file, work / "mounted") as mount:
            verify_ticket(mount / "OpenBot.app", hashes)
        # This verifier opens two app processes with a temporary profile and mock keychain.
        # Give them their own home, cache and workspaces too.
        home = work / "smoke-home"
        home.mkdir()
        smoke_tmp = work / "smoke-tmp"
        smoke_tmp.mkdir()
        smoke_env = {
            "PATH": os.environ["PATH"], "HOME": str(home), "CFFIXED_USER_HOME": str(home),
            "TMPDIR": str(smoke_tmp), "LANG": "en_US.UTF-8", "OPENBOT_AUTH_API_URL": "http://127.0.0.1:9",
        }
        run("bun", "scripts/verify-macos-package.ts", app, cwd=source, env=smoke_env)
        assets = (zip_file, dmg_file, Path(f"{zip_file}.blockmap"), Path(f"{dmg_file}.blockmap"),
                  dist / "latest-mac.yml")
        result = work / "verified"
        result.mkdir()
        checksums = "".join(f"{file_digest(p, 'sha256').hexdigest()}  {p.name}\n" for p in assets)
        (result / "SHA256SUMS-macos.txt").write_text(checksums)
        for path in (*assets, app.parent):
            path.rename(result / path.name)
        result.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_worktree", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    try:
        package(args.source_worktree.resolve(), args.output_dir.absolute())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Mac release packaging failed: {error}\n")
    print(f"Verified Mac release files in {args.output_dir.absolute()}")


if __name__ == "__main__":
    sys.exit(main())
