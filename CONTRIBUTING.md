# Contributing

Thanks for your interest in improving the Zammad charm! This charm follows the
[Canonical charm development guidelines](https://documentation.ubuntu.com/ops/latest/).

## Development setup

```shell
uv sync --group unit --group lint --group integration
```

You'll need a bootstrapped Juju controller (LXD works for machine charms):

```shell
juju bootstrap lxd
juju add-model zammad
```

## Testing

This project uses `tox`:

```shell
tox run -e format        # auto-format
tox run -e lint          # ruff + codespell + pyright
tox run -e unit          # unit tests
tox run -e integration   # integration tests on LXD
tox                      # format + lint + unit
```

Unit tests use `ops.testing.Harness` and mock the workload module (`src/zammad.py`)
so they run without a Juju controller. The workload module is also tested
directly with mocked `subprocess` calls.

Integration tests deploy the charm with the charmed `postgresql` VM charm on LXD.
The full active path additionally needs a charmed Redis provider and an external
Elasticsearch 8/9 cluster.

## Build

```shell
charmcraft pack
```

## Code style

- Python is formatted with `ruff format` and checked with `ruff` + `pyright`.
- Docstrings use imperative mood (Google style).
- Keep `src/zammad.py` free of charming concerns so it stays unit-testable.
- Vendored charm libraries live under `lib/charms/` and must not be hand-edited;
  update them by re-fetching from their source repository.

## Committing

This repo uses conventional, descriptive commit messages. Commit at logical
points and keep the build green on `main`.
