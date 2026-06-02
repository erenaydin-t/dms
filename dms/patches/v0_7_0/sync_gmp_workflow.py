"""Issues #2 / #6: add the 'Obsolete' workflow state and (re)assert the
per-actor transition conditions on the existing GMP Document Workflow.

_ensure_gmp_workflow() is a no-op when the workflow already exists, so this
patch runs _sync_gmp_workflow() to upgrade installs created before v0.7.0.
"""

from dms.install import _sync_gmp_workflow


def execute():
    _sync_gmp_workflow()
