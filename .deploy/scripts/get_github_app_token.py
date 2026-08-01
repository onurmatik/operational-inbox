"""Generate a short-lived GitHub App installation token for deployments."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import jwt
import requests

DEPLOY_DIR = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env(DEPLOY_DIR / "deploy.env")
load_env(DEPLOY_DIR / ".credentials.env")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mint a GitHub App installation token for deployments."
    )
    parser.add_argument(
        "--app-id",
        type=int,
        default=os.environ.get("GITHUB_APP_ID"),
        help="The GitHub App ID (env GITHUB_APP_ID)",
    )
    parser.add_argument(
        "--installation-id",
        type=int,
        default=os.environ.get("GITHUB_APP_INSTALLATION_ID"),
        help="The installation ID (env GITHUB_APP_INSTALLATION_ID)",
    )
    parser.add_argument(
        "--private-key",
        type=Path,
        default=Path(
            os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "~/.ssh/optbot-app.pem")
        ).expanduser(),
        help="Path to the GitHub App private key PEM file",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.app_id or not args.installation_id:
        raise SystemExit("ERROR: GitHub App ID and installation ID are required.")
    if not args.private_key.is_file():
        raise SystemExit("ERROR: GitHub App private key file was not found.")

    now = int(time.time())
    app_jwt = jwt.encode(
        {"iat": now - 60, "exp": now + (10 * 60), "iss": str(args.app_id)},
        args.private_key.read_text(),
        algorithm="RS256",
    )
    response = requests.post(
        f"https://api.github.com/app/installations/{args.installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    response.raise_for_status()
    print(response.json()["token"])


if __name__ == "__main__":
    main()
