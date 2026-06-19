# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the Zammad charm (src/charm.py)."""

from __future__ import annotations

import types

import ops
import pytest
from ops.testing import Harness

import charm


@pytest.fixture
def harness(monkeypatch):
    """Provide a Harness with the Zammad workload fully mocked out."""
    monkeypatch.setattr(charm.zammad, "get_version", lambda: None)
    monkeypatch.setattr(charm.zammad, "add_apt_repo", lambda: None)
    monkeypatch.setattr(charm.zammad, "install", lambda version="": None)
    monkeypatch.setattr(charm.zammad, "is_installed", lambda: False)
    monkeypatch.setattr(charm.zammad, "is_active", lambda service="": False)
    monkeypatch.setattr(charm.zammad, "start", lambda: None)
    monkeypatch.setattr(charm.zammad, "stop", lambda: None)
    monkeypatch.setattr(charm.zammad, "restart", lambda service="": None)
    monkeypatch.setattr(charm.zammad, "render_database_yml", lambda **k: None)
    monkeypatch.setattr(charm.zammad, "write_env", lambda env: None)
    monkeypatch.setattr(charm.zammad, "render_nginx", lambda **k: None)
    monkeypatch.setattr(charm.zammad, "render_s3_storage", lambda **k: None)
    monkeypatch.setattr(charm.zammad, "disable_s3_storage", lambda: None)
    monkeypatch.setattr(charm.zammad, "set_storage_provider", lambda p: None)
    monkeypatch.setattr(charm.zammad, "es_is_ready", lambda url="": False)
    monkeypatch.setattr(charm.zammad, "set_es_settings", lambda **k: None)
    monkeypatch.setattr(charm.zammad, "db_migrate", lambda: None)
    monkeypatch.setattr(charm.zammad, "update_translations", lambda: None)
    monkeypatch.setattr(charm.zammad, "clear_cache", lambda: None)
    monkeypatch.setattr(charm.zammad, "rebuild_searchindex", lambda threads=1: "ok")
    monkeypatch.setattr(charm.zammad, "backup", lambda **k: "/tmp/zammad-1.tar.gz")
    monkeypatch.setattr(charm.zammad, "restore", lambda **k: None)
    monkeypatch.setattr(charm.zammad, "set_admin_password", lambda **k: None)
    monkeypatch.setattr(charm.zammad, "service_status", lambda service="": "active")
    monkeypatch.setattr(charm.zammad, "pg_is_ready", lambda host="", port=5432: True)
    monkeypatch.setattr(charm.zammad, "redis_is_ready", lambda url="": True)
    monkeypatch.setattr(charm.zammad, "http_ok", lambda url: True)
    h = Harness(charm.ZammadCharm)
    h.add_relation("peers", "zammad")
    h.begin()
    yield h
    h.cleanup()


def _db_event(*, endpoints="db:5432", database="zammad", username="u", password="p", uris=None):
    return types.SimpleNamespace(
        endpoints=endpoints, database=database, username=username, password=password, uris=uris
    )


def _redis_event(*, uris="redis://r:6379"):
    return types.SimpleNamespace(
        endpoints=None, database=None, username=None, password=None, uris=uris
    )


def _set_installed(harness, monkeypatch):
    harness.charm._stored.installed = True
    monkeypatch.setattr(charm.zammad, "is_installed", lambda: True)
    monkeypatch.setattr(charm.zammad, "get_version", lambda: "7.0.2")


# --- Status logic --------------------------------------------------------


def test_blocked_without_database(harness):
    harness.charm._reconcile()
    status = harness.charm.unit.status
    assert isinstance(status, ops.BlockedStatus)
    assert "PostgreSQL" in status.message


def test_blocked_without_redis(harness):
    harness.charm._on_db_changed(_db_event())
    harness.charm._reconcile()
    assert "Redis" in harness.charm.unit.status.message


def test_blocked_without_elasticsearch(harness, monkeypatch):
    _set_installed(harness, monkeypatch)
    harness.charm._on_db_changed(_db_event())
    harness.charm._on_redis_changed(_redis_event())
    # No elasticsearch-url configured.
    harness.charm._reconcile()
    assert "elasticsearch-url" in harness.charm.unit.status.message


def test_active_when_ready(harness, monkeypatch):
    _set_installed(harness, monkeypatch)
    monkeypatch.setattr(charm.zammad, "es_is_ready", lambda url="": True)
    monkeypatch.setattr(charm.zammad, "is_active", lambda service="": True)
    monkeypatch.setattr(charm.zammad, "get_version", lambda: "7.0.2")
    harness.update_config({"elasticsearch-url": "http://es:9200"})
    harness.charm._on_db_changed(_db_event())
    harness.charm._on_redis_changed(_redis_event())
    harness.charm._reconcile()
    status = harness.charm.unit.status
    assert isinstance(status, ops.ActiveStatus)
    assert status.message == "Zammad 7.0.2 ready"


