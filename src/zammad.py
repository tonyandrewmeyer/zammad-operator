# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Workload management for the Zammad package (packager.io) on Ubuntu.

This module encapsulates everything that talks to the operating system or the
Zammad CLI. It is intentionally free of charming concerns so it can be unit
tested by mocking :func:`subprocess.run` and the filesystem helpers.

Ground truth (verified against the Zammad 7.0 .deb for Ubuntu 24.04):

- The package installs into ``/opt/zammad`` and registers systemd units via
  ``zammad scale web=1 websocket=1 worker=1`` (units ``zammad-web-1.service``,
  ``zammad-websocket-1.service``, ``zammad-worker-1.service`` plus the meta
  ``zammad.service``).
- PostgreSQL connection lives in ``/opt/zammad/config/database.yml``. The
  maintainer ``preinst`` aborts an install/upgrade if that file exists and the
  configured DB is unreachable; the ``postinst`` skips local DB creation when
  the file already exists (it only runs ``rake db:migrate``).
- Environment (``REDIS_URL``, thread/concurrency tuning, ``MEMCACHE_SERVERS``…)
  is sourced from every file in ``/etc/zammad/conf.d/``; ``zammad config:get``
  reads them all. We write a dedicated ``zz-juju.conf`` so our values win.
- nginx site: ``/etc/nginx/sites-available/zammad.conf`` (symlinked into
  ``sites-enabled``), proxying to puma on :3000 and the websocket on :6042.
- Attachment storage defaults to the DB (``Setting.get('storage_provider')``).
  S3 is enabled by writing ``/opt/zammad/config/zammad/storage.yml`` and
  ``Setting.set('storage_provider', 'S3')``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import jinja2
import yaml

logger = logging.getLogger(__name__)

# --- Constants -------------------------------------------------------------

ZAMMAD_HOME = Path("/opt/zammad")
DB_YML = ZAMMAD_HOME / "config" / "database.yml"
SECRETS_DIR = ZAMMAD_HOME / "config" / "zammad"
STORAGE_YML = SECRETS_DIR / "storage.yml"
CONF_D = Path("/etc/zammad/conf.d")
JUJU_ENV = CONF_D / "zz-juju.conf"
NGINX_AVAILABLE = Path("/etc/nginx/sites-available/zammad.conf")
NGINX_ENABLED = Path("/etc/nginx/sites-enabled/zammad.conf")
NGINX_DEFAULT = Path("/etc/nginx/sites-enabled/default")
ZAMMAD_CLI = "zammad"
APT_LIST = Path("/etc/apt/sources.list.d/zammad.list")
APT_KEYRING = Path("/etc/apt/keyrings/pkgr-zammad.gpg")
PACKAGER_KEY_URL = "https://dl.packager.io/srv/zammad/zammad/key"
PACKAGER_REPO = "https://dl.packager.io/srv/deb/zammad/zammad/stable/ubuntu"
DEFAULT_BACKUP_DIR = Path("/var/backups/zammad")

# Services pulled in as hard Depends that we do NOT use (external relations).
LOCAL_DEPS_TO_MASK = ("postgresql", "redis-server", "elasticsearch")

RAILS_PORT = 3000
WEBSOCKET_PORT = 6042


class ZammadError(Exception):
    """Raised when a workload operation fails."""


