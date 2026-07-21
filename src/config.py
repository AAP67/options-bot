"""Environment loading, shared by every entrypoint.

Secrets reach the process differently depending on where it runs (iron rule #3):
GitHub Actions injects them as real environment variables, while local runs keep
them in a gitignored `.env`. Entrypoints call `load_dotenv()` so both work
without the caller needing to know which context it's in.
"""

from __future__ import annotations

import os
import pathlib

ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path: pathlib.Path = ENV_FILE) -> None:
    """Populate os.environ from a .env file, never overriding what's already set.

    Real environment variables win, so CI (where no .env exists) is unaffected
    and a stale local file can never shadow an explicitly exported value.
    Missing file is a no-op, not an error — that is the normal CI case.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
