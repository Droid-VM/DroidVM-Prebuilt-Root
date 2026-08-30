#!/usr/bin/env python3
"""auto-build.py -- build DroidVM third-party binaries and pack prebuilt artifacts.

Reads auto-build.toml, (optionally) syncs each source repo into auto-build/<dir>,
builds it per architecture, lays the outputs into the staging trees, and packs
per arch (see README.md and auto-build.toml for the full contract):

    prebuilt-<arch>.tar.xz        runtime payload  (first-run extraction)
    prebuilt-<arch>-comptime.zip  compile-time payload (Gradle -> APK jniLibs)
    prebuilt-<arch>.json          sha256 manifest of the runtime tree

Two modes:
    local native dev (default) -- a non-empty auto-build/<dir> is built AS-IS and
        never touched; a missing/empty one is cloned per the yaml. Lets a dev edit
        a util in place and just rebuild the APK.
    CI publish (--force)       -- every repo is converged to its pinned commit /
        branch HEAD (remote wins: hard reset + clean), then built.

Requires: python3 (3.11+, for stdlib tomllib), xz-utils, plus the toolchains the
builds need (Go, Rust/cargo, clang, make, ...). zip is handled by the Python
stdlib.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from prebuild_common import load_config, read_arches, is_nonempty_dir, needs_rebuild

ROOT = Path(__file__).resolve().parent
AUTO_BUILD = ROOT / "auto-build"     # source repos (gitignored contents)
MANUAL_BUILD = ROOT / "manual-build"  # runtime staging: committed usr/ + built bin/
COMPTIME = ROOT / ".comptime"         # gitignored work dir for comptime staging
COMPRESSION_LEVEL_ENV = "DROIDVM_PREBUILT_COMPRESSION_LEVEL"


def log(msg):
    print(f"\033[1;34m==>\033[0m {msg}", flush=True)


def warn(msg):
    print(f"\033[1;33mWARN:\033[0m {msg}", file=sys.stderr, flush=True)


def die(msg):
    sys.exit(f"\033[1;31mERROR:\033[0m {msg}")


def compression_level():
    """Return the shared xz/deflate level (1..9), defaulting to maximum."""
    raw = os.environ.get(COMPRESSION_LEVEL_ENV, "9")
    try:
        level = int(raw)
    except ValueError:
        die(f"{COMPRESSION_LEVEL_ENV} must be an integer from 1 to 9 (got {raw!r})")
    if not 1 <= level <= 9:
        die(f"{COMPRESSION_LEVEL_ENV} must be from 1 to 9 (got {level})")
    return level


def run(cmd, cwd=None):
    log(f"$ {cmd}")
    subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), check=True)


def git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True)


def _sync_submodules(path):
    # Best-effort: util repos build from their own tree (+ Go/Cargo deps), so a
    # missing/broken submodule (e.g. a test-only one with no URL in .gitmodules)
    # must not fail the build. If a repo genuinely needs a submodule, its own
    # build will surface that with a clearer error.
    try:
        git(["submodule", "sync", "--recursive"], cwd=path)
        git(["submodule", "update", "--init", "--recursive", "--force"], cwd=path)
    except subprocess.CalledProcessError:
        warn(f"submodule sync failed in {path.name}; continuing without submodules")


def clone_fresh(repo, path):
    """rm -rf the dir and clone the configured source from scratch.

    --depth 1 for a 'latest' pin (we only want the branch tip); a full clone for
    a pinned commit (a shallow clone may not contain an arbitrary sha).
    """
    if path.exists():
        shutil.rmtree(path)
    url, branch = repo["url"], repo.get("branch", "master")
    commit = str(repo.get("commit", "latest"))
    if commit == "latest":
        log(f"{repo['name']}: clone --depth 1 {url} ({branch})")
        git(["clone", "--depth", "1", "--branch", branch, url, str(path)], cwd=ROOT)
    else:
        log(f"{repo['name']}: clone {url} ({branch}) @ {commit}")
        git(["clone", "--branch", branch, url, str(path)], cwd=ROOT)
        git(["checkout", "--detach", commit], cwd=path)
    _sync_submodules(path)


def _converge(repo, path) -> bool:
    """Bring an existing checkout to the desired commit in place (reuse objects).
    Returns False if the checkout is too broken to fix and must be re-cloned."""
    branch = repo.get("branch", "master")
    commit = str(repo.get("commit", "latest"))
    target = commit if commit != "latest" else f"origin/{branch}"
    try:
        git(["remote", "set-url", "origin", repo["url"]], cwd=path)
        # --force/--prune so remote-tracking refs follow an upstream force-push.
        git(["fetch", "--force", "--prune", "--prune-tags", "--tags", "origin",
             f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], cwd=path)
        git(["reset", "--hard", target], cwd=path)       # remote wins
        git(["clean", "-ffdx"], cwd=path)                 # drop untracked/ignored/nested
        _sync_submodules(path)
        return True
    except subprocess.CalledProcessError:
        return False


def sync_repo(repo, force) -> bool:
    """Make auto-build/<dir> reflect the configured source for the active mode.

    Returns True when the checkout was (re)synced and therefore must be rebuilt;
    False when it is already at the desired commit (a GitHub-Actions cache hit)
    and its cached build outputs can be reused as-is.
    """
    path = AUTO_BUILD / repo["dir"]

    # Local dev: a non-empty checkout is the dev's working tree -- build it as-is,
    # never touch git, always rebuild so local edits land.
    if not force and is_nonempty_dir(path):
        log(f"{repo['name']}: using local checkout as-is (non-empty, no --force)")
        return True

    # CI (--force): rebuild iff the checkout diverges from the desired commit
    # (commit/url changed, wrong branch, missing/broken checkout, ...).
    if not needs_rebuild(ROOT, repo, force=False):
        log(f"{repo['name']}: already up to date (cache hit; reusing build)")
        return False

    # Converge to upstream. Prefer an in-place fetch+reset (reuses cached
    # objects); if the checkout is missing or too broken to fix, nuke + re-clone.
    if not is_nonempty_dir(path) or not _converge(repo, path):
        clone_fresh(repo, path)
    return True


def place(src: Path, dst: Path):
    if not src.is_file():
        die(f"expected build output not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    dst.chmod(0o755)
    log(f"placed {dst.relative_to(ROOT)}")


def build_repo(repo, arch, changed):
    spec = repo.get("arches", {}).get(arch)
    if spec is None:
        log(f"{repo['name']}: no {arch} target, skipping")
        return
    path = AUTO_BUILD / repo["dir"]
    dist = spec.get("dist", {}) or {}
    entries = (dist.get("runtime") or []) + (dist.get("comptime") or [])
    outputs = [path / d["from"] for d in entries]

    # Build unless this is a cache hit (commit unchanged) whose outputs all still
    # exist -- then reuse them and only re-stage. A missing output forces a build.
    if changed or any(not o.is_file() for o in outputs):
        for cmd in spec.get("build", []):
            run(cmd, cwd=path)
    else:
        log(f"{repo['name']} ({arch}): cached build, reusing outputs")

    for d in dist.get("runtime") or []:
        place(path / d["from"], MANUAL_BUILD / arch / d["to"])
    for d in dist.get("comptime") or []:
        place(path / d["from"], COMPTIME / arch / d["to"])


def pack_arch(arch, out_dir, level):
    stage = MANUAL_BUILD / arch
    if not stage.is_dir():
        die(f"runtime staging tree missing: {stage}")

    # Executables in the staging tree are often committed as 0644; force +x on
    # bin/ and usr/bin/ so they are runnable once extracted on-device.
    for sub in ("bin", "usr/bin"):
        d = stage / sub
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    f.chmod(0o755)

    json_path = out_dir / f"prebuilt-{arch}.json"
    run([sys.executable, str(ROOT / "hash-gen.py"), str(stage), str(json_path)])

    # runtime: plain USTAR tar (paths < 100 chars, no symlinks -> safe), piped to
    # xz so compression can use all available CPU cores. USTAR keeps the
    # on-device reader (org.tukaani:xz + a tiny ustar parser) dependency-free --
    # no 7z container or native binary is needed in the APK.
    # Normalize mtime/owner so identical content -> byte-identical archive; that
    # lets the publish step's git diff skip a no-op republish on a cache hit.
    def _norm(ti):
        ti.mtime = 0
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    tar_path = out_dir / f"prebuilt-{arch}.tar.xz"
    xz = shutil.which("xz")
    if xz is None:
        die("xz not found; install xz-utils for multi-threaded archive compression")

    # Stream the tar instead of materializing a large intermediate .tar. Write
    # to a temporary output so an interrupted/failed xz doesn't replace a good
    # existing artifact with a truncated one. -T0 lets xz select as many worker
    # threads as the host and its memory limit permit.
    tmp_path = tar_path.with_name(f".{tar_path.name}.tmp")
    tmp_path.unlink(missing_ok=True)
    cmd = [xz, "--threads=0", f"-{level}", "--stdout"]
    log(f"packing {tar_path.name} ($ {' '.join(cmd)})")
    try:
        with tmp_path.open("wb") as output:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=output)
            try:
                assert proc.stdin is not None
                with proc.stdin:
                    with tarfile.open(fileobj=proc.stdin, mode="w|",
                                      format=tarfile.USTAR_FORMAT) as tar:
                        for entry in sorted(stage.iterdir()):
                            tar.add(entry, arcname=entry.name, filter=_norm)
            except BaseException:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                raise
            if proc.wait() != 0:
                raise subprocess.CalledProcessError(proc.returncode, cmd)
        tmp_path.replace(tar_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    # comptime: plain deflate zip, unpacked by Gradle (java.util.zip) at build
    # time. Fixed date_time + mode keeps it reproducible like the tar above.
    ctstage = COMPTIME / arch
    if is_nonempty_dir(ctstage):
        zip_path = out_dir / f"prebuilt-{arch}-comptime.zip"
        log(f"packing {zip_path.name} (deflate level {level})")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=level) as zf:
            for f in sorted(ctstage.rglob("*")):
                if not f.is_file():
                    continue
                zi = zipfile.ZipInfo(f.relative_to(ctstage).as_posix(),
                                     date_time=(1980, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = 0o755 << 16
                zf.writestr(zi, f.read_bytes())


def main():
    ap = argparse.ArgumentParser(description="Build and pack DroidVM prebuilt artifacts.")
    ap.add_argument("--force", action="store_true",
                    help="CI mode: sync every repo to its pinned commit (remote wins).")
    ap.add_argument("--out", default=str(ROOT),
                    help="Where to write prebuilt-* artifacts (default: this repo).")
    ap.add_argument("--arch", action="append",
                    help="Build only these arch(es); default: all in arch.lst.")
    args = ap.parse_args()

    cfg = load_config(ROOT)
    arches = args.arch or read_arches(ROOT)
    if not arches:
        die("no architectures (arch.lst is empty)")
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    level = compression_level()
    log(f"prebuilt compression level: {level} ({COMPRESSION_LEVEL_ENV})")

    if COMPTIME.exists():
        shutil.rmtree(COMPTIME)

    repos = cfg.get("repos", [])
    changed = {repo["dir"]: sync_repo(repo, args.force) for repo in repos}
    for arch in arches:
        for repo in repos:
            build_repo(repo, arch, changed[repo["dir"]])
    for arch in arches:
        pack_arch(arch, out_dir, level)

    log(f"done -> {out_dir}")


if __name__ == "__main__":
    main()