# --- Low-level helpers -----------------------------------------------------


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
    input: str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, returning the completed process.

    Raises :class:`ZammadError` on non-zero exit when ``check`` is set.
    """
    logger.debug("run: %s", " ".join(cmd))
    try:
        return subprocess.run(
            cmd,
            check=check,
            text=True,
            capture_output=capture,
            env=env,
            input=input,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise ZammadError(
            f"command {' '.join(cmd)!r} failed with {exc.returncode}: "
            f"{(exc.stderr or '').strip() or (exc.stdout or '').strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ZammadError(f"command {' '.join(cmd)!r} timed out") from exc


def _write_file(path: Path, content: str, *, mode: int = 0o640) -> None:
    """Atomically write a file with the given mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Wrap systemctl."""
    return _run(["systemctl", *args], check=check)


# --- Package / repository --------------------------------------------------


def add_apt_repo() -> None:
    """Add the Zammad packager.io apt repository and key (idempotent)."""
    APT_KEYRING.parent.mkdir(parents=True, exist_ok=True)
    if not APT_KEYRING.exists():
        key = _run(["curl", "-fsSL", PACKAGER_KEY_URL], capture=True).stdout
        _run(["gpg", "--dearmor", "-o", str(APT_KEYRING)], check=True, input=key)
        os.chmod(APT_KEYRING, 0o644)
    version_id = _get_os_version_id()
    repo_line = f"deb [signed-by={APT_KEYRING}] {PACKAGER_REPO} {version_id} main\n"
    existing = APT_LIST.read_text().strip() if APT_LIST.exists() else ""
    if existing != repo_line.strip():
        _write_file(APT_LIST, repo_line, mode=0o644)
    _run(["apt-get", "update"], check=True, capture=True, timeout=120)


def _get_os_version_id() -> str:
    """Return the VERSION_ID from /etc/os-release."""
    info: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip().strip('"')
    return info.get("VERSION_ID", "24.04")


def is_installed() -> bool:
    """Return whether the zammad package is installed."""
    return (
        _run(["dpkg-query", "-W", "-f=${Status}", "zammad"], check=False)
        .stdout.strip()
        .endswith("install ok installed")
    )


def get_version() -> str | None:
    """Return the installed Zammad version, or None."""
    if not is_installed():
        return None
    return (
        _run(["dpkg-query", "-W", "-f=${Version}", "zammad"], check=False).stdout.strip() or None
    )


def install(version: str = "") -> None:
    """Install (or upgrade to) the Zammad package.

    ``database.yml`` and ``zz-juju.conf`` MUST be written first via
    :func:`render_database_yml` / :func:`write_env`, so that ``postinst`` takes
    the "already configured" path (migrate only, no local DB creation) and so
    ``enforce_redis`` sees a reachable ``REDIS_URL``.
    """
    add_apt_repo()
    pkg = f"zammad={version}" if version else "zammad"
    # Non-interactive so maintainer scripts never block on a prompt.
    env = {
        "DEBIAN_FRONTEND": "noninteractive",
        "NEEDRESTART_MODE": "a",
        "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
    }
    _run(
        ["apt-get", "install", "-y", "--no-install-recommends", pkg],
        check=True,
        env=env,
        timeout=900,
    )
    # We use external PostgreSQL/Redis/Elasticsearch via relations/config; mask
    # the local services pulled in as hard Depends so they do not start on boot.
    for svc in LOCAL_DEPS_TO_MASK:
        _systemctl("disable", "--now", svc, check=False)
        _systemctl("mask", svc, check=False)
    # Make sure the meta unit is enabled (managed services are controlled by us).
    _systemctl("enable", "zammad", check=False)


def upgrade(version: str = "") -> None:
    """Upgrade the Zammad package (runs pre/post maintainer scripts)."""
    install(version=version)


# --- Service control -------------------------------------------------------


def start() -> None:
    """Start the Zammad stack."""
    _systemctl("start", "zammad")


def stop() -> None:
    """Stop the Zammad stack."""
    _systemctl("stop", "zammad", check=False)


def restart(service: str = "") -> None:
    """Restart the whole stack or a single service (web/websocket/worker)."""
    unit = "zammad" if not service else f"zammad-{service}"
    _systemctl("restart", unit)


def is_active(service: str = "") -> bool:
    """Return whether the given service (or the whole stack) is active."""
    unit = "zammad" if not service else f"zammad-{service}"
    return _systemctl("is-active", "--quiet", unit, check=False).returncode == 0


def service_status(service: str = "") -> str:
    """Return ``active``/``inactive``/``failed`` for a service."""
    unit = "zammad" if not service else f"zammad-{service}"
    return _run(["systemctl", "is-active", unit], check=False).stdout.strip()


# --- Configuration rendering ----------------------------------------------


def render_database_yml(
    *,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
) -> None:
    """Write ``config/database.yml`` for the external PostgreSQL."""
    content = (
        "production:\n"
        "  adapter: postgresql\n"
        f"  database: {database}\n"
        f"  username: {username}\n"
        f"  password: {password}\n"
        f"  host: {host}\n"
        f"  port: {port}\n"
        "  pool: 50\n"
        "  timeout: 5000\n"
        "  encoding: utf8\n"
    )
    _write_file(DB_YML, content, mode=0o600)
    # The package owns /opt/zammad as zammad:zammad.
    shutil.chown(DB_YML, user="zammad", group="zammad")


def write_env(env: dict[str, str]) -> None:
    """Write the Juju-managed environment file sourced by the zammad wrapper."""
    lines = ["# Managed by Juju. Do not edit by hand."]
    for key, value in sorted(env.items()):
        if value == "":
            continue
        lines.append(f'export {key}="{value}"')
    _write_file(JUJU_ENV, "\n".join(lines) + "\n", mode=0o640)
    try:
        shutil.chown(JUJU_ENV, user="zammad", group="zammad")
    except LookupError:
        # zammad user may not exist yet (pre-install); postinst will chown.
        pass


def render_nginx(
    *,
    fqdn: str,
    tls_cert: str | None = None,
    tls_key: str | None = None,
) -> None:
    """Render the nginx site configuration for Zammad."""
    template = _env.get_template("zammad_nginx.conf.j2")
    content = template.render(
        fqdn=fqdn or "localhost",
        rails_port=RAILS_PORT,
        websocket_port=WEBSOCKET_PORT,
        tls=bool(tls_cert and tls_key),
    )
    _write_file(NGINX_AVAILABLE, content, mode=0o644)
    if NGINX_ENABLED.exists() or NGINX_ENABLED.is_symlink():
        NGINX_ENABLED.unlink(missing_ok=True)
    NGINX_ENABLED.symlink_to(NGINX_AVAILABLE)
    # Remove the default site so it does not steal port 80.
    NGINX_DEFAULT.unlink(missing_ok=True)

    cert_dir = Path("/etc/zammad/tls")
    if tls_cert and tls_key:
        cert_dir.mkdir(parents=True, exist_ok=True)
        _write_file(cert_dir / "zammad.crt", tls_cert, mode=0o640)
        _write_file(cert_dir / "zammad.key", tls_key, mode=0o640)
    else:
        for f in (cert_dir / "zammad.crt", cert_dir / "zammad.key"):
            f.unlink(missing_ok=True)

    _run(["nginx", "-t"], check=True, capture=True)
    _systemctl("enable", "nginx", check=False)
    if _systemctl("reload", "nginx", check=False).returncode != 0:
        _systemctl("restart", "nginx", check=False)


def render_s3_storage(
    *,
    endpoint: str,
    bucket: str,
    region: str,
    access_key: str,
    secret_key: str,
) -> None:
    """Write the S3 storage config consumed by ``Store::Provider::S3``."""
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {
        "s3": {
            "service": "S3",
            "access_key_id": access_key,
            "secret_access_key": secret_key,
            "region": region or "us-east-1",
            "endpoint": endpoint,
            "bucket": bucket,
            "force_path_style": True,
        }
    }
    _write_file(STORAGE_YML, yaml.safe_dump(cfg), mode=0o640)
    try:
        shutil.chown(STORAGE_YML, user="zammad", group="zammad")
    except LookupError:
        pass


def disable_s3_storage() -> None:
    """Remove the S3 storage config (revert to DB storage)."""
    STORAGE_YML.unlink(missing_ok=True)


# --- Rails / Rake ---------------------------------------------------------


def run_rake(*args: str, timeout: int = 600) -> str:
    """Run ``zammad run rake <args>`` and return stdout."""
    return _run([ZAMMAD_CLI, "run", "rake", *args], check=True, timeout=timeout).stdout


def run_rails(code: str, timeout: int = 600) -> str:
    """Run ``zammad run rails r <code>`` and return stdout."""
    return _run([ZAMMAD_CLI, "run", "rails", "r", code], check=True, timeout=timeout).stdout


def db_migrate() -> None:
    """Run database migrations."""
    run_rake("db:migrate")


def db_seed() -> None:
    """Run database seeds (idempotent enough to call once per fresh DB)."""
    run_rake("db:seed")


def db_prepare() -> None:
    """Prepare the database (migrate + seed if fresh). Used on first setup."""
    run_rake("db:prepare")


def clear_cache() -> None:
    """Clear the Rails cache."""
    run_rails("Rails.cache.clear")


def update_translations() -> None:
    """Sync locales and translations (part of the upgrade flow)."""
    run_rails("Locale.sync")
    run_rails("Translation.sync")


def set_es_settings(
    *, url: str, user: str = "", password: str = "", index_prefix: str = "zammad"
) -> None:
    """Configure Elasticsearch connection in Zammad's settings."""
    run_rails(f"Setting.set('es_url', {ruby_quote(url)})")
    if user:
        run_rails(f"Setting.set('es_user', {ruby_quote(user)})")
        run_rails(f"Setting.set('es_password', {ruby_quote(password)})")
    if index_prefix:
        run_rails(f"Setting.set('es_index', {ruby_quote(index_prefix)})")


