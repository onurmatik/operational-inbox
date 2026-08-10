"""Install the managed MCP include into the existing HTTPS Nginx server block."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

LOCAL_ROUTES_MARKER = "    # LOCAL PROXY ROUTES → Loopback services"


def install_include(site_path: Path, include_path: Path) -> bool:
    include_line = f"    include {include_path};"
    source = site_path.read_text()
    if include_line in source:
        return False
    marker_count = source.count(LOCAL_ROUTES_MARKER)
    if marker_count != 1:
        raise RuntimeError(
            f"Expected exactly one local proxy routes marker in {site_path}; found {marker_count}."
        )
    backup_path = site_path.with_suffix(f"{site_path.suffix}.pre-operationalinbox-mcp")
    if not backup_path.exists():
        shutil.copy2(site_path, backup_path)
    updated = source.replace(
        LOCAL_ROUTES_MARKER,
        f"{LOCAL_ROUTES_MARKER}\n{include_line}",
        1,
    )
    temporary_path = site_path.with_suffix(f"{site_path.suffix}.tmp")
    temporary_path.write_text(updated)
    shutil.copymode(site_path, temporary_path)
    temporary_path.replace(site_path)
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("site_path", type=Path)
    parser.add_argument("include_path", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    install_include(args.site_path, args.include_path)


if __name__ == "__main__":
    main()
