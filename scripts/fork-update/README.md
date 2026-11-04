# One-click Mac updates

The owner of this Apple silicon Mac can use **Settings → Updates → Update this Mac** on the local server.
A click fetches `main` from both `nightly-labs/openbot` and `its-bash33r-here/openbot`.
It merges upstream into the fork in a private checkout and checks that the result merges with
the saved Mac changes. A clean merge advances the fork's `main` with a normal push.
The release includes the upstream commits, the fork commits and the local Mac changes.

1. **Check.** The runner pushes the candidate to the permanent `fork/mac-release` branch and requests hosted CI on that exact commit.
2. **Build.** On success the Mac builds, signs, notarizes, and verifies the app, update ZIP, DMG and
   manifest. Signing uses the team's Developer ID in the login Keychain and the signed-in Xcode account.
3. **Install.** OpenBot waits for work to finish and quits. The runner backs up the old app, profile and
   workspaces and rehearses the upgrade on copies. It checks that the tested candidate still owns
   `fork/mac-release`, creates a numeric
   Git tag, checks every draft asset against its local SHA-256, publishes the release, checks the
   public Mac feed, exchanges the app bundles and reopens OpenBot. Other installs on the fork feed
   can get the published package with the regular update checker.

The fork keeps two branches. `main` holds synced source. `fork/mac-release` holds the Mac candidate
being tested and can move before the update is ready to install. The published version tag and
GitHub Release identify the downloadable build. A failed check leaves the previous release available.

After an interruption, open **Settings → Updates** to see which step stopped. Select **Retry** to resume. A changed file, failed check, inaccessible
feed or signing error stops installation. The runner advances the permanent branches with normal pushes.
Older updater records can lack the failed step. OpenBot shows when those details are unavailable.
Keep both the old bundle and its matching profile in **OpenBot Fork Update Recovery** after a schema change.

## One-time owner setup

Run `python3 scripts/fork-update/configure.py` in a committed checkout with Python 3.11 or later.
This installs fixed commands and pins the seed commit under
`~/Library/Application Support/OpenBot Fork Updates`. The existing fork GitHub login needs release,
workflow and Git push access. The Mac must have Bun, Node 24, Xcode and its Developer ID signing key.
The first installation of the button uses a local signed bootstrap package. The previous 0.59.0 app
must finish its work and quit once. Later releases use the idle handoff. A secondary local server
must stop before the app can be exchanged.

Use `"$HOME/Library/Application Support/OpenBot Fork Updates/update" --probe` to test both merges
without pushing, building or installing. The probe can write a local Git cache and
status. Use `--status` for a read-only phase query.

## Conflicts and retries

For an overlap in ordinary source or documentation, OpenBot can use its selected model to combine
the two edits. Only the conflicting section goes to the provider. This uses the owner's normal model
quota. The model has no tools. The runner checks the proposed lines and keeps the surrounding file.
The no-push probe never calls a model.

When the model cannot preserve both edits, the owner chooses between the two versions in an OpenBot
window or pauses the update. Sensitive files and structural conflicts go directly to this window.
A choice for an entire file is labeled. The temporary Git checkout holds the result until the
candidate passes CI, signing and copied-profile checks. The updater then advances fork main.
The first update from 0.59.1 needs a clean Git merge to install the in-app merge assistant.

This Mac release can include commits before upstream tags a release. It can include WhatsApp
changes before upstream publishes a review or phone test and client features before the
live Signal service supports them. The normal CI, signing, package and copied-profile checks
still run. A failed build or check stops publication and installation.

Retry to resume an interrupted release. New upstream or fork commits supersede an unsealed
candidate. A retry reuses the signed files if the fork still contains the expected source.

If a normal GitHub CLI asset upload fails or times out, the runner checks the same fork release ID,
tag, asset name and signed SHA-256. A matching upload can continue even if its response was lost.
For a missing asset it tries one other IPv4 address in the same click. It checks a public DNS answer
against GitHub's published web ranges and excludes the Mac's normal addresses. Only the public DNS
query goes to Google. HTTPS still verifies uploads.github.com. Each upload has a two-hour limit.
The runner reads the draft’s dedicated asset list because the release summary can hide failed uploads.
Before deleting a starter, it fetches the individual asset by ID and checks its name, state, missing
digest, and size. The reported size must be zero or exactly the signed file’s local size.
If the route fails or an asset conflicts, the update stops before publication and installation.
Keep the sealed files and draft for the existing Retry action.

## Rehearse without publishing

Run `python3 scripts/fork-update/release-e2e.py` for a real Git rehearsal with local substitutes for
GitHub, signing and installation. Its report is under `.openbot-build/fork-release-e2e/`.
Run `bun scripts/fork-update-click-e2e.ts` for the trusted click and keyboard boundary.
