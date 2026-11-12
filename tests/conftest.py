import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import get_db
from app.main import app
from app.seed import seed


@pytest.fixture(scope="session")
def database():
    if not settings.test_database_url:
        raise pytest.UsageError(
            "Set TEST_DATABASE_URL to an isolated PostgreSQL database ending in _test"
        )
    url = make_url(settings.test_database_url)
    production = make_url(settings.database_url)
    if (
        url.get_backend_name() != "postgresql"
        or not (url.database or "").endswith("_test")
        or url.database == production.database
    ):
        raise pytest.UsageError("Refusing to truncate a non-test database")
    config = Config("alembic.ini")
    config.attributes["database_url"] = settings.test_database_url
    command.upgrade(config, "head")
    engine = create_engine(url, pool_size=10, max_overflow=10)
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(database):
    with database.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE events, operations, balances, warehouses, products RESTART IDENTITY CASCADE"
            )
        )
    factory = sessionmaker(database, expire_on_commit=False)
    with factory.begin() as db:
        seed(db)
    return factory


@pytest.fixture()
def client(session_factory):
    def override():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
