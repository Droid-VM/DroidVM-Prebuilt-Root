#!/usr/bin/env python3
"""resolv_dep.py -- print the install deps for the repos that need rebuilding.

Reads auto-build.toml + the auto-build/ checkout state via the shared
needs_rebuild() predicate, and prints the union of each changed repo's `install:`
packages. Repos already at their pinned/branch commit contribute nothing, so an
all-cached run prints an empty line and nothing gets installed.

  python3 resolv_dep.py             # only repos whose commit/url changed
  python3 resolv_dep.py --force     # every repo (cold install)
  python3 resolv_dep.py --no-fetch  # compare offline (don't hit the network)

Used by the publish workflow:  apt-get install -y $(python3 resolv_dep.py)
Only the package list goes to stdout; diagnostics go to stderr.
"""
import argparse
import sys
from pathlib import Path

from prebuild_common import load_config, needs_rebuild

ROOT = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="return deps for every repo (treat all as changed)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="don't fetch upstream; compare against local refs only")
    args = ap.parse_args()

    pkgs = set()
    for repo in load_config(ROOT).get("repos", []):
        if needs_rebuild(ROOT, repo, force=args.force, fetch=not args.no_fetch):
            deps = repo.get("install") or []
            pkgs.update(deps)
            print(f"{repo['name']}: rebuild -> {deps}", file=sys.stderr)
        else:
            print(f"{repo['name']}: up to date", file=sys.stderr)

    print(" ".join(sorted(pkgs)))


if __name__ == "__main__":
    main()
