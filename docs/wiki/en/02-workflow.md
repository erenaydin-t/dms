# Document Lifecycle & Workflow

Every `GMP Document` moves through the native Frappe workflow **GMP Document Workflow** (the form's *Actions* menu). Direct submission is blocked — the only path to an approved, submitted document is through the workflow.

## States

| State | docstatus | Waiting on |
|---|---|---|
| **Draft** | 0 | the preparer, authoring |
| **Pending Supervisor Approval** | 0 | the preparer's supervisor (`Employee.reports_to`) |
| **Under Review** | 0 | the Reviewer — the supervisor's own manager |
| **Pending QA Supervisor** | 0 | the QA Supervisor (DMS Settings), who approves or delegates |
| **QA Review In Progress** | 0 | the delegated QA reviewers, one after another (driven by the queue, not by actions) |
| **Pending Regulatory Validation** | 0 | the Regulatory Manager (DMS Settings) |
| **Pending Manager Approval** | 0 | the Reviewer again, now as Manager |
| **Pending Final QA Approval** | 0 | the QA Approver (DMS Settings) |
| **Approved** | 1 | QA-approved and submitted. If `is_active = 1`, it is the effective version; if `is_active = 0` with a future Effective Date, it is **pending** (scheduled) |
| **Revision Requested** | 0 | the preparer, after a return |
| **Revision Cancelled** | 0 | Terminal: an abandoned draft revision, retained for audit (cannot be deleted) |
| **Obsolete** | 1 or 2 | Retired: superseded by an approved revision, expired, or manually cancelled |

The supervisor and reviewer are resolved from the preparer's Employee reporting chain when the draft is submitted; the QA Supervisor, Regulatory Manager and QA Approver come from **DMS Settings** (global, or per department).

## Transitions and authorization

Each action needs the role in the *Role* column **plus** a per-actor condition — only the person the document is waiting on (or Administrator) can act. A **DMS Proxy Approver** can take any of them on that person's behalf (see *Roles*).

| From | Action | To | Role · who |
|---|---|---|---|
| Draft / Revision Requested | Submit for Approval | Pending Supervisor Approval | DMS Initiator · the **preparer** |
| Pending Supervisor Approval | Approve (Supervisor) / Return to Preparer | Under Review / Revision Requested | DMS Approver · the **supervisor** |
| Under Review | Approve as Reviewer / Return to Supervisor / Return to Preparer | Pending QA Supervisor / … | DMS Approver · the **reviewer** |
| Pending QA Supervisor | Approve (QA Supervisor) / Return to Reviewer / Return to Preparer | Pending Regulatory Validation / … | DMS Approver · the **QA supervisor** |
| QA Review In Progress | Recall Delegation | Pending QA Supervisor | DMS Approver · the QA supervisor |
| Pending Regulatory Validation | Validate (Regulatory) / Return to QA Supervisor | Pending Manager Approval / … | DMS Approver · the **regulatory manager** |
| Pending Manager Approval | Approve (Manager) / Return to Regulatory | Pending Final QA Approval / … | DMS Approver · the **reviewer** |
| Pending Final QA Approval | Publish / Return to Manager | Approved *(auto-submits)* / … | QA Manager · the **QA approver** |
| Draft / Revision Requested / Pending Supervisor Approval | Cancel Revision | Revision Cancelled | DMS Initiator · preparer or QA approver — only on drafts created via **Create Revision** |

**Every Return asks for a reason.** The form prompts for it when a *Return …* action is chosen; it is recorded in the timeline with the person and time, and shown on the form (*Latest Revision Request*) until the document moves forward again.

**Who can edit.** On a draft, only the person the document is waiting on can save changes (plus the unrestricted roles); anyone else named on it can read it. Fields the system writes — the approval stamps, signatures, *on behalf of* records, the CEO block, *Revision Of*, the integrity hash — cannot be changed by a save, from the form or the API.

Publication (`Publish`) triggers, in order:

1. approver stamping (`approved_by` / `approved_on`) and the CEO block,
2. effective-date stamping (approval date, unless manually scheduled — see *Effective Dates*),
3. template rendering: clean deliverable + signed base PDF (signatures + QA stamp),
4. retirement of the superseded predecessor (revision flow, unless future-dated),
5. repointing of dependents' live references to this version (*Supersedes* and *Derived From* rows, and references held by retired documents, keep the version they name).

## Signatures (21 CFR Part 11)

- The assigned Reviewer and QA Approver **must** have a signature image configured on their Employee record (`Employee → custom_signature_image`, PNG/JPG) — saving the document is blocked otherwise.
- Rendered PDFs embed the **actual actor's** signature (`reviewed_by` / `approved_by`), falling back to the assigned reviewer/approver when an administrator acted via the escape hatch.
- Signature files must live on **local disk** (see the S3 note in *Overview*).

## Audit trail

- Every transition writes a Workflow comment (who, when, what).
- ToDos hand off automatically from each actor to the next.
- Obsoleted and cancelled-revision records are retained permanently; deletion of a cancelled revision is blocked server-side.
