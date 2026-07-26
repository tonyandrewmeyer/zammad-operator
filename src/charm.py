#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the Zammad application."""

from __future__ import annotations

import logging
import socket
from pathlib import Path

import ops
from charms.data_platform_libs.v0.data_interfaces import DatabaseCreatedEvent, DatabaseRequires
from charms.grafana_agent.v0.cos_agent import COSAgentProvider
from charms.tls_certificates_interface.v3.tls_certificates import (
    TLSCertificatesRequiresV3,
    generate_csr,
    generate_private_key,
)

import zammad

logger = logging.getLogger(__name__)

PEER = "peers"
DB_RELATION = "database"
REDIS_RELATION = "redis"
CERT_RELATION = "certificates"
COS_RELATION = "cos-agent"

ZAMMAD_DB_NAME = "zammad"


class ZammadCharm(ops.CharmBase):
    """Charm the Zammad application."""

    _stored = ops.StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self._stored.set_default(
            installed=False,
            db_initialized=False,
            db_host="",
            db_port=0,
            db_name="",
            db_user="",
            db_pass="",
            redis_url="",
            es_configured=False,
            tls_ready=False,
            s3_enabled=False,
        )

        self.database = DatabaseRequires(self, DB_RELATION, ZAMMAD_DB_NAME)
        self.redis = DatabaseRequires(self, REDIS_RELATION, ZAMMAD_DB_NAME)
        self.tls = TLSCertificatesRequiresV3(self, CERT_RELATION)
        # Zammad has no native Prometheus metrics endpoint; the grafana-agent
        # subordinate still collects host node metrics and ships dashboards/
        # alert rules for the application.
        self.cos = COSAgentProvider(
            self,
            relation_name=COS_RELATION,
            metrics_endpoints=[],
            dashboard_dirs=["./src/grafana_dashboards"],
            refresh_events=[self.on.config_changed],
        )

        # Lifecycle
        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.update_status, self._on_update_status)
        framework.observe(self.on.stop, self._on_stop)

        # Relations
        framework.observe(self.database.on.database_created, self._on_db_changed)
        framework.observe(self.database.on.endpoints_changed, self._on_db_changed)
        framework.observe(self.redis.on.database_created, self._on_redis_changed)
        framework.observe(self.redis.on.endpoints_changed, self._on_redis_changed)
        framework.observe(self.on[CERT_RELATION].relation_joined, self._on_cert_relation)
        framework.observe(self.tls.on.certificate_available, self._on_cert_available)

        # Actions
        self._observe_actions()

        self.unit.set_workload_version(zammad.get_version() or "")

    # ------------------------------------------------------------------
    # Lifecycle handlers
    # ------------------------------------------------------------------

    def _on_install(self, _event: ops.InstallEvent) -> None:
        """Add the apt repository so the package is ready to install."""
        self.unit.status = ops.MaintenanceStatus("adding Zammad package repository")
        try:
            zammad.add_apt_repo()
        except zammad.ZammadError as exc:
            self.unit.status = ops.BlockedStatus(f"failed to add apt repo: {exc}")
            return
        self.unit.status = ops.WaitingStatus("waiting for database and redis relations")

    def _on_config_changed(self, _event: ops.ConfigChangedEvent) -> None:
        self._configure_logging()
        self._reconcile()

    def _on_start(self, _event: ops.StartEvent) -> None:
        self._reconcile()

    def _on_update_status(self, _event: ops.UpdateStatusEvent) -> None:
        self._reconcile()

    def _on_stop(self, _event: ops.StopEvent) -> None:
        zammad.stop()

    # ------------------------------------------------------------------
    # Relation handlers
    # ------------------------------------------------------------------

    def _on_db_changed(self, event: DatabaseCreatedEvent) -> None:
        """Capture PostgreSQL credentials and reconcile."""
        endpoints = event.endpoints or ""
        host, port = _split_endpoint(endpoints)
        if not host or not event.username:
            self._stored.db_host = ""
            self.unit.status = ops.WaitingStatus("waiting for PostgreSQL credentials")
            return
        self._stored.db_host = host
        self._stored.db_port = port or 5432
        self._stored.db_name = event.database or ZAMMAD_DB_NAME
        self._stored.db_user = event.username or ""
        self._stored.db_pass = event.password or ""
        self._reconcile()

    def _on_redis_changed(self, event: DatabaseCreatedEvent) -> None:
        """Capture the Redis URL and reconcile."""
        uris = event.uris or ""
        if not uris:
            self._stored.redis_url = ""
            self.unit.status = ops.WaitingStatus("waiting for Redis URL")
            return
        self._stored.redis_url = uris
        self._reconcile()

    def _on_cert_relation(self, _event: ops.RelationJoinedEvent) -> None:
        """Request a TLS certificate for the configured FQDN."""
        self._request_certificate()

    def _on_cert_available(self, _event: ops.EventBase) -> None:
        """Render nginx with the provided TLS certificate."""
        self._stored.tls_ready = False
        certs = self.tls.get_assigned_certificates()
        if not certs:
            return
        cert = certs[0]
        key = self._read_private_key()
        if not cert.certificate or not key:
            return
        zammad.render_nginx(fqdn=self._fqdn, tls_cert=cert.certificate, tls_key=key)
        self._stored.tls_ready = True
        self._reconcile()

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def _reconcile(self) -> None:
        """Bring Zammad to its desired state based on relations and config."""
        if not self._stored.db_host:
            self.unit.status = ops.BlockedStatus(
                "relate a PostgreSQL charm: juju integrate zammad:database <postgresql>:database"
            )
            return
        if not self._stored.redis_url:
            self.unit.status = ops.BlockedStatus(
                "relate a Redis charm: juju integrate zammad:redis <redis>:redis"
            )
            return

        # First-time install: pre-render config so the package postinst takes
        # the "already configured" path (external DB, external Redis).
        if not zammad.is_installed():
            self.unit.status = ops.MaintenanceStatus("installing Zammad")
            self._render_database_yml()
            self._render_env()
            try:
                zammad.install()
            except zammad.ZammadError as exc:
                self.unit.status = ops.BlockedStatus(f"install failed: {exc}")
                return
            self._stored.installed = True
            self.unit.set_workload_version(zammad.get_version() or "")

        # Keep runtime config in sync (idempotent).
        self._render_database_yml()
        self._render_env()
        self._render_nginx()
        self._apply_storage()

        # Elasticsearch is required for Active.
        if not self._configure_elasticsearch():
            self.unit.status = ops.BlockedStatus(
                "set 'elasticsearch-url' to a reachable Elasticsearch 8/9 cluster"
            )
            # Zammad can run without ES (degraded search) — keep it up.
            zammad.start()
            return

        try:
            zammad.restart()
        except zammad.ZammadError as exc:
            self.unit.status = ops.MaintenanceStatus(f"restarting: {exc}")
            return

        if zammad.is_active():
            self.unit.status = ops.ActiveStatus(f"Zammad {zammad.get_version()} ready")
        else:
            self.unit.status = ops.MaintenanceStatus("starting Zammad services")

    def _configure_elasticsearch(self) -> bool:
        """Configure Elasticsearch. Returns True if configured and reachable."""
        url = str(self.config.get("elasticsearch-url", "")).strip()
        if not url:
            # If previously configured but URL was removed, mark unconfigured.
            self._stored.es_configured = False
            return False
        if not zammad.es_is_ready(url=url):
            self._stored.es_configured = False
            return False
        if not self._stored.es_configured:
            try:
                user, password = self._es_credentials()
                zammad.set_es_settings(
                    url=url,
                    user=user,
                    password=password,
                    index_prefix=str(self.config.get("elasticsearch-index-prefix", "zammad")),
                )
                self._stored.es_configured = True
            except zammad.ZammadError as exc:
                logger.warning("failed to configure Elasticsearch: %s", exc)
                return False
        return True

    def _es_credentials(self) -> tuple[str, str]:
        """Fetch optional Elasticsearch credentials from a Juju secret."""
        secret_id = str(self.config.get("elasticsearch-secret-id", "")).strip()
        if not secret_id:
            return "", ""
        try:
            secret = self.model.get_secret(id=secret_id)
            content = secret.get_content()
            return content.get("username", ""), content.get("password", "")
        except (ops.SecretNotFoundError, ops.model.ModelError) as exc:
            logger.warning("could not read elasticsearch secret: %s", exc)
            return "", ""

    def _render_database_yml(self) -> None:
        if not self._stored.db_host:
            return
        try:
            zammad.render_database_yml(
                host=self._stored.db_host,
                port=int(self._stored.db_port) or 5432,
                database=self._stored.db_name or ZAMMAD_DB_NAME,
                username=self._stored.db_user,
                password=self._stored.db_pass,
            )
        except zammad.ZammadError as exc:
            logger.warning("could not render database.yml: %s", exc)

    def _render_env(self) -> None:
        env = {
            "REDIS_URL": self._stored.redis_url,
            "ZAMMAD_WEB_CONCURRENCY": str(self.config.get("web-concurrency", 0)),
            "MIN_THREADS": str(self.config.get("min-threads", 5)),
            "MAX_THREADS": str(self.config.get("max-threads", 30)),
            "MEMCACHE_SERVERS": str(self.config.get("memcache-servers", "")),
            "RAILS_TRUSTED_PROXIES": str(self.config.get("trusted-proxies", "127.0.0.1,::1")),
            "ZAMMAD_BIND_IP": "127.0.0.1",
            "ZAMMAD_RAILS_PORT": str(zammad.RAILS_PORT),
            "ZAMMAD_WEBSOCKET_PORT": str(zammad.WEBSOCKET_PORT),
        }
        try:
            zammad.write_env(env)
        except zammad.ZammadError as exc:
            logger.warning("could not write environment: %s", exc)

    def _render_nginx(self) -> None:
        cert = key = None
        if self._stored.tls_ready:
            certs = self.tls.get_assigned_certificates()
            if certs:
                cert = certs[0].certificate
                key = self._read_private_key()
        try:
            zammad.render_nginx(fqdn=self._fqdn, tls_cert=cert, tls_key=key)
        except zammad.ZammadError as exc:
            logger.warning("could not render nginx config: %s", exc)

    def _apply_storage(self) -> None:
        """Apply S3 attachment storage configuration when configured."""
        endpoint = str(self.config.get("s3-endpoint", "")).strip()
        bucket = str(self.config.get("s3-bucket", "")).strip()
        secret_id = str(self.config.get("s3-secret-id", "")).strip()
        if endpoint and bucket:
            access_key, secret_key = self._s3_credentials(secret_id)
            if access_key and secret_key:
                try:
                    zammad.render_s3_storage(
                        endpoint=endpoint,
                        bucket=bucket,
                        region=str(self.config.get("s3-region", "")),
                        access_key=access_key,
                        secret_key=secret_key,
                    )
                    zammad.set_storage_provider("S3")
                except zammad.ZammadError as exc:
                    logger.warning("could not configure S3 storage: %s", exc)
        else:
            try:
                zammad.disable_s3_storage()
                # Only flip back to DB if we previously set S3.
                if self._stored.s3_enabled:
                    zammad.set_storage_provider("DB")
                    self._stored.s3_enabled = False
            except zammad.ZammadError as exc:
                logger.warning("could not disable S3 storage: %s", exc)

    def _s3_credentials(self, secret_id: str) -> tuple[str, str]:
        if not secret_id:
            return "", ""
        try:
            secret = self.model.get_secret(id=secret_id)
            content = secret.get_content()
            return content.get("access-key", ""), content.get("secret-key", "")
        except (ops.SecretNotFoundError, ops.model.ModelError) as exc:
            logger.warning("could not read S3 secret: %s", exc)
            return "", ""

    # ------------------------------------------------------------------
    # TLS helpers
    # ------------------------------------------------------------------

    def _request_certificate(self) -> None:
        """Generate a private key + CSR and request a certificate."""
        key = self._read_private_key()
        if key is None:
            key_pem = generate_private_key()
            self._write_private_key(key_pem)
        else:
            key_pem = key.encode()
        csr = generate_csr(
            private_key=key_pem, subject=self._fqdn, organization="Zammad", sans_dns=[self._fqdn]
        )
        self.tls.request_certificate_creation(csr)

    def _private_key_path(self) -> Path:
        return Path("/etc/zammad/tls/private.key")

    def _write_private_key(self, pem: bytes) -> None:
        path = self._private_key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pem)
        path.chmod(0o600)

    def _read_private_key(self) -> str | None:
        path = self._private_key_path()
        if path.exists():
            return path.read_text()
        return None

    # ------------------------------------------------------------------
    # Properties / small helpers
    # ------------------------------------------------------------------

    @property
    def _fqdn(self) -> str:
        return str(self.config.get("fqdn", "")).strip() or socket.getfqdn()

    def _configure_logging(self) -> None:
        level = str(self.config.get("log-level", "info")).upper()
        logging.getLogger().setLevel(getattr(logging, level, logging.INFO))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _observe_actions(self) -> None:
        actions = {
            "backup": self._on_backup,
            "restore": self._on_restore,
            "rebuild-search-index": self._on_rebuild_search_index,
            "upgrade": self._on_upgrade,
            "set-admin-password": self._on_set_admin_password,
            "restart": self._on_restart,
            "health-check": self._on_health_check,
        }
        for name, handler in actions.items():
            self.framework.observe(self.on[name].action, handler)

    def _on_backup(self, event: ops.ActionEvent) -> None:
        if not self._ensure_ready(event):
            return
        output_dir = Path(event.params.get("output-dir", str(zammad.DEFAULT_BACKUP_DIR)))
        retention = int(self.config.get("backup-retention-days", 7))
        event.log(f"creating Zammad backup in {output_dir}")
        try:
            archive = zammad.backup(output_dir=output_dir, retention_days=retention)
        except zammad.ZammadError as exc:
            event.fail(f"backup failed: {exc}")
            return
        event.set_results({"backup-file": archive})

    def _on_restore(self, event: ops.ActionEvent) -> None:
        backup_file = event.params["backup-file"]
        event.log(f"restoring Zammad from {backup_file}")
        try:
            zammad.restore(backup_file=backup_file)
        except zammad.ZammadError as exc:
            event.fail(f"restore failed: {exc}")
            return
        event.set_results({"restored-from": backup_file})

    def _on_rebuild_search_index(self, event: ops.ActionEvent) -> None:
        if not self._ensure_ready(event):
            return
        threads = int(event.params.get("threads", 1))
        event.log("rebuilding Elasticsearch search index")
        try:
            output = zammad.rebuild_searchindex(threads=threads)
        except zammad.ZammadError as exc:
            event.fail(f"search index rebuild failed: {exc}")
            return
        event.set_results({"output": output[-2000:]})

    def _on_upgrade(self, event: ops.ActionEvent) -> None:
        version = event.params.get("version", "")
        event.log(f"upgrading Zammad{' to ' + version if version else ''}")
        self.unit.status = ops.MaintenanceStatus("upgrading Zammad")
        try:
            zammad.stop()
            zammad.upgrade(version=version)
            zammad.db_migrate()
            zammad.update_translations()
            zammad.clear_cache()
            if self._stored.es_configured:
                event.log("rebuilding search index after upgrade")
                zammad.rebuild_searchindex(threads=1)
        except zammad.ZammadError as exc:
            event.fail(f"upgrade failed: {exc}")
            self._reconcile()
            return
        self.unit.set_workload_version(zammad.get_version() or "")
        self._reconcile()
        event.set_results({"version": zammad.get_version()})

    def _on_set_admin_password(self, event: ops.ActionEvent) -> None:
        if not zammad.is_installed():
            event.fail("Zammad is not installed yet")
            return
        login = event.params.get("login", "admin")
        email = event.params.get("email", "admin@example.com")
        password = event.params["password"]
        try:
            zammad.set_admin_password(login=login, email=email, password=password)
        except zammad.ZammadError as exc:
            event.fail(f"could not set admin password: {exc}")
            return
        event.set_results({"login": login})

    def _on_restart(self, event: ops.ActionEvent) -> None:
        service = event.params.get("service", "")
        try:
            zammad.restart(service=service)
        except zammad.ZammadError as exc:
            event.fail(f"restart failed: {exc}")
            return
        event.set_results({"restarted": service or "zammad"})

    def _on_health_check(self, event: ops.ActionEvent) -> None:
        result: dict[str, object] = {}
        result["installed"] = zammad.is_installed()
        result["zammad_active"] = zammad.is_active()
        result["web_active"] = zammad.is_active("web")
        result["websocket_active"] = zammad.is_active("websocket")
        result["worker_active"] = zammad.is_active("worker")
        result["nginx_active"] = zammad.service_status("nginx") == "active"
        result["http"] = zammad.http_ok("http://127.0.0.1:80/")
        if self._stored.db_host:
            result["postgres"] = zammad.pg_is_ready(
                host=self._stored.db_host, port=int(self._stored.db_port) or 5432
            )
        if self._stored.redis_url:
            result["redis"] = zammad.redis_is_ready(url=self._stored.redis_url)
        url = str(self.config.get("elasticsearch-url", ""))
        if url:
            result["elasticsearch"] = zammad.es_is_ready(url=url)
        event.set_results(result)

    def _ensure_ready(self, event: ops.ActionEvent) -> bool:
        if not zammad.is_installed():
            event.fail("Zammad is not installed yet")
            return False
        return True


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _split_endpoint(endpoint: str) -> tuple[str, int]:
    """Split an ``endpoints`` value (``host:port``) into host and port."""
    if not endpoint:
        return "", 0
    # endpoints may be comma-separated; take the first (primary).
    first = endpoint.split(",")[0].strip()
    if ":" in first:
        host, _, port = first.rpartition(":")
        try:
            return host, int(port)
        except ValueError:
            return host, 0
    return first, 0


if __name__ == "__main__":  # pragma: nocover
    ops.main(ZammadCharm)
