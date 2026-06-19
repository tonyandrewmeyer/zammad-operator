# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Integration tests for the Zammad charm on LXD.
#
# The full active path needs three relations: PostgreSQL, Redis and
# Elasticsearch 8/9. PostgreSQL is provided by the charmed `postgresql` VM
# charm (deployable from the store). There is no published machine Redis charm
# release in this store, and Zammad does not support OpenSearch, so Elasticsearch
# has no maintained machine charm either. The tests below therefore verify the
# parts that *can* be exercised here: the charm packs, deploys, installs the
# Zammad package, wires the PostgreSQL relation, and correctly reports the
# relations-only requirements (Blocked until Redis is related, and until
# `elasticsearch-url` is configured).

from __future__ import annotations

import logging
import pathlib

import jubilant
import pytest

logger = logging.getLogger(__name__)

POSTGRES_CHANNEL = "14/beta"


def test_deploy_with_postgres_blocks_on_redis(charm: pathlib.Path, juju: jubilant.Juju):
    """Deploy Zammad + PostgreSQL, relate, and assert it blocks waiting for Redis.

    This exercises the real install path (apt repo, package install, systemd
    units), the PostgreSQL relation wiring, and the relations-only status logic.
    """
    juju.deploy(charm.resolve(), app="zammad")
    juju.deploy("postgresql", channel=POSTGRES_CHANNEL, config={"profile": "testing"})

    juju.integrate("zammad:database", "postgresql:database")

    # The charm installs Zammad (heavy: pulls PG/Redis/ES deps via apt) and then
    # blocks because the Redis relation is absent (relations-only model).
    status = juju.wait(
        lambda s: _zammad_blocked(s, "Redis"),
        timeout=60 * 50,
        successes=2,
    )
    assert "Redis" in status.apps["zammad"].app_status.message


def test_health_check_action_runs(charm: pathlib.Path, juju: jubilant.Juju):
    """The health-check action executes and reports a structured result."""
    # Reuse the deployment from the previous test (same model).
    result = juju.run("zammad/0", "health-check")
    # Zammad is installed by the first test; services may be up or starting.
    assert "installed" in result.results


def _zammad_blocked(status: jubilant.Status, needle: str) -> bool:
    app = status.apps.get("zammad")
    if app is None:
        return False
    return app.is_blocked and needle in (app.app_status.message or "")


@pytest.mark.skip(
    reason=(
        "Full active path requires a machine Redis charm (not in this store) "
        "and an external Elasticsearch 8/9 cluster."
    )
)
def test_full_active_path(charm: pathlib.Path, juju: jubilant.Juju):
    """Placeholder for the full active-path test.

    To run: relate a charmed Redis provider to `zammad:redis` and set
    `elasticsearch-url` to a reachable ES 8/9 cluster, then wait for active.
    """