def test_install_runs_on_first_reconcile(harness, monkeypatch):
    called = {"install": 0}

    def fake_install(version=""):
        called["install"] += 1
        harness.charm._stored.installed = True

    monkeypatch.setattr(charm.zammad, "install", fake_install)
    monkeypatch.setattr(charm.zammad, "is_installed", lambda: called["install"] > 0)
    monkeypatch.setattr(charm.zammad, "es_is_ready", lambda url="": True)
    monkeypatch.setattr(charm.zammad, "is_active", lambda service="": True)
    harness.update_config({"elasticsearch-url": "http://es:9200"})
    harness.charm._on_db_changed(_db_event())
    harness.charm._on_redis_changed(_redis_event())
    harness.charm._reconcile()
    assert called["install"] == 1


# --- Actions ------------------------------------------------------------


class _FakeAction:
    def __init__(self, params=None):
        self.params = params or {}
        self.logs: list[str] = []
        self.results: dict = {}
        self.failed: str | None = None

    def log(self, msg):
        self.logs.append(msg)

    def set_results(self, results):
        self.results = results

    def fail(self, msg):
        self.failed = msg


def test_action_backup(harness, monkeypatch):
    _set_installed(harness, monkeypatch)
    ev = _FakeAction({"output-dir": "/tmp/bk"})
    harness.charm._on_backup(ev)
    assert ev.failed is None
    assert ev.results["backup-file"].endswith(".tar.gz")


def test_action_backup_not_installed(harness):
    ev = _FakeAction()
    harness.charm._on_backup(ev)
    assert ev.failed is not None


def test_action_restart(harness, monkeypatch):
    _set_installed(harness, monkeypatch)
    calls = []
    monkeypatch.setattr(charm.zammad, "restart", lambda service="": calls.append(service))
    ev = _FakeAction({"service": "web"})
    harness.charm._on_restart(ev)
    assert calls == ["web"]
    assert ev.results["restarted"] == "web"


def test_action_health_check(harness, monkeypatch):
    _set_installed(harness, monkeypatch)
    harness.charm._on_db_changed(_db_event())
    harness.charm._on_redis_changed(_redis_event())
    harness.update_config({"elasticsearch-url": "http://es:9200"})
    monkeypatch.setattr(charm.zammad, "es_is_ready", lambda url="": True)
    ev = _FakeAction()
    harness.charm._on_health_check(ev)
    assert ev.results["installed"] is True
    assert ev.results["postgres"] is True
    assert ev.results["redis"] is True
    assert ev.results["elasticsearch"] is True


def test_action_set_admin_password(harness, monkeypatch):
    _set_installed(harness, monkeypatch)
    called = {}
    monkeypatch.setattr(charm.zammad, "set_admin_password", lambda **k: called.update(k))
    ev = _FakeAction({"password": "s3cret", "login": "admin", "email": "a@b.c"})
    harness.charm._on_set_admin_password(ev)
    assert called == {"login": "admin", "email": "a@b.c", "password": "s3cret"}


def test_action_rebuild_search_index(harness, monkeypatch):
    _set_installed(harness, monkeypatch)
    ev = _FakeAction({"threads": 4})
    harness.charm._on_rebuild_search_index(ev)
    assert ev.failed is None
    assert "output" in ev.results


def test_action_restore(harness, monkeypatch):
    _set_installed(harness, monkeypatch)
    ev = _FakeAction({"backup-file": "/tmp/x.tar.gz"})
    harness.charm._on_restore(ev)
    assert ev.results["restored-from"] == "/tmp/x.tar.gz"


def test_action_upgrade(harness, monkeypatch):
    _set_installed(harness, monkeypatch)
    monkeypatch.setattr(charm.zammad, "get_version", lambda: "7.1.0")
    monkeypatch.setattr(charm.zammad, "es_is_ready", lambda url="": True)
    monkeypatch.setattr(charm.zammad, "is_active", lambda service="": True)
    harness.charm._stored.es_configured = True
    ev = _FakeAction({"version": ""})
    harness.charm._on_upgrade(ev)
    assert ev.results["version"] == "7.1.0"


# --- Relation parsing ---------------------------------------------------


def test_db_changed_captures_credentials(harness):
    harness.charm._on_db_changed(_db_event(endpoints="10.0.0.5:5432", password="hunter2"))
    assert harness.charm._stored.db_host == "10.0.0.5"
    assert harness.charm._stored.db_port == 5432
    assert harness.charm._stored.db_pass == "hunter2"


def test_redis_changed_captures_url(harness):
    harness.charm._on_redis_changed(_redis_event(uris="rediss://:pw@r:6379/0"))
    assert harness.charm._stored.redis_url == "rediss://:pw@r:6379/0"


def test_db_changed_empty_waits(harness):
    harness.charm._on_db_changed(_db_event(endpoints="", username=None))
    assert harness.charm._stored.db_host == ""


# --- small helpers ------------------------------------------------------


def ops_blocked():
    from ops import BlockedStatus

    return BlockedStatus
