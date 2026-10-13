# Roles & Permissions

The DMS module ships its own roles plus the standard *Employee* role for read-only consumers. Role names are canonical **English** names — do not rename the Role records (see *Special considerations*).

## Role summary

| Role | Mandatory? | Purpose |
|---|---|---|
| **DMS Initiator** | ✅ Yes — for authors | Prepares documents: creates drafts and revisions, uploads the controlled file, submits for approval |
| **DMS Approver** | ✅ Yes — for the chain | Supervisors, reviewers/managers, QA supervisors and regulatory managers: acts on the documents waiting on them |
| **QA Manager** | ✅ Yes — for final QA | The QA approver who publishes; sees every document |
| **DMS Manager** | ✅ Yes (at least one holder recommended) | Module owner/administrator: full control incl. delete, cross-department oversight, source-file distribution |
| **System Manager** | Built-in | IT administration; same unrestricted DMS access as DMS Manager |
| **DMS Proxy Approver** | Optional | Delegated approval: acts for any role-holder, on any document, when an approver is unavailable |
| **Employee** | Optional | Read-only consumers: see the *approved, active* documents of their own department |

Give each person the **narrowest role that covers what they do**. QA Manager, DMS Manager, System Manager and DMS Proxy Approver are *unrestricted* — they see every document in every department and state — so handing QA Manager to every author and approver (what this page recommended before v2.7.2) also removes department scoping for all of them. Before v2.7.2 that was in fact the only setup that worked: a user holding only DMS Initiator could not create a document, and a DMS Approver could not take a transition.

`Administrator` implicitly passes every role and per-actor gate (escape hatch) — fine for emergencies, wrong for daily use, since audit trails should name real actors. **DMS Proxy Approver exists to make that escape hatch unnecessary**: it grants the same reach through a named account, and every action it takes is recorded on the document as "acted on behalf of".

---

## DMS Initiator

**Purpose:** authors.

**DocType permissions:** read, write, create, print, email, report, share — no submit, no delete.

**Workflow actions:** *Submit for Approval* (from Draft or Revision Requested) and *Cancel Revision* on their own draft revisions — only as the document's **preparer**.

