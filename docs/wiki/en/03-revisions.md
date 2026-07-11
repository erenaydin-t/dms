# The Revision Model (non-destructive)

Since v2.3.0, revising a controlled document **never invalidates the current approved version up front**. The old Cancel + Amend mechanism is not used.

## How it works

```
QA-SOP-0001-1  (Approved, active)          ← stays the effective version …
      │  Create Revision
      ▼
QA-SOP-0001-2  (Draft → Under Review → Pending QA)   ← separate record, revision_of = …-1
      │  Approve as QA
      ▼
QA-SOP-0001-2  (Approved, active)          ← now the effective version
QA-SOP-0001-1  (Obsolete, is_active = 0)   ← retired automatically, kept for audit
```

1. On an **Approved + active** document, click **Create Revision** (QA/DMS Manager). Enter the mandatory *Reason for Change*.
2. A new **draft record** is created with the next version segment, linked back via `revision_of`. It starts with **no attachment** — upload the revised source file. The predecessor's controlled file is never reused or replaced.
3. The draft goes through the full review workflow. All along, the predecessor remains **Approved, active and effective** — its Controlled Copy PDF still reads `CONTROLLED COPY`.
4. On final QA approval:
   - the revision becomes the effective version and the official version number advances (`version_number` = predecessor + 1),
   - the predecessor automatically transitions to **Obsolete** (`is_active = 0`, still submitted/docstatus 1 — an immutable audit record),
   - other documents' references to the predecessor are **repointed** to the new version automatically.

## Cancelling a revision

At any pre-approval stage, use the workflow action **Cancel Revision**:

- the draft moves to the terminal **Revision Cancelled** state and is **retained in the database** (deletion is blocked for audit traceability),
- the predecessor is completely untouched — still Approved, still effective, version number unchanged,
- a new revision can then be started; it takes the **next** name segment (a cancelled `…-2` never collides with the following attempt `…-3`).

## Guards (enforced server-side)

- Only the **current effective version** (Approved + active + submitted) can be revised.
- **One open revision at a time** per document — a second `Create Revision` is rejected while a draft revision is in progress (cancelled revisions don't count).
- A predecessor that was already superseded can never grow a second, parallel successor.
- `Reason for Change` is mandatory for every revision.

## Worked example (from the acceptance test run)

| Step | Record | State after step |
|---|---|---|
| Create + approve | `QA-SOP-0001-1` v1 | Approved, active |
| Create Revision + approve | `QA-SOP-0001-2` v2 | Approved, active; `…-1` Obsolete |
| Create Revision, then **Cancel Revision** | `QA-SOP-0001-3` | Revision Cancelled (retained); `…-2` still active |
| Create Revision again + approve | `QA-SOP-0001-4` v3 | Approved, active; `…-2` Obsolete |

Note the name segment (`-4`) and the version number (`v3`) diverging: names count every attempt, the official version counts only approvals.
