# Zammad Operator

A [Juju](https://juju.is/) machine charm for [Zammad](https://zammad.org/), the
open-source customer support and helpdesk ticketing system.

This charm installs the official Zammad package (packager.io) on Ubuntu 24.04
and manages its systemd services (`zammad-web`, `zammad-websocket`,
`zammad-worker` behind the `zammad` meta unit, fronted by nginx). It composes
with other charmed operators and exposes day-2 operations as Juju actions.

## Architecture

Zammad requires three backing services. This charm provides two of them via
Juju relations and the third via configuration:

| Service        | Provided by                          | Required? |
|----------------|---------------------------------------|-----------|
| PostgreSQL     | `postgresql` charm (`postgresql_client` interface) | Yes (relation) |
| Redis          | charmed Redis provider (`redis_client` interface)   | Yes (relation) |
| Elasticsearch  | external ES 8/9 cluster (`elasticsearch-url` config) | Yes (config) |

> Zammad supports **Elasticsearch 8/9 only** — it does **not** support OpenSearch.
> There is no maintained Elasticsearch machine charm in Charmhub, so ES is wired
> via the `elasticsearch-url` config option (and optional credentials in a Juju
> secret).

Attachment storage defaults to the PostgreSQL database. S3 attachment storage is
supported via the `s3-*` config options.

## Deploy

```bash
# Build the charm
charmcraft pack

# Deploy the dependencies and Zammad
juju deploy ./zammad_*.charm zammad
juju deploy postgresql --channel 14/beta
juju deploy <redis-charm> redis

juju integrate zammad:database postgresql:database
juju integrate zammad:redis redis:redis

# Point Zammad at an external Elasticsearch 8/9 cluster
juju config zammad elasticsearch-url=http://<es-host>:9200
# Optional ES auth (Juju secret with username/password):
#   juju add-secret es-creds username=... password=...
#   juju config zammad elasticsearch-secret-id=<secret-id>

# Optional: TLS via a certificate provider
juju deploy self-signed-certificates
juju integrate zammad:certificates self-signed-certificates

# Optional: observability via grafana-agent
juju deploy grafana-agent
juju integrate zammad:cos-agent grafana-agent:cos-agent

juju wait   # wait for zammad to reach active
```

Set the FQDN Zammad is served on (used for `server_name` and URL generation):

```bash
juju config zammad fqdn=zammad.example.com
```

## Actions (day-2 operations)

| Action                 | Description |
|------------------------|-------------|
| `backup`               | Take a backup (pg_dump + files) to a local directory. |
| `restore`              | Restore Zammad from a backup archive. |
| `rebuild-search-index` | Rebuild the Elasticsearch search index. |
| `upgrade`              | Upgrade the Zammad package (runs migrations + reindex). |
| `set-admin-password`   | Create/reset an admin user's password. |
| `restart`              | Restart the stack or a single service (`web`/`websocket`/`worker`). |
| `health-check`         | Report service, HTTP and dependency (PG/Redis/ES) health. |

Examples:

```bash
juju run zammad/0 backup
juju run zammad/0 rebuild-search-index threads=4
juju run zammad/0 set-admin-password password='S3cret!' login=admin
juju run zammad/0 health-check
```

## Configuration

See `charmcraft.yaml` for the full list of config options. Notable ones:

- `fqdn` — public hostname / IP Zammad is served on.
- `elasticsearch-url` / `elasticsearch-secret-id` — Elasticsearch cluster and credentials.
- `web-concurrency`, `min-threads`, `max-threads` — Puma tuning.
- `memcache-servers` — optional Memcached cache layer.
- `s3-endpoint` / `s3-bucket` / `s3-region` / `s3-secret-id` — S3 attachment storage.
- `backup-retention-days` — local backup retention.

## Development

```bash
# Install dependencies (uses uv)
uv sync --group unit --group lint --group integration

# Run unit tests
tox -e unit            # or: PYTHONPATH=lib:src uv run --group unit pytest tests/unit

# Lint and static checks
tox -e lint

# Pack
charmcraft pack

# Integration tests (requires a bootstrapped LXD Juju controller)
tox -e integration
```

Vendored charm libraries live under `lib/charms/`:

- `charms.data_platform_libs.v0.data_interfaces` (PostgreSQL + Redis relations)
- `charms.grafana_agent.v0.cos_agent` (observability)
- `charms.tls_certificates_interface.v3.tls_certificates` (TLS)

## How Zammad is configured

This charm relies on behaviors verified against the Zammad 7.0 .deb:

- PostgreSQL connection is written to `/opt/zammad/config/database.yml`. The
  file is pre-created **before** `apt install` so the package `postinst` takes
  the "already configured" path (runs `rake db:migrate` against the external DB
  instead of creating a local one).
- Environment (`REDIS_URL`, Puma concurrency, `MEMCACHE_SERVERS`, …) is written
  to `/etc/zammad/conf.d/zz-juju.conf`, which the `zammad` wrapper sources.
- Elasticsearch is configured via Zammad settings (`Setting.set('es_url', …)`).
- nginx site is rendered at `/etc/nginx/sites-available/zammad.conf`.
- The local `postgresql`, `redis-server` and `elasticsearch` services pulled in
  as hard package dependencies are masked, since the charm uses external ones.

## License

Apache-2.0 (see `LICENSE`). Zammad itself is AGPL-3.0; see
https://github.com/zammad/zammad for the upstream project.
