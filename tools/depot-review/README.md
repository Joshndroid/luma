# depot-review

The Depot submission pipeline. It claims developer submissions from the Hub, checks them, runs them in a throwaway Luma VM, and reports back. It publishes approved builds to the `luma` Flatpak remote, withdraws releases, and signs the Depot catalogue.

It runs on the build host as `luma-depot-review.service`. Configuration is `/etc/luma/depot-review.json`; see `depot_review/config.py` for every setting and its default.

## Checks (every new app and every update)

| Check | How |
|---|---|
| Format and integrity | The digest is compared with the Hub's record. The package is opened with its own format's tools in the review container (no network, no capabilities): `flatpak build-import-bundle` and `ostree`, `rpm`, `dpkg-deb` or `unsquashfs`. |
| Identity | The app id matches the listing. Reserved prefixes and ids Luma ships are refused (`config/policy.json`, `packaging/flatpak/apps/titles.json`, Luma beta/nightly refs). |
| Runtime | Must be one of `config/policy.json` `runtimes`. |
| Architecture | x86_64 is required; aarch64 is welcome. |
| Malware | ClamAV scans the upload and every file it would install. Signatures are refreshed with `freshclam` every 3 hours. |
| Permissions | What the package can reach, in Depot's eleven words (`depot_review/words.py`). Flatpak reach comes from `scripts/depot/flatpak-permissions.py`. A universal package always reaches Files and Internet, plus System or Devices for services, administrator rules, udev rules or maintainer scripts. Any word the listing doesn't declare fails, with a plain message. |
| Metadata | Summary of 60 characters or fewer. Icon is an SVG or a square PNG of at least 512 px. Desktop screenshots are at least 1180 × 698; phone screenshots are 1170 × 2532. An app needs at least one desktop screenshot. |
| VM run | Install, launch, screenshot and uninstall in a copy-on-write overlay of a Luma base disk (`depot_review/vm.py`). The launch screenshot goes to the Hub. |

Results are `passed` (to Review), `needs_changes` (back to the developer, with the failed checks' messages), `attention` (only internal problems; Luma looks at it), or `couldnt_run`.

`couldnt_run` means Depot's own checks didn't run: the VM didn't boot or its desktop didn't start, its screen was off, the host had no room, or the Hub or a container timed out. Those checks carry `infrastructure: true` and a `reason`, and are never findings against the app. The service retries the whole check after each of `infra_retry_delays` (5, 15 and 45 min), and only then reports `couldnt_run`, with an `ALERT depot-review:` line at error priority in the service journal. A developer finding in the same run still wins (`needs_changes`).

The review VM boots only with headroom: it takes the review lane `vm_lock` (`/var/lock/luma-heavy-review.lock`, one review VM beside one luma-heavy job, like the conform lane), then waits up to `vm_slot_wait_seconds` until `vm_memory_mib + vm_reserve_mib` is available, the 1-minute load is under 0.75 per CPU and `luma-heavy.slice` isn't frozen by the thermal guard. The boot timeout starts when qemu does. The review desktop's dconf policy (in the base, and re-applied each run) turns off idle, dim, lock and blanking, and each run holds an idle inhibitor and wakes the display before it watches the screen.

Android stays off in the review VM: the kickstart and `prepare-base` mask `luma-android-initialize.service`, `waydroid-container.service` and the user prewarm/session units, and every run stops them for that boot. `prepare-base` works on an overlay and commits it into the base only after the verify boot. It refuses a base that is already prepared (`/etc/luma-depot-review/prepared`; its boots run first-boot jobs inside the base, so build a new base instead, or pass `--force`) and a base other processes have open (disks backed by it would break).

When a launch fails, the run saves the session state (`session.log`) and launches the control app (`control_bundle`, Depot Hello) on the same VM; if that fails too, the launch is `couldnt_run`. The launch watch ends after 180 s of wall time; an app unit still in Flatpak's first-run ldconfig then is `couldnt_run` (`flatpak_ldconfig_timeout`).

`depot-review recheck JOB/submission.json ...` re-runs the checks of submissions that were already reported and prints each verdict. It reports nothing to the Hub.

AppImages are opened and scanned, then answered "coming soon". `.rpm`/`.deb` are fully checked but don't publish yet.

## Publishing and withdrawal

On approval, each reviewed bundle becomes an unsigned repository holding exactly `app/<id>/<arch>/stable`. It is sealed as a signer snapshot and published with the existing chain, from a sealed control snapshot of this checkout:

`seal-signing-control.py seal-inputs` → `tools-container.sh --signing` → `publish-app.sh --third-party` → `update-remote.sh` → `sync-remote.sh`

- `--third-party` keeps developers' apps on the stable branch.
- It refuses any id Luma owns.
- Luma's own publishes refuse ids a third party holds.

`withdraw-app.sh` moves a ref forward to a new signed commit with the previous release's content. If there is no previous release, it re-signs the ref as end-of-life. Both take `/srv/luma-build/depot-publication.lock`.

## Catalogue

The Hub is the only source of Depot listings: Luma's apps, Flathub and community listings, publisher packages and developer submissions are all edited in admin (Depot > Apps). Every round (`catalogue_seconds`) publishes two documents from it:

- Catalogue v1 (`catalogue.py`), read by the redesigned Depot and the installer's third-party path: signed with the `depot-catalogue-v1` minisign key (`/srv/luma-build/depot-keys/minisign/`) and published to `https://dl.simplyluma.com/depot/catalogue/v1/catalogue.json` and `.minisig`. Pictures are copied to `https://media.simplyluma.com/depot/<app-id>/<sha256>.<ext>`.
- catalog-4 and catalog-3 (`catalog4.py`), read by installed Depots: `scripts/depot/sign-catalog.py` from the deployed checkout fetches the Hub's snapshots, copies new listing pictures into `$DEPOT_ROOT/site/media/listings`, validates with the installed Depot's rules, signs with the catalog key and uploads with `sync-remote.sh` (pictures first) to `https://dl.simplyluma.com/catalog/`. It skips a round while the publication lock is held. `scripts/depot/run-catalog-signer.sh` is the same run by hand. `publish_catalog4: false` turns it off.

## Commands

```
python3 -m depot_review serve
python3 -m depot_review check FILE --app-id ID [--declare Internet Files] [--no-vm]
python3 -m depot_review publish --id SUBMISSION --app-id ID BUNDLE:ARCH ...
python3 -m depot_review withdraw --id REQUEST --app-id ID --arch ARCH [--target COMMIT]
python3 -m depot_review catalogue [--force]
python3 -m depot_review keygen
python3 -m depot_review.vm build-base --iso LUMA.iso --out DIR
```

`testapp/` is Depot Hello, a one-file GTK app used for the end-to-end test. Build it with `testapp/build.sh VERSION ARCH OUT.flatpak`.

## Tests

```
python3 -m unittest discover -s tools/depot-review/tests
```
