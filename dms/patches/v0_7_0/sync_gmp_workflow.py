"""Issues #2 / #6: add the 'Obsolete' workflow state and (re)assert the
per-actor transition conditions on the existing GMP Document Workflow.

_ensure_gmp_workflow() is a no-op when the workflow already exists, so this
patch restores the shipped definition to upgrade installs created before
v0.7.0.

(Historical. Since v2.6.0 the workflow is seeded once and then owned by the
site; restore_workflow_defaults() — this patch's target, renamed from
_sync_gmp_workflow — is no longer called by install or migrate. This patch has
long since run everywhere it applies, and re-running it is harmless.)
"""

from dms.install import restore_workflow_defaults


def execute():
    restore_workflow_defaults()
