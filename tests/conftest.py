import os
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
import pytest

from enterpriseiq.dataset import read_bundle
from enterpriseiq.store import import_bundle


@pytest.fixture(scope="session")
def bundle():
    archive = Path("data/enterprise-bench-data.zip")
    if not archive.exists():
        pytest.skip("Audited data archive is not present")
    return read_bundle(archive)


@pytest.fixture
def conn():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to a separate local test database")
    if conninfo_to_dict(dsn).get("dbname") == conninfo_to_dict(os.environ.get("DATABASE_URL", "")).get("dbname"):
        pytest.fail("TEST_DATABASE_URL must use a different database")
    with psycopg.connect(dsn) as connection:
        with connection.transaction(force_rollback=True):
            connection.execute("SET LOCAL statement_timeout='10s'")
            yield connection


@pytest.fixture
def populated(conn, bundle):
    import_bundle(conn, bundle)
    return conn
