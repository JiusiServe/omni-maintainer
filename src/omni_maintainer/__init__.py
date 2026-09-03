"""omni-maintainer: policy-enforcing tooling for the self-maintenance routines.

The package is split by trust level:

- ``gate``: what the GitHub Actions gate workflow runs. It decides, from
  GitHub state alone, whether a pull request may merge. Nothing here trusts a
  routine.
- ``monitor``: what the hourly monitor routine runs against the production
  dashboards and CI: fingerprints, canary evaluation, rollback bookkeeping.
- ``routine``: helpers for the routines themselves (preflight, work queue,
  ledger rendering, the ``gh`` wrapper with dry-run).

Runtime is standard-library only.
"""

__version__ = "0.1.0"
