"""Shared pytest fixtures.

Unit tests need nothing here. Integration tests (RUN_INTEGRATION=1) need real
Supabase credentials, which live in the gitignored `.env` locally and in Actions
secrets in CI — so we load `.env` into the environment only when they're asked for.
"""

from __future__ import annotations

import os

import pytest

from src.config import load_dotenv as _load_dotenv


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


@pytest.fixture(scope="session")
def live_snaptrade():
    """A real SnapTrade client against the live Personal key, or skip.

    Read-only: the tests using this only ever list accounts and holdings.
    """
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("integration test; set RUN_INTEGRATION=1 to run")
    _load_dotenv()

    from src.sync import build_client

    return build_client()
