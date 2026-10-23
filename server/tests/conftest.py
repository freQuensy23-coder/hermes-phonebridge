from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from phonebridge_server.app import create_app
from phonebridge_server.config import Settings
from phonebridge_server.store import Store

from phonebridge_test_support import ADMIN_TOKEN, Clock


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings(tmp_path):
    return Settings(admin_token=ADMIN_TOKEN, public_url="https://phone.example.test",
                    db_path=tmp_path / "state" / "bridge.sqlite3", revision="ci-revision",
                    http_rate=10000, claim_rate=1000, poll_interval=0.01)


@pytest.fixture
def service(settings, clock):
    application = create_app(settings, clock)
    with TestClient(application) as client:
        yield application, client


@pytest.fixture
def client(service):
    return service[1]


@pytest.fixture
def store_factory(settings, clock):
    stores = []

    def create(**overrides):
        store = Store(replace(settings, **overrides), clock)
        stores.append(store)
        return store

    yield create
    for store in stores:
        store.close()

