# Effective Dates & Scheduling

`effective_date` follows the familiar ERPNext **posting date** pattern.

## Default: system-controlled

Leave **Edit Effective Date** unchecked (the default) and the system stamps the **QA-approval date** at submit. Any manually typed date is silently normalized away on save — exactly like ERPNext overwrites the posting date when *Edit Posting Date and Time* is off. Derived dates follow automatically:

- `expiry_date` = effective date + validity period (2/3/5 years)
- `next_revision_date` = expiry − 1 month

## Manual mode: backdating and future scheduling

Tick **Edit Effective Date** (visible to QA Manager / DMS Manager / System Manager — enforced server-side) and enter any date **before approval**:

- **Past date (backdating)** — the document is effective immediately on approval, and expiry/next-revision derive from the backdate.
- **Future date (scheduling)** — the document is approved but **pending**:

| While pending | |
|---|---|
| `docstatus` | 1 (submitted) |
| `workflow_status` | Approved |
| `is_active` | **0** — *not* the effective version |
| Watermark on every PDF variant | **NOT YET EFFECTIVE** |
| Predecessor (revision flow) | **remains Approved, active and effective** |
| List view badge | orange *“Effective DD-MM-YYYY”* |

If manual mode is on but no date is entered, QA approval is blocked with a clear message.

## Automatic activation

The daily scheduler `activate_effective_documents` (runs **before** the expiry sweep) promotes every pending document whose date has arrived:

1. `is_active` → 1 — it becomes the effective version,
2. the superseded predecessor transitions to **Obsolete**,
3. dependents' references are repointed,
4. an audit comment is written on both records.

Between midnight and the sweep, a due document still reads *NOT YET EFFECTIVE* — never falsely *Obsolete* or *Controlled*. Activation is the scheduler's job alone, which keeps the hand-over atomic and auditable.

## Expiry

The daily `expire_gmp_documents` sweep obsoletes every active document past its `expiry_date` (docstatus stays 1; downloads then watermark as OBSOLETE). Schedule a replacement revision before `next_revision_date` to avoid a coverage gap.