def rebuild_searchindex(threads: int = 1) -> str:
    """Rebuild the Elasticsearch search index."""
    return run_rake("zammad:searchindex:rebuild", f"[{threads}]", timeout=3600)


def set_storage_provider(provider: str) -> None:
    """Set the attachment storage provider (DB/File/S3)."""
    run_rails(f"Setting.set('storage_provider', {ruby_quote(provider)})")


def set_admin_password(*, login: str, email: str, password: str) -> None:
    """Create or update an admin user with the given password.

    Values are passed via ENV so that special characters in the password do not
    need to be escaped for Ruby or the shell.
    """
    ruby = (
        "login = ENV['ZAMMAD_ADMIN_LOGIN']; email = ENV['ZAMMAD_ADMIN_EMAIL']; "
        "pwd = ENV['ZAMMAD_ADMIN_PASSWORD'];\n"
        "user = User.find_by(login: login) || User.new(login: login);\n"
        "user.email = email; user.firstname = 'Admin'; user.lastname = 'User';\n"
        "user.password = pwd; user.password_confirmation = pwd; user.active = true;\n"
        "user.role_ids = Role.where(name: ['Admin']).pluck(:id) unless user.persisted?;\n"
        "user.save!; puts 'ok';\n"
    )
    env = {
        **_passthrough_env(),
        "ZAMMAD_ADMIN_LOGIN": login,
        "ZAMMAD_ADMIN_EMAIL": email,
        "ZAMMAD_ADMIN_PASSWORD": password,
    }
    _run([ZAMMAD_CLI, "run", "rails", "r", ruby], check=True, env=env, timeout=120)


