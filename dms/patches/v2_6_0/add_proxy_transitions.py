# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""v2.6.0: give an existing GMP Document Workflow the delegated-approval twins
that the new "DMS Proxy Approver" role acts through.

A fresh install gets these from the seed in `_ensure_gmp_workflow()`. Sites
that already have a workflow do not — and since v2.6.0 the workflow is seeded
rather than synchronised, nothing else will ever add them. Without this patch
the role would exist but be able to do precisely nothing, which is a worse
outcome than either behaviour on its own.

This is a ONE-TIME, STRICTLY ADDITIVE migration, not a resynchronisation:

* it only ever APPENDS rows — no existing transition, state, condition, role
  or routing is edited or deleted;
* it mirrors the transitions the SITE actually has, not the ones we ship. The
  twin copies that row's own `next_state` and `allow_self_approval`, so a site
  that re-routed its chain gets twins following ITS routing;
* a step the site removed stays removed — no row is resurrected;
* a row whose `condition` the site rewrote is skipped rather than guessed at,
  because the twin's condition is derived from which actor the original pins
  to, and an unrecognised condition no longer answers that;
* it is idempotent, and does nothing at all when no workflow exists.

Sites that do not want delegated approval can simply never grant the role, or
delete the twin rows from the Workflow page afterwards — nothing will put them
back.
"""

import frappe

from dms.install import (
    DMS_PROXY_ROLE,
    GMP_WORKFLOW_NAME,
    _ACTOR_FIELD_BY_CONDITION,
    _proxy_for,
)


def execute():
    if not frappe.db.exists("Workflow", GMP_WORKFLOW_NAME):
        return

    wf = frappe.get_doc("Workflow", GMP_WORKFLOW_NAME)
    have = {(t.state, t.action, t.allowed) for t in wf.transitions}

    added = []
    for row in list(wf.transitions):
        if row.allowed == DMS_PROXY_ROLE:
            continue
        actor_field = _ACTOR_FIELD_BY_CONDITION.get((row.condition or "").strip())
        if not actor_field:
            continue  # not one of our actor-gated rows, or the site rewrote it
        if (row.state, row.action, DMS_PROXY_ROLE) in have:
            continue

        if not frappe.db.exists("Workflow Action Master", row.action):
            frappe.get_doc(
                {"doctype": "Workflow Action Master", "workflow_action_name": row.action}
            ).insert(ignore_permissions=True)

        wf.append(
            "transitions",
            {
                "state": row.state,
                "action": row.action,
                "next_state": row.next_state,
                "allowed": DMS_PROXY_ROLE,
                "condition": _proxy_for(actor_field),
                "allow_self_approval": row.allow_self_approval,
            },
        )
        have.add((row.state, row.action, DMS_PROXY_ROLE))
        added.append(f"{row.state} / {row.action}")

    if not added:
        return

    wf.save(ignore_permissions=True)
    # get_workflow_name() caches; without this the new rows stay invisible
    # until the worker recycles.
    frappe.cache.delete_key("workflow")
    print(
        f"dms: added {len(added)} delegated-approval transition(s) for "
        f"'{DMS_PROXY_ROLE}': " + ", ".join(added)
    )
