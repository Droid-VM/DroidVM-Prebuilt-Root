# DroidVM-Prebuilt-Root

Build pipeline for DroidVM's native prebuilt artifacts. It compiles the
third-party daemons, merges them with the manually-curated VM runtime, and packs
the artifacts that the main [DroidVM](https://github.com/Droid-VM/DroidVM) app
ships — published to
[DroidVM-Prebuilts](https://github.com/Droid-VM/DroidVM-Prebuilts).

## Layout

```
auto-build.toml      source-of-truth config (repos, branches, build cmds, dist, install)
auto-build.py        builds repos + packs artifacts (replaces the old 7z step)
resolv_dep.py        prints apt deps for the repos that changed (for the CI install step)
prebuild_common.py   shared helpers incl. needs_rebuild() (used by both .py above)
hash-gen.py          sha256 manifest generator (called by auto-build.py)
arch.lst             architectures to build, one per line
auto-build/<dir>/    source checkouts (contents gitignored; cloned on demand)
manual-build/<arch>/ runtime staging tree:
                       usr/  -> committed VM runtime (crosvm, qemu, kernel, libs)
                       bin/  -> auto-built daemons merged in (gitignored)
```

## Caching & conditional installs

`needs_rebuild()` (in `prebuild_common.py`) decides, per repo, whether the
`auto-build/<dir>` checkout diverges from its pinned/branch commit — returning
`True` (conservatively) for force, a changed commit/URL, the wrong branch, or a
missing/broken/non-git checkout. It's shared so two things stay in lockstep:

* **`auto-build.py`** skips the compile when a repo is unchanged (reusing the
  cached `dist/` outputs) and, when it *is* changed, converges the checkout to
  upstream in place (`fetch --force` + `reset --hard` + `clean -ffdx`), falling
  back to `rm -rf` + `git clone --depth 1` if the checkout is too broken to fix.
* **`resolv_dep.py`** prints the union of the `install:` packages of only the
  changed repos, so the CI step installs nothing on an all-cached run.

Combined with the rolling GitHub Actions cache (the `auto-build/` checkouts +
`~/.cargo`/`~/go` caches) and reproducible archives (normalized mtimes), an
unchanged push does almost no work and publishes nothing.

## Artifacts (per arch)

| File | Compression | Consumed | Contents |
|------|-------------|----------|----------|
| `prebuilt-<arch>.tar.xz` | tar + xz (`org.tukaani:xz` on device) | first app run, extracted into the data dir | the whole `manual-build/<arch>/` tree (`usr/…`, `bin/…`) |
| `prebuilt-<arch>-comptime.zip` | deflate zip (`java.util.zip` in Gradle) | Gradle build time, unzipped into the APK's `lib/<arch>/` | files that must be executable from `nativeLibraryDir` (currently `liblbx.so`) |
| `prebuilt-<arch>.json` | — | first app run | sha256 manifest of the runtime tree |

The tar is plain **USTAR** (no PAX/GNU extensions) so the on-device reader stays
a tiny dependency-free ustar parser feeding `XZInputStream`. The build streams
that tar through multi-threaded `xz`; both its xz level and the compile-time
zip's deflate level are controlled by `DROIDVM_PREBUILT_COMPRESSION_LEVEL`
(`1`–`9`, default `9`). There is no `.7z` and no bundled `7za` anymore.

## Two usage modes

**Pure-Java development** — you only change the app, not the native utils.
* Cloud: this repo's `Publish prebuilts` workflow builds everything and pushes
  artifacts to `DroidVM-Prebuilts`.
* Local: leave `DroidVM-Prebuilt-Root` absent from the DroidVM checkout. The
  app's Gradle build uses the `DroidVM-Prebuilts` submodule artifacts as-is.

**Native development** — you edit one of the utils.
* Clone this repo to the DroidVM checkout root (`DroidVM/DroidVM-Prebuilt-Root`).
* Put / edit the util source under `auto-build/<dir>/`. A **non-empty** dir is
  built as-is and never synced (your local edits win). Missing/empty dirs are
  cloned per `auto-build.toml`.
* The app's Gradle build detects the non-empty `auto-build/` and runs
  `auto-build.py`, regenerating the `prebuilt-*` artifacts locally before
  packaging — so the APK contains your changes.

## Running it

```bash
# Needs Python 3.11+, xz-utils, and the build toolchains (no pip deps).

# local native dev: build whatever is checked out (no remote sync)
python3 auto-build.py

# CI publish: force every repo to its pinned commit / branch HEAD, then build
python3 auto-build.py --force

# write artifacts somewhere else (Gradle passes the app submodule dir)
python3 auto-build.py --out /path/to/DroidVM/app/src/main/assets/prebuilts
```

Toolchains needed when actually building: Go (gvswitch, bridgedhcp),
Rust/cargo (lbx, netbox, pbridge — pbridge also needs clang), `make`, and
`xz-utils`. Zip creation is handled by the Python stdlib.

## Editing the config

`auto-build.toml` defines, per repo: `url`, `branch`, `commit` (`latest` tracks
the branch HEAD, or pin a 40-char sha), the `dir` under `auto-build/`, and per
arch a list of `build` commands plus a `dist` map. Each `dist` entry is
`{ from: <path the build produces>, to: <path inside the artifact> }`:

* `runtime` `to:` is relative to the data-dir root (`bin/netbox`, `usr/bin/…`).
* `comptime` `to:` is flat inside the zip → `lib/<arch>/` (`liblbx.so`).

Add an arch by appending it to `arch.lst` and giving each repo an entry under
`arches:`.