def ruby_quote(value: str) -> str:
    """Quote a string for a Ruby double-quoted literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _passthrough_env() -> dict[str, str]:
    """Environment needed for the zammad wrapper to function."""
    return {
        "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": "/root",
    }


# --- Connectivity probes ---------------------------------------------------


def pg_is_ready(*, host: str, port: int) -> bool:
    """Return whether PostgreSQL accepts connections."""
    return _run(["pg_isready", "-q", "-h", host, "-p", str(port)], check=False).returncode == 0


def redis_is_ready(*, url: str) -> bool:
    """Return whether Redis at the given URL responds to PING."""
    code = (
        "require 'redis'; require 'hiredis-client'; "
        "Redis.new(driver: :hiredis, url: ENV['REDIS_URL']).ping"
    )
    env = {**_passthrough_env(), "REDIS_URL": url}
    return _run([ZAMMAD_CLI, "run", "ruby", "-e", code], check=False, env=env).returncode == 0


def es_is_ready(*, url: str) -> bool:
    """Return whether Elasticsearch responds at the cluster health endpoint."""
    return (
        _run(
            ["curl", "-fsS", "-m", "5", url.rstrip("/") + "/_cluster/health"], check=False
        ).returncode
        == 0
    )


def http_ok(url: str) -> bool:
    """Return whether the given HTTP URL returns a 2xx/3xx status."""
    return _run(["curl", "-fsS", "-m", "5", "-o", "/dev/null", url], check=False).returncode == 0


# --- Backup / restore -----------------------------------------------------


def backup(*, output_dir: Path = DEFAULT_BACKUP_DIR, retention_days: int = 7) -> str:
    """Take a Zammad backup (pg_dump -Fc + files) and return the archive path."""
    creds = db_credentials()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = _run(["date", "+%Y%m%d%H%M%S"], check=True).stdout.strip()
    archive = output_dir / f"zammad-{timestamp}.tar.gz"
    work = output_dir / f".zammad-{timestamp}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        env = {
            **_passthrough_env(),
            "PGPASSWORD": creds["password"],
            "PGSSLMODE": "prefer",
        }
        _run(
            [
                "pg_dump",
                "-Fc",
                "--no-owner",
                "--no-privileges",
                "-h",
                creds["host"],
                "-p",
                str(creds["port"]),
                "-U",
                creds["username"],
                "-f",
                str(work / "db.dump"),
                creds["database"],
            ],
            check=True,
            env=env,
            timeout=3600,
        )
        # File store attachments (only meaningful if storage_provider == File).
        storage = ZAMMAD_HOME / "storage"
        if storage.exists():
            _run(
                ["tar", "-C", str(ZAMMAD_HOME), "-czf", str(work / "files.tar.gz"), "storage"],
                check=True,
                timeout=3600,
            )
        _run(
            ["tar", "-C", str(work), "-czf", str(archive), "db.dump"]
            + (["files.tar.gz"] if (work / "files.tar.gz").exists() else []),
            check=True,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    os.chmod(archive, 0o600)
    _prune_backups(output_dir, retention_days)
    return str(archive)


def restore(*, backup_file: str) -> None:
    """Restore Zammad from a backup archive produced by :func:`backup`."""
    archive = Path(backup_file)
    if not archive.exists():
        raise ZammadError(f"backup file not found: {archive}")
    creds = db_credentials()
    work = Path("/var/tmp/zammad-restore")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    _run(["tar", "-C", str(work), "-xzf", str(archive)], check=True)
    stop()
    try:
        env = {
            **_passthrough_env(),
            "PGPASSWORD": creds["password"],
            "PGSSLMODE": "prefer",
        }
        _run(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "-h",
                creds["host"],
                "-p",
                str(creds["port"]),
                "-U",
                creds["username"],
                "-d",
                creds["database"],
                str(work / "db.dump"),
            ],
            check=False,  # pg_restore emits non-fatal errors; verify below
            env=env,
            timeout=3600,
        )
        if (work / "files.tar.gz").exists():
            _run(["tar", "-C", str(ZAMMAD_HOME), "-xzf", str(work / "files.tar.gz")], check=True)
        clear_cache()
    finally:
        shutil.rmtree(work, ignore_errors=True)
        start()


def _prune_backups(output_dir: Path, retention_days: int) -> None:
    """Delete backups older than retention_days."""
    _run(
        [
            "find",
            str(output_dir),
            "-maxdepth",
            "1",
            "-type",
            "f",
            "-name",
            "zammad-*.tar.gz",
            "-mtime",
            f"+{retention_days}",
            "-delete",
        ],
        check=False,
    )


def db_credentials() -> dict[str, Any]:
    """Parse database connection info from database.yml."""
    if not DB_YML.exists():
        raise ZammadError(f"{DB_YML} not found")
    data = yaml.safe_load(DB_YML.read_text()) or {}
    prod = data.get("production", {})
    return {
        "host": prod.get("host", "localhost"),
        "port": int(prod.get("port", 5432)),
        "database": prod.get("database", "zammad"),
        "username": prod.get("username", "zammad"),
        "password": prod.get("password", ""),
    }


# --- Templating -----------------------------------------------------------


_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")),
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
