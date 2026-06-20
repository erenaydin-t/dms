# Permissions & Access Control Guide

How visibility and access work in the GMP DMS module, and how to configure it
from the desk panel. Applies to **v1.2.0+**.

---

## 1. The model at a glance

| Who | Role | Can see | Can do |
| --- | --- | --- | --- |
| **Department member** (consumer) | `Employee` | The **approved, active** controlled copies of **their own department(s)** only — plus any document on which they are personally named (preparer / reviewer / QA approver). | **Read-only.** View the document and **download the watermarked "Controlled Copy" PDF**. No edit / create / cancel. |
| **Workflow operators** (preparers, reviewers, QA approvers) | `QA Manager` | **Every** document, any department, any state. | Create, edit, run the review/approval workflow, cancel, amend, download PDF **and** the clean Word file. |
| **Module owner / administrator** | `DMS Manager` | **Every** document, any department, any state. | Full CRUD + cancel + amend + delete across all documents regardless of creator or department. |
| **Super admin** | `System Manager` | Everything. | Everything (Frappe-level). |

> **Why a member sees "their department" only:** a member's department is read
> from their **Employee** record (`Employee.user_id` → `Employee.department`).
> A user with no linked Employee record sees nothing except documents that name
> them.

Enforcement lives in two cooperating hooks on `GMP Document`
(`dms/hooks.py` → `dms.dms.doctype.gmp_document.gmp_document`):

- `get_permission_query_conditions` — filters **lists, reports, search and the
  Tree view**.
- `has_permission` — gates **opening a single document** and the
  **PDF/Word download** endpoints.

The **GMP Document Tree** page applies the same department scope, so a member
only sees their own department branches and only active, approved leaves.

---

## 2. Configuring it from the panel

### 2.1 Make someone a read-only department member

1. **Create / open the User** (`Users` list). Assign them the **`Employee`**
   role (Role Profile or the User's *Roles* tab). Do **not** give them
   `QA Manager` / `DMS Manager` / `System Manager` if they should stay
   read-only.
2. **Link them to an Employee** record: open the **Employee** (`HR > Employee`),
   set **`User ID`** to their user, and set **`Department`** to the department
   whose documents they should see.
   - A user can be linked to more than one Employee/department; they then see
     all of those departments.
3. Done. They can now open the **GMP Document Tree** (or list), browse their
   department's approved/active documents, open them, and click
   **Get PDF → Download PDF (Controlled Copy)**.

### 2.2 Make someone a module owner / administrator

1. Open the **User**.
2. Assign the **`DMS Manager`** role (auto-created on install/migrate).
3. They now have full create / edit / cancel / delete / amend access to every
   document in every department, and can download both the PDF and the clean
   Word file.

> **Editing across the lifecycle.** Frappe Workflow allows exactly one editor
> role per state. The DMS Manager owns editing of the in-pipeline and
> submitted states — **Under Review**, **Pending QA Approval**, **Approved** —
> so an owner can correct or override a document there. The **Draft** and
> **Revision Requested** states are reserved for the author (`QA Manager`). A
> module owner who also authors or corrects drafts should therefore hold
> **both** `DMS Manager` and `QA Manager`.
>
> **Amending a cancelled document (creating a new version).** When an approved
> document is cancelled it enters the **Obsolete** state, and Frappe hides the
> **Amend** action whenever the current workflow state makes the form read-only
> for the user. The Obsolete state is therefore editable by **`QA Manager`**, so
> the preparer/approver who cancels a document can immediately revise it into a
> new version. A user who needs to amend a cancelled document must hold the
> `QA Manager` role (Administrator and module owners who also hold `QA Manager`
> are unaffected).
>
> **To also drive the review/approval workflow** (the *Actions* menu:
> Submit for Review → Approve as Reviewer → Approve as QA), the user must hold
> the **`QA Manager`** role and be the assigned `reviewer` / `qa_approver` on
> the document. The workflow steps are deliberately actor-specific for GMP
> segregation of duties.

### 2.3 Department abbreviation (unrelated to access, but required)

Each **Department** still needs its `Abbreviation` (`custom_abbr`) set for
document naming — see the README. This is separate from visibility.

---

## 3. End-to-end workflow

```
                    ┌─────────────────────────────────────────────┐
                    │ DMS Manager / System Manager (module owner)  │
                    │  • sees & administers ALL departments        │
                    │  • create / edit / cancel / delete / amend   │
                    └─────────────────────────────────────────────┘

   author / review / approve (per-actor, QA Manager role)
   ┌──────────┐  Submit for   ┌──────────────┐  Approve as  ┌────────────────────┐
   │  Draft   │ ───review───► │ Under Review │ ──reviewer─► │ Pending QA Approval │
   └──────────┘               └──────────────┘             └────────────────────┘
                                                                      │ Approve as QA
                                                                      ▼
                                                            ┌────────────────────┐
                                                            │ Approved (live)    │
                                                            │ docstatus=1         │
                                                            │ is_active=1         │
                                                            └────────────────────┘
                                                                      │
            department members (Employee role) can now ◄─────────────┘
            see it in the Tree and download the Controlled Copy PDF
```

A member's view is strictly the bottom box: **Approved + Active**. Drafts,
in-review documents and obsolete/superseded versions are hidden from them.

---

## 4. Verifying the configuration

Quick checks an admin can run:

- **Member sees only their department:** log in as the member (or use
  *Switch User*), open the **GMP Document Tree** — only their department's
  branches should appear, each leaf green/ACTIVE.
- **Member is read-only:** opening an approved document shows no *Save*,
  *Submit*, or workflow *Actions* — only **Get PDF → Download PDF (Controlled
  Copy)**.
- **Member cannot reach other departments:** pasting another department's
  document URL returns *No permission*.
- **Module owner sees all:** log in as a `DMS Manager` — the list/tree shows
  every department and state, and the form allows edit/cancel/amend.

---

## 5. Tuning the policy (for developers)

All knobs are in `dms/dms/doctype/gmp_document/gmp_document.py`:

- **`UNRESTRICTED_ROLES`** — the set of roles that bypass department scoping.
  Add/remove a role here to change who is a "full access" user.
- **`get_permission_query_conditions(user)`** — the list/report/tree filter.
  The member clause is
  `department IN (their depts) AND is_active = 1 AND docstatus = 1`,
  OR the user is named as `prepared_by` / `reviewer` / `qa_approver`.
  - To let members also see **obsolete** approved versions, drop the
    `is_active = 1` term.
- **`has_permission(doc, ptype, user)`** — the single-document gate; mirrors
  the query conditions and additionally allows `print`.
- **Department membership** is resolved by **`_user_departments(user)`**
  (Employee records linked to the user). Change this to use the Department
  *tree* (parent + descendants) if you want managers of a parent department to
  see child departments.

After changing roles or permission rows, run:

```bash
bench --site <site> migrate
bench restart
```
