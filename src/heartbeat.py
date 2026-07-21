"""Heartbeat entrypoint: write one proof-of-life row to Supabase.

Run by the CI cron (`python -m src.heartbeat`) to prove the scheduled
GitHub Actions -> Supabase pipe works end-to-end. Fails loudly (iron rule #4)
if the DB is unreachable or secrets are missing — never exits 0 on a silent skip.
"""

from __future__ import annotations

import os
import sys

from src.db import DB


def build_row() -> dict[str, str]:
    """Assemble a heartbeat row from the environment.

    In CI, GITHUB_ACTIONS / GITHUB_SHA are set by the runner; locally they are
    absent, so `source` falls back to 'local'.
    """
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    row: dict[str, str] = {
        "source": "ci" if in_ci else "local",
        "note": os.environ.get("GITHUB_WORKFLOW", "manual"),
    }
    git_sha = os.environ.get("GITHUB_SHA")
    if git_sha:
        row["git_sha"] = git_sha
    return row


def main() -> int:
    db = DB.from_env()
    written = db.insert_heartbeat(build_row())
    if not written:
        print("heartbeat: insert returned no rows", file=sys.stderr)
        return 1
    print(f"heartbeat: wrote row id={written[0].get('id')} source={written[0].get('source')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