**Other capabilities:** *Create Revision* on an approved, active document (the creator becomes the revision's preparer, so the button is offered only to roles that can submit a draft); edit their drafts in **Draft** and **Revision Requested** (workflow `allow_edit`).

**Visibility:** documents that name them (as preparer or any other actor), plus the approved, active documents of their department.

---

## DMS Approver

**Purpose:** every actor of the chain between the author and final QA — supervisor, reviewer/manager, QA supervisor, regulatory manager — and the delegated QA reviewers.

**DocType permissions:** read, write, print, email, report.

**Workflow actions:** the approve / return actions of the stage the document is waiting on, and only when they are **the person resolved onto it** (see *Workflow*). Each *Return* asks for a reason.

**Visibility:** documents that name them, documents whose QA review queue includes them, plus the approved, active documents of their department. Writing is limited to the stage that waits on them.

---

## QA Manager

**Purpose:** final QA authorization.

**DocType permissions:** read, write, create, submit, cancel, amend, report, export, print, email, share — **no delete**.

**Visibility:** unrestricted — sees all documents in every department and state.

**Workflow actions:** *Publish* and *Return to Manager* at **Pending Final QA Approval**, as the assigned **QA Approver**.

**Other capabilities:** *Edit Effective Date* (schedule/backdate) — the date can be changed only by QA Manager, DMS Manager or System Manager, though the approvers who act after them are not blocked by it; regenerate a missing base PDF on download.

**Dependencies:** to *act* in a workflow the user must also be **assigned** on the document, and reviewers/QA approvers need a **signature image** on their linked Employee record — saving a document with a signature-less assignee is blocked. A QA Manager who also authors needs **DMS Initiator** too.

---

## DMS Manager

**Purpose:** module owner. Everything QA Manager can do, plus administrative overrides.

**DocType permissions:** all of QA Manager **plus delete**.

**Workflow role in states:** documents in every state between submission and approval, *Approved* and *Revision Cancelled* are editable only by DMS Manager (`allow_edit`) — nobody else can modify a document that has left the author's hands.

**Exclusive capabilities:** *Download Word (clean)* — the unwatermarked source file (controlled distribution of editables is a manager privilege); deleting drafts (never cancelled revisions — retention guard blocks everyone).

**Note:** DMS Manager does not appear in the workflow *transitions* — workflow actions still require the stage's role + assignment. A module owner who also authors documents should also hold **DMS Initiator**.

---

## System Manager

Treated as unrestricted by the module (same as DMS Manager) and passes the effective-date and word-download role gates. Assign it for IT administration only, not as a substitute for the module roles.

---

## DMS Proxy Approver

**Purpose:** keep documents moving when the assigned approver is unavailable — on leave, travelling, or no longer with the company — without anyone sharing the `Administrator` password.

**What it can do:** every stage's workflow action, on every document, in every department. Each actor-gated transition ships a twin allowed to this role, so **the single role grant is enough on its own** — the holder needs none of QA Manager, DMS Initiator or DMS Approver. The one action it cannot take is *Cancel Revision*: abandoning a draft revision stays with the author and QA.

**What it records.** A delegated action is never silent, and never disguised as the role-holder's own:

| Field | Holds |
|---|---|
| `<stage>_approved_by` | the account that **actually acted** — the proxy |
| `<stage>_on_behalf_of` | the role-holder it was performed **for** |
| `<stage>_date` | when |

The form shows an orange **Delegated approval** banner ("X on behalf of Y — date") and a *Delegated Approvals* section. The **signature** that prints is the represented approver's, so the signature block reads as the assigned approver's — with `{{ <stage>_on_behalf_of_name }}` available in Word templates to state the delegation beside it.

Why both sides: 21 CFR Part 11 §11.200 requires an electronic signature to be unique to one individual and never reassigned, so the record of who actually pressed the button must survive. Recording the delegation alongside it — rather than instead of it — is what makes the signature block readable *and* the audit trail truthful.

**Grant it sparingly.** It is approval authority over every controlled document in the company. Treat it like a key to the QA cabinet: few holders, and review who has it.

> Administrator's escape-hatch approvals are recorded the same way from v2.6.0 — if Administrator approves for a supervisor, the document now says so.

---

## Employee (department members)

**Purpose:** consumers of controlled documentation.

**DocType permissions:** read, report, print only.

**Visibility (enforced by SQL filter + per-document check):** only documents that are **approved (submitted), active, and belong to the user's own department(s)** — plus any document that *names them* as preparer/reviewer/QA approver regardless of state. They never see drafts, pending (future-dated), obsolete or cancelled-revision records of other people.

**Downloads:** Controlled/Uncontrolled/Plain PDF of documents they can read. Never the clean Word file, and they can't trigger base-PDF regeneration.

---

## How the roles interact across the lifecycle

```
 Preparer        Supervisor      Reviewer        QA Supervisor     Regulatory      Manager         QA Approver
 (Initiator)     (Approver)      (Approver)      (Approver)        (Approver)      (= Reviewer)    (QA Manager)
   │ create,         │               │               │ approve, or      │               │               │
   │ attach,         │               │               │ delegate to a    │               │               │
   │ submit ────────▶│ approve ─────▶│ approve ─────▶│ QA review queue ▶│ validate ────▶│ approve ─────▶│ Publish:
   │                 │               │               │                  │               │               │ render, sign,
   │ ◀────────────── every stage can Return one step (or to the preparer), with a reason ◀───────────── │ stamp, activate
   ▼                                                                                                    ▼
 (edits only in Draft /                                                                   Department Employees
  Revision Requested)                                                                     read + print the
                                                                                          Controlled Copy
 DMS Manager: can intervene in any state, deletes drafts, distributes clean Word files, owns admin overrides.
```

The same chain repeats for every revision; the predecessor stays readable by its department until the moment the revision is approved (or activated, if future-dated), at which point members automatically see only the new version — obsolete documents drop out of their scope.

## Special considerations

- **Never rename the Role records.** A rename drags the workflow transitions with it while doctype permissions and code checks keep the canonical names — locking everyone out ("Not a valid Workflow Action"). Since v2.6.0 migrate no longer repairs the workflow; run `bench --site <site> execute dms.install.restore_workflow_defaults` to put the shipped roles back (see *Administration*). For localized display, use **Translation** records (`Setup → Translation`) instead.
- **One user, several hats** is fine (e.g. reviewer = QA approver), but each hat must be assigned on the document, and the workflow's per-actor conditions keep a user from approving a step they're not assigned to.
- **Self-approval** is intentionally allowed at the Frappe level (`allow_self_approval`) because the preparer owns the draft they submit; separation of duties is enforced by the *assignments* (a document's reviewer/QA approver fields), not by record ownership.
- **Department membership** for Employee visibility comes from the user's **Active** Employee record's department (a Left record from before a transfer or rehire no longer counts); users without one see only documents naming them.
- The workflow `GMP Document Workflow` must remain **Active**; disabling it breaks every transition for every role.
