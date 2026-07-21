"""Shared pytest fixtures.

Unit tests need nothing here. Integration tests (RUN_INTEGRATION=1) need real
Supabase credentials, which live in the gitignored `.env` locally and in Actions
secrets in CI — so we load `.env` into the environment only when they're asked for.
"""

from __future__ import annotations

import os
import pathlib

import pytest

ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv() -> None:
    """Populate os.environ from .env without overriding what's already set."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@pytest.fixture(scope="session")
def integration_db():
    """A real DB against the live Supabase project, or skip.

    Gated behind RUN_INTEGRATION=1 so the default `pytest` run stays hermetic
    and CI never writes to the production project.
    """
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("integration test; set RUN_INTEGRATION=1 to run")
    _load_dotenv()

    from src.db import DB

    return DB.from_env()
