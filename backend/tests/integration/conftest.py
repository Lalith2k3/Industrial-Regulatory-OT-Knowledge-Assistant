import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture
def engine():
    """A throwaway database on RAG_TEST_DATABASE_URL's server, dropped afterwards.

    Set RAG_TEST_DATABASE_URL to a server you can create databases on, e.g.
    postgresql+psycopg://rag:rag@localhost:5433/rag.
    """
    base = os.environ.get("RAG_TEST_DATABASE_URL")
    if not base:
        pytest.skip("RAG_TEST_DATABASE_URL not set")
    url = make_url(base)
    name = f"rag_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    test_engine = create_engine(url.set(database=name))
    try:
        yield test_engine
    finally:
        test_engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
