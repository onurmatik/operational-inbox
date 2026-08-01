from __future__ import annotations

import os
import sys
from pathlib import Path


def read_updates(path: Path) -> dict[str, str]:
    updates: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        if not raw_line or raw_line.lstrip().startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        if key:
            updates[key] = value
    return updates


def merge_env(target: Path, updates: dict[str, str]) -> None:
    lines = target.read_text().splitlines() if target.exists() else []
    merged: list[str] = []
    applied: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                merged.append(f"{key}={updates[key]}")
                applied.add(key)
                continue
        merged.append(line)

    if merged and merged[-1]:
        merged.append("")
    for key, value in updates.items():
        if key not in applied:
            merged.append(f"{key}={value}")

    temporary = target.with_name(f"{target.name}.tmp")
    temporary.write_text("\n".join(merged).rstrip("\n") + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: sync_env.py TARGET_ENV UPDATE_ENV")
    target, update_file = map(Path, sys.argv[1:])
    merge_env(target, read_updates(update_file))


if __name__ == "__main__":
    main()
