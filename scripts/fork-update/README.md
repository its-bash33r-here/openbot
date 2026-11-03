# One-click Mac updates

The owner of this Apple silicon Mac can use **Settings → Updates → Update this Mac** on the local server.
A click fetches the latest published stable release from `nightly-labs/openbot` and the latest
`its-bash33r-here/openbot` main. It merges both with the saved fork changes in a private checkout.
The first fork update includes the previously integrated upstream changes. The button runs on demand.

1. **Check.** The runner stops on a merge conflict or an unmet release requirement. For an eligible
   candidate it pushes an immutable candidate branch and requests hosted CI on that exact commit.
2. **Build.** On success the Mac builds, signs, notarizes, and verifies the app, update ZIP, DMG and
   manifest. Signing uses the team's Developer ID in the login Keychain and the signed-in Xcode account.
3. **Install.** OpenBot waits for work to finish and quits. The runner backs up the old app, profile and
   workspaces and rehearses the upgrade on copies. It updates `fork/mac-release`, creates a numeric
   Git tag, checks every draft asset against its local SHA-256, publishes the release, checks the
   public Mac feed, exchanges the app bundles and reopens OpenBot. Other installs on the fork feed
   can get the published package with the regular update checker.

A second click after interruption resumes the saved commit. A changed file, failed check, inaccessible
feed or signing error stops installation. The runner never force-pushes to the fork.
The owner can find the last phase and a short reason in the private `status.json`.
Keep both the old bundle and its matching profile in **OpenBot Fork Update Recovery** after a schema change.

## One-time owner setup

Run `python3 scripts/fork-update/configure.py` in a committed checkout with Python 3.11 or later.
This installs fixed commands and pins the seed commit under
`~/Library/Application Support/OpenBot Fork Updates`. The existing fork GitHub login needs release,
workflow and Git push access. The Mac must have Bun, Node 24, Xcode and its Developer ID signing key.
The first installation of the button uses a local signed bootstrap package. The previous 0.59.0 app
must finish its work and quit once. Later releases use the idle handoff. A secondary local server
must stop before the app can be exchanged.

Use `"$HOME/Library/Application Support/OpenBot Fork Updates/update" --probe` to check release
prerequisites without pushing, building or installing. The probe can write a local Git cache and
status. Use `--status` for a read-only phase query.

## Upstream release prerequisites

The public Signal health endpoint must identify a commit with the same `remote` and Signal protocol
files as the candidate. If the candidate includes the WhatsApp crypto code, the reader can use the upstream stable release notes or
`packages/whatsapp/release-evidence.json` in a **published stable upstream tag**. It must name
a full `reviewedCommit` covered by that tag and include `legalReview` and `realPhoneTest` URLs for
the upstream GitHub project. The WhatsApp source and vendor trees must match the reviewed commit.
For release notes, the reader accepts explicit passing results for **WhatsApp legal review** and
**WhatsApp two-phone test** on the same source. A stable tag by itself does not confirm those results.
Upstream has not agreed on an evidence format. If upstream records
clearance in another form, adapt the reader to the actual published evidence. Until then the update
reports a hold. No extra approval dialog is part of a successful release.

The runner preserves your checkout on a conflict. Merge the conflicting change into fork main and retry.
After a CI failure, retry to rerun the failing jobs. A new stable tag, fork main commit or configured
seed supersedes a failed, unsigned candidate. A published tag and its assets are immutable.

## Rehearse without publishing

Run `python3 scripts/fork-update/release-e2e.py` for a real Git rehearsal with local substitutes for
GitHub, signing and installation. Its report is under `.openbot-build/fork-release-e2e/`.
Run `bun scripts/fork-update-click-e2e.ts` for the trusted click and keyboard boundary.
