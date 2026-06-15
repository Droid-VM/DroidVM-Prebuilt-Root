#!/usr/bin/env python3
import os
import sys
import json
import hashlib
from pathlib import Path


def sha256_digest(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def generate_hash_entries(input_dir: str, source: str = "prebuilt") -> list[dict]:
    root = Path(input_dir)
    entries = []
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        rel_path = file_path.relative_to(root)
        entries.append({
            "file": str(rel_path),
            "source": source,
            "sha256": sha256_digest(file_path),
        })
    return entries

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <dir> [output.json]", file=sys.stderr)
        sys.exit(1)

    dir = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) >= 3 else "hash.json"

    entries = generate_hash_entries(dir)
    result = {"hash": entries}

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Wrote {output_path}, {len(entries)} entries")


if __name__ == "__main__":
    main()

