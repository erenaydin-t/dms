# User Guide — Step by Step

## 1. Create a new controlled document

1. **GMP Document → New.**
2. Fill both titles (FA + EN), Document Type, Department (must have an abbreviation configured), GMP Impact, Validity Period.
3. Pick the **Word Template** (tag-mapping profile) and assign the **Reviewer** and **QA Approver** — both must have a signature image on their Employee record (you are warned immediately if not).
4. Attach the source **.docx** containing the template tags (see *Templates*).
5. Save. The document gets its permanent ID (e.g. `QA-SOP-0001-1`); the file is renamed to that ID and its SHA-256 integrity hash is stored.
6. **Actions → Submit for Review** (only you, the preparer, can do this).
7. The Reviewer gets a ToDo → **Actions → Approve as Reviewer** (or *Request Revision* to bounce it back).
8. The QA Approver → **Actions → Approve as QA**. The document is submitted, rendered, signed, stamped and becomes the **effective version** (green *Controlled* badge).

## 2. Download PDFs

On any approved document → **Get PDF** menu:

- **Controlled Copy** — official distribution, status watermark.
- **Uncontrolled Copy** — `UNCONTROLLED COPY` watermark + Jalali print timestamp footer.
- **Plain** — no watermark, Jalali print timestamp footer only.

The live status always shows through: obsolete documents watermark `OBSOLETE`, future-dated ones `NOT YET EFFECTIVE` — on **every** variant.

## 3. Revise a document

1. Open the current effective version → **Create Revision** → enter the *Reason for Change*.
2. You are taken to the new draft (`…-2`). **The original stays valid and effective** — nothing happens to it yet.
3. Upload the revised .docx (the predecessor's file is never carried over), adjust fields, and run the same review workflow.
4. On QA approval, the new version becomes effective and the old one automatically turns **Obsolete**. References from other documents move to the new version automatically.

To abandon a revision at any point before approval: **Actions → Cancel Revision**. The draft is kept for audit (status *Revision Cancelled*) and the original remains in force. You can start a fresh revision immediately afterwards.

You cannot: revise an obsolete/pending version, run two revisions of the same document at once, or delete a cancelled revision.

## 4. Schedule or backdate an Effective Date

1. On the draft, tick **Edit Effective Date** (QA/DMS Manager only) and pick the date.
2. **Future date:** after QA approval the document shows an orange *Effective DD-MM-YYYY* badge and watermark `NOT YET EFFECTIVE`; the current version stays in force. On the scheduled date, the daily sweep swaps them automatically.
3. **Past date:** the document is effective immediately; expiry (+2/3/5 years) and next-revision (−1 month) derive from the backdate.
4. Untick the box to hand the date back to the system (approval date).

## 5. Retire a document without replacement

Cancel the approved document (standard Cancel). It turns Obsolete, keeps its record, and its PDFs watermark `OBSOLETE`. The daily expiry sweep does the same automatically once `expiry_date` passes.

## Common messages

| Message | Meaning |
|---|---|
| *Reason for Change is mandatory when revising…* | Every revision needs a documented reason |
| *A revision of X is already in progress: Y* | Finish or cancel the open revision first |
| *Only the current effective version can be revised* | You opened an obsolete/pending version — revise the active one |
| *…cannot be used until a signature is configured* | The assigned reviewer/QA approver has no Employee signature image |
| *'Edit Effective Date' is enabled but no Effective Date is set* | Enter the date or untick the option |
| *This document cannot be submitted directly* | Use the workflow Actions menu, not the Submit button |
| *Cancelled revisions are retained for audit…* | Deletion of cancelled revisions is blocked by design |
