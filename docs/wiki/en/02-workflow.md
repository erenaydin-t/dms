# Document Lifecycle & Workflow

Every `GMP Document` moves through the native Frappe workflow **GMP Document Workflow** (the form's *Actions* menu). Direct submission is blocked — the only path to an approved, submitted document is through the workflow.

## States

| State | docstatus | Meaning |
|---|---|---|
| **Draft** | 0 | Being authored by the preparer |
| **Under Review** | 0 | Waiting for the assigned Reviewer |
| **Pending QA Approval** | 0 | Waiting for the assigned QA Approver |
| **Approved** | 1 | QA-approved and submitted. If `is_active = 1`, it is the effective version; if `is_active = 0` with a future Effective Date, it is **pending** (scheduled) |
| **Revision Requested** | 0 | Bounced back to the preparer with comments |
| **Revision Cancelled** | 0 | Terminal: an abandoned draft revision, retained for audit (cannot be deleted) |
| **Obsolete** | 1 or 2 | Retired: superseded by an approved revision, expired, or manually cancelled |

## Transitions and authorization

Every transition requires the **QA Manager** role *plus* a per-actor condition — only the assigned person (or Administrator) can act:

| From | Action | To | Who |
|---|---|---|---|
| Draft / Revision Requested | Submit for Review | Under Review | the **preparer** (`prepared_by`) |
| Under Review | Approve as Reviewer | Pending QA Approval | the assigned **Reviewer** |
| Under Review | Request Revision (Reviewer) | Revision Requested | the assigned Reviewer |
| Pending QA Approval | Approve as QA | Approved *(auto-submits)* | the assigned **QA Approver** |
| Pending QA Approval | Request Revision (QA) | Under Review | the assigned QA Approver |
| Draft / Under Review / Pending QA / Revision Requested | Cancel Revision | Revision Cancelled | preparer or QA approver — only on drafts created via **Create Revision** |

Approval (`Approve as QA`) triggers, in order:

1. approver stamping (`approved_by` / `approved_on`),
2. effective-date stamping (approval date, unless manually scheduled — see *Effective Dates*),
3. template rendering: clean deliverable + signed base PDF (signatures + QA stamp),
4. retirement of the superseded predecessor (revision flow, unless future-dated),
5. repointing of dependents' references to this version.

## Signatures (21 CFR Part 11)

- The assigned Reviewer and QA Approver **must** have a signature image configured on their Employee record (`Employee → custom_signature_image`, PNG/JPG) — saving the document is blocked otherwise.
- Rendered PDFs embed the **actual actor's** signature (`reviewed_by` / `approved_by`), falling back to the assigned reviewer/approver when an administrator acted via the escape hatch.
- Signature files must live on **local disk** (see the S3 note in *Overview*).

## Audit trail

- Every transition writes a Workflow comment (who, when, what).
- ToDos hand off automatically between preparer → reviewer → QA approver.
- Obsoleted and cancelled-revision records are retained permanently; deletion of a cancelled revision is blocked server-side.
