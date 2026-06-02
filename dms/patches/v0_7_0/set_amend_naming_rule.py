"""Issue #3: amended GMP Documents must run autoname() (…-v1), not the
framework's default `-1` counter.

Registers (or fixes) the per-doctype 'Default Naming' row in Document Naming
Settings for GMP Document. Delegates to the idempotent installer helper so the
fresh-install and existing-install paths share one implementation.
"""

from dms.install import _ensure_amend_naming_rule


def execute():
    _ensure_amend_naming_rule()
