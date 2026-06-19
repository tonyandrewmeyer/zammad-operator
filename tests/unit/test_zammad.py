# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the Zammad workload module (src/zammad.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

import zammad


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Redirect all filesystem paths the module uses to a tmp tree."""
    monkeypatch.setattr(zammad, "ZAMMAD_HOME", tmp_path / "opt/zammad")
    monkeypatch.setattr(zammad, "DB_YML", tmp_path / "opt/zammad/config/database.yml")
    monkeypatch.setattr(zammad, "STORAGE_YML", tmp_path / "opt/zammad/config/zammad/storage.yml")
    monkeypatch.setattr(zammad, "SECRETS_DIR", tmp_path / "opt/zammad/config/zammad")
    monkeypatch.setattr(zammad, "CONF_D", tmp_path / "etc/zammad/conf.d")
    monkeypatch.setattr(zammad, "JUJU_ENV", tmp_path / "etc/zammad/conf.d/zz-juju.conf")
    monkeypatch.setattr(
        zammad, "NGINX_AVAILABLE", tmp_path / "etc/nginx/sites-available/zammad.conf"
    )
    monkeypatch.setattr(zammad, "NGINX_ENABLED", tmp_path / "etc/nginx/sites-enabled/zammad.conf")
    monkeypatch.setattr(zammad, "NGINX_DEFAULT", tmp_path / "etc/nginx/sites-enabled/default")
    monkeypatch.setattr(zammad, "APT_LIST", tmp_path / "etc/apt/sources.list.d/zammad.list")
    monkeypatch.setattr(zammad, "APT_KEYRING", tmp_path / "etc/apt/keyrings/pkgr-zammad.gpg")
    monkeypatch.setattr(zammad, "DEFAULT_BACKUP_DIR", tmp_path / "var/backups/zammad")
    # Pre-create the keyring so add_apt_repo() skips the curl/gpg path.
    zammad.APT_KEYRING.parent.mkdir(parents=True, exist_ok=True)
    zammad.APT_KEYRING.touch()
    return tmp_path


def _mock_run(monkeypatch, responses=None):
    """Replace zammad._run with a MagicMock returning canned outputs."""
    calls: list[list[str]] = []

    def fake_run(cmd, *, check=True, capture=True, env=None, input=None, timeout=None):
        calls.append(list(cmd))
        proc = MagicMock(spec=subprocess.CompletedProcess)
        proc.returncode = 0
        proc.stdout = (responses or {}).get(tuple(cmd), "")
        proc.stderr = ""
        return proc

    monkeypatch.setattr(zammad, "_run", fake_run)
    return calls


def test_split_endpoint():
    """Unit test for the endpoint splitter used by the charm."""
    from charm import _split_endpoint

    assert _split_endpoint("1.2.3.4:5432") == ("1.2.3.4", 5432)
    assert _split_endpoint("host:5432,other:5432") == ("host", 5432)
    assert _split_endpoint("justhost") == ("justhost", 0)
    assert _split_endpoint("") == ("", 0)
    assert _split_endpoint("host:notaport") == ("host", 0)


def test_ruby_quote():
    assert zammad.ruby_quote("simple") == '"simple"'
    assert zammad.ruby_quote('a"b') == '"a\\"b"'
    assert zammad.ruby_quote("a\\b") == '"a\\\\b"'


def test_render_database_yml(paths, monkeypatch):
    monkeypatch.setattr(zammad.shutil, "chown", lambda *a, **k: None)
    zammad.render_database_yml(
        host="db.example", port=5432, database="zammad", username="u", password="p"
    )
    data = yaml.safe_load(zammad.DB_YML.read_text())
    assert data["production"]["host"] == "db.example"
    assert data["production"]["port"] == 5432
    assert data["production"]["password"] == "p"
    assert data["production"]["adapter"] == "postgresql"


def test_write_env_skips_empty(paths, monkeypatch):
    monkeypatch.setattr(zammad.shutil, "chown", lambda *a, **k: None)
    zammad.write_env({"REDIS_URL": "redis://x:6379", "EMPTY": "", "THREADS": "5"})
    content = zammad.JUJU_ENV.read_text()
    assert 'export REDIS_URL="redis://x:6379"' in content
    assert "THREADS" in content
    assert "EMPTY" not in content


def test_db_credentials(paths, monkeypatch):
    monkeypatch.setattr(zammad.shutil, "chown", lambda *a, **k: None)
    zammad.render_database_yml(host="h", port=5433, database="d", username="u", password="pw")
    creds = zammad.db_credentials()
    assert creds == {"host": "h", "port": 5433, "database": "d", "username": "u", "password": "pw"}


def test_db_credentials_missing(paths):
    with pytest.raises(zammad.ZammadError):
        zammad.db_credentials()


def test_render_and_disable_s3(paths, monkeypatch):
    monkeypatch.setattr(zammad.shutil, "chown", lambda *a, **k: None)
    zammad.render_s3_storage(
        endpoint="https://s3.local",
        bucket="b",
        region="us-east-1",
        access_key="ak",
        secret_key="sk",
    )
    cfg = yaml.safe_load(zammad.STORAGE_YML.read_text())
    assert cfg["s3"]["bucket"] == "b"
    assert cfg["s3"]["access_key_id"] == "ak"
    assert cfg["s3"]["force_path_style"] is True
    zammad.disable_s3_storage()
    assert not zammad.STORAGE_YML.exists()


def test_add_apt_repo_writes_list(paths, monkeypatch):
    calls = _mock_run(monkeypatch, {(("curl", "-fsSL", zammad.PACKAGER_KEY_URL),): "KEY"})
    monkeypatch.setattr(zammad, "_get_os_version_id", lambda: "24.04")
    zammad.add_apt_repo()
    assert zammad.APT_LIST.read_text().strip().endswith("24.04 main")
    assert any(c[0] == "apt-get" for c in calls)


def test_install_masks_local_deps(paths, monkeypatch):
    mask_calls: list[str] = []

    def fake_systemctl(*args, check=True):
        if args and args[0] == "mask":
            mask_calls.append(args[1])
        m = MagicMock(spec=subprocess.CompletedProcess)
        m.returncode = 0
        m.stdout = ""
        return m

    def fake_run(cmd, **kw):
        m = MagicMock(spec=subprocess.CompletedProcess)
        m.returncode = 0
        m.stdout = ""
        return m

    monkeypatch.setattr(zammad, "_run", fake_run)
    monkeypatch.setattr(zammad, "_systemctl", fake_systemctl)
    zammad.install()
    assert set(mask_calls) == set(zammad.LOCAL_DEPS_TO_MASK)


def test_service_control(monkeypatch):
    seen: list[str] = []

    def fake_systemctl(*args, check=True):
        seen.append(" ".join(args))
        m = MagicMock(spec=subprocess.CompletedProcess)
        m.returncode = 0
        m.stdout = "active" if args[:1] == ["is-active"] else ""
        return m

    monkeypatch.setattr(zammad, "_systemctl", fake_systemctl)
    zammad.start()
    zammad.restart("web")
    assert zammad.is_active()
    assert seen[0] == "start zammad"
    assert "restart zammad-web" in seen


def test_backup_creates_archive(paths, monkeypatch):
    monkeypatch.setattr(zammad.shutil, "chown", lambda *a, **k: None)
    zammad.render_database_yml(host="h", port=5432, database="zammad", username="u", password="p")

    def fake_run(cmd, **kw):
        m = MagicMock(spec=subprocess.CompletedProcess)
        m.returncode = 0
        if cmd[0] == "date":
            m.stdout = "20260101000000"
        elif cmd[0] == "pg_dump":
            Path(cmd[cmd.index("-f") + 1]).write_bytes(b"PGDUMP")
            m.stdout = ""
        elif cmd[:2] == ["tar", "-C"] and "-czf" in cmd:
            Path(cmd[cmd.index("-czf") + 1]).write_bytes(b"TARGZ")
            m.stdout = ""
        else:
            m.stdout = ""
        return m

    monkeypatch.setattr(zammad, "_run", fake_run)
    archive = zammad.backup(output_dir=zammad.DEFAULT_BACKUP_DIR, retention_days=7)
    assert Path(archive).exists()
    assert archive.endswith(".tar.gz")


def test_restore_runs_pg_restore(paths, monkeypatch):
    monkeypatch.setattr(zammad.shutil, "chown", lambda *a, **k: None)
    zammad.render_database_yml(host="h", port=5432, database="zammad", username="u", password="p")
    cmds: list[list[str]] = []

    def fake_systemctl(*args, check=True):
        cmds.append(["systemctl", *args])
        m = MagicMock(spec=subprocess.CompletedProcess)
        m.returncode = 0
        m.stdout = ""
        return m

    def fake_run(cmd, **kw):
        cmds.append(list(cmd))
        m = MagicMock(spec=subprocess.CompletedProcess)
        m.returncode = 0
        m.stdout = ""
        if cmd[0] == "tar":
            # extract creates db.dump in the work dir
            Path(cmd[2]).mkdir(parents=True, exist_ok=True)
            (Path(cmd[2]) / "db.dump").write_bytes(b"X")
        return m

    monkeypatch.setattr(zammad, "_run", fake_run)
    monkeypatch.setattr(zammad, "_systemctl", fake_systemctl)
    monkeypatch.setattr(zammad, "clear_cache", lambda: None)

    backup = paths / "bk.tar.gz"
    backup.write_bytes(b"DATA")
    zammad.restore(backup_file=str(backup))
    assert any(c[0] == "pg_restore" for c in cmds)
    assert ["systemctl", "stop", "zammad"] in cmds
    assert ["systemctl", "start", "zammad"] in cmds


def test_is_installed_and_version(monkeypatch):
    def fake_run(cmd, **kw):
        m = MagicMock(spec=subprocess.CompletedProcess)
        m.returncode = 0
        if "-f=${Status}" in cmd:
            m.stdout = "install ok installed"
        elif "-f=${Version}" in cmd:
            m.stdout = "7.0.2-1"
        else:
            m.stdout = ""
        return m

    monkeypatch.setattr(zammad, "_run", fake_run)
    assert zammad.is_installed() is True
    assert zammad.get_version() == "7.0.2-1"
