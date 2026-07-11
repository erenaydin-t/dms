# Roles & Permissions

The DMS module uses **three module roles** plus the standard *Employee* role for read-only consumers. Role names are canonical **English** names — do not rename the Role records (see *Special considerations*).

## Role summary

| Role | Mandatory? | Purpose |
|---|---|---|
| **QA Manager** | ✅ Yes — the working role | Authors and workflow actors: everyone who prepares, reviews or QA-approves controlled documents |
| **DMS Manager** | ✅ Yes (at least one holder recommended) | Module owner/administrator: full control incl. delete, cross-department oversight, source-file distribution |
| **System Manager** | Built-in | IT administration; same unrestricted DMS access as DMS Manager |
| **Employee** | Optional | Read-only consumers: see the *approved, active* documents of their own department |

`Administrator` implicitly passes every role and per-actor gate (escape hatch) — fine for emergencies, wrong for daily use, since audit trails should name real actors.

---

## QA Manager

**Purpose:** the day-to-day working role. Every preparer, reviewer and QA approver must hold it.

**DocType permissions:** read, write, create, submit, cancel, amend, report, export, print, email, share — **no delete**.

**Visibility:** unrestricted — sees all documents in every department and state.

**Workflow actions** (all additionally gated by the *per-actor condition* — holding the role is necessary but not sufficient):

| Action | Extra condition |
|---|---|
| Submit for Review | must be the document's **preparer** (`prepared_by`) |
| Approve as Reviewer / Request Revision (Reviewer) | must be the assigned **Reviewer** |
| Approve as QA / Request Revision (QA) | must be the assigned **QA Approver** |
| Cancel Revision | must be the preparer **or** QA approver, and only on drafts created via *Create Revision* |

**Other capabilities:** Create Revision button; *Edit Effective Date* checkbox (schedule/backdate); regenerate a missing base PDF on download; edit documents in **Draft** and **Revision Requested** states (workflow `allow_edit`).

**Dependencies:** to *act* in a workflow the user must also be **assigned** on the document, and reviewers/QA approvers need a **signature image** on their linked Employee record — saving a document with a signature-less assignee is blocked.

---

## DMS Manager

**Purpose:** module owner. Everything QA Manager can do, plus administrative overrides.

**DocType permissions:** all of QA Manager **plus delete**.

**Workflow role in states:** documents sitting in *Under Review*, *Pending QA Approval*, *Approved* and *Revision Cancelled* are editable only by DMS Manager (`allow_edit`) — regular QA Managers cannot modify a document that has left their hands.

**Exclusive capabilities:** *Download Word (clean)* — the unwatermarked source file (controlled distribution of editables is a manager privilege); deleting drafts (never cancelled revisions — retention guard blocks everyone).

**Note:** DMS Manager does not appear in the workflow *transitions* — workflow actions still require QA Manager + assignment. A module owner who also authors documents should hold **both** roles.

---

## System Manager

Treated as unrestricted by the module (same as DMS Manager) and passes the effective-date and word-download role gates. Assign it for IT administration only, not as a substitute for the module roles.

---

## Employee (department members)

**Purpose:** consumers of controlled documentation.

**DocType permissions:** read, report, print only.

**Visibility (enforced by SQL filter + per-document check):** only documents that are **approved (submitted), active, and belong to the user's own department(s)** — plus any document that *names them* as preparer/reviewer/QA approver regardless of state. They never see drafts, pending (future-dated), obsolete or cancelled-revision records of other people.

**Downloads:** Controlled/Uncontrolled/Plain PDF of documents they can read. Never the clean Word file, and they can't trigger base-PDF regeneration.

---

## How the roles interact across the lifecycle

```
 Preparer (QA Manager)      Reviewer (QA Manager)      QA Approver (QA Manager)
        │ create + attach          │                          │
        │ Submit for Review ──────▶│ Approve as Reviewer ────▶│ Approve as QA
        │ ◀── Request Revision ────┘ ◀── Request Revision ────┘        │
        │                                                     auto-submit, render,
        │                                                     sign, stamp, activate
        ▼                                                              ▼
   (edits only in Draft /                                    Department Employees
    Revision Requested)                                      read + print the
                                                             Controlled Copy
   DMS Manager: can intervene in any state, deletes drafts,
   distributes clean Word files, owns admin overrides.
```

The same three-actor pattern repeats for every revision; the predecessor stays readable by its department until the moment the revision is approved (or activated, if future-dated), at which point members automatically see only the new version — obsolete documents drop out of their scope.

## Special considerations

- **Never rename the Role records.** A rename drags the workflow transitions with it while doctype permissions and code checks keep the canonical names — locking everyone out ("Not a valid Workflow Action"). Since v2.3.1, `bench migrate` self-heals the workflow rows back to canonical roles. For localized display, use **Translation** records (`Setup → Translation`) instead.
- **One user, several hats** is fine (e.g. reviewer = QA approver), but each hat must be assigned on the document, and the workflow's per-actor conditions keep a user from approving a step they're not assigned to.
- **Self-approval** is intentionally allowed at the Frappe level (`allow_self_approval`) because the preparer owns the draft they submit; separation of duties is enforced by the *assignments* (a document's reviewer/QA approver fields), not by record ownership.
- **Department membership** for Employee visibility comes from the Employee record's department; users without an Employee record (or department) see only documents naming them.
- The workflow `GMP Document Workflow` must remain **Active**; disabling it breaks every transition for every role.
