"""Shared helpers for the prebuilt pipeline (auto-build.py + resolv_dep.py)."""
import subprocess
import sys
from pathlib import Path

try:
    import tomllib  # stdlib since Python 3.11 -- no pip/apt dependency
except ModuleNotFoundError:
    sys.exit("Python 3.11+ is required (for the stdlib tomllib parser)")


def load_config(root):
    with open(Path(root) / "auto-build.toml", "rb") as f:
        return tomllib.load(f)


def read_arches(root):
    arches = []
    for line in (Path(root) / "arch.lst").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            arches.append(line)
    return arches


def is_nonempty_dir(p) -> bool:
    """True if p has any content other than a lone .gitignore."""
    p = Path(p)
    if not p.is_dir():
        return False
    return any(c.name != ".gitignore" for c in p.iterdir())


def auto_build_dir(root):
    return Path(root) / "auto-build"


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _norm_url(u):
    """Canonicalize a git remote URL for comparison, ignoring scheme / user /
    a trailing .git or slash, so SSH and HTTPS forms of the same repo match:
    https://github.com/o/r(.git) and git@github.com:o/r.git -> github.com/o/r
    """
    if not u:
        return u
    u = u.strip().removesuffix(".git").rstrip("/")
    for scheme in ("https://", "http://", "ssh://", "git://"):
        if u.startswith(scheme):
            u = u[len(scheme):]
            break
    if "@" in u:
        u = u.split("@", 1)[1]      # drop user@
    return u.replace(":", "/", 1)   # scp-style host:path -> host/path


def needs_rebuild(root, repo, force=False, fetch=True) -> bool:
    """Whether repo's auto-build/<dir> checkout must be (re)built.

    Conservative -- any uncertainty returns True:
      * force                                       -> True
      * missing/empty dir, or not a git checkout    -> True
      * origin URL != the configured url            -> True
      * wrong branch / detached (for a 'latest' pin) -> True
      * HEAD != desired (upstream branch HEAD for
        'latest', or the pinned commit otherwise)   -> True
      * any git error (missing ref, weird state)    -> True
    Otherwise (checkout already at the desired commit) -> False.

    Shared by auto-build.py (to decide whether to rebuild) and resolv_dep.py
    (to decide whether to install that repo's deps), so the two always agree.
    """
    if force:
        return True
    path = auto_build_dir(root) / repo["dir"]
    if not is_nonempty_dir(path) or not (path / ".git").exists():
        return True
    branch = repo.get("branch", "master")
    commit = str(repo.get("commit", "latest"))
    try:
        if _norm_url(_git(["remote", "get-url", "origin"], path)) != _norm_url(repo["url"]):
            return True   # the configured url changed
        if fetch:
            subprocess.run(["git", "fetch", "--force", "origin", branch],
                           cwd=path, check=True, capture_output=True, text=True)
        head = _git(["rev-parse", "HEAD"], path)
        if commit == "latest":
            if _git(["rev-parse", "--abbrev-ref", "HEAD"], path) != branch:
                return True   # detached or on the wrong branch
            return head != _git(["rev-parse", f"origin/{branch}"], path)
        return head != _git(["rev-parse", commit], path)
    except subprocess.CalledProcessError:
        return True
