# PDF Output, Watermarks & Copy Types

On QA approval, the source file is rendered into a signed **base PDF** (signatures + QA status stamp baked in). Watermarks are applied **dynamically at download time**, so a status change (obsolescence, activation) is reflected immediately without re-rendering.

## Download actions (Get PDF menu, approved documents)

| Action | Watermark | Footer | Intended use |
|---|---|---|---|
| **Download PDF (Controlled Copy)** | status-driven (below) | — | official controlled distribution |
| **Download PDF (Uncontrolled Copy)** | `UNCONTROLLED COPY` on every page | Jalali print timestamp + *“not subject to change control”* | reference prints, external sharing |
| **Download PDF (Plain)** | none | Jalali print timestamp | clean working copy |
| **Download Word (clean)** | — | — | managers only: source-format distribution |

The Uncontrolled and Plain variants reuse **all existing pages** — no extra cover page. The footer carries the **Persian (Jalali) date and time of generation**, e.g. `Uncontrolled copy - printed 1405/04/18 14:30:45 (Jalali) - not subject to change control`.

## Status-driven watermarks

The document's live status always wins — **no variant can hide it**:

| Document status | Watermark (all variants) |
|---|---|
| Approved + active (the effective version) | `CONTROLLED COPY` (controlled variant; uncontrolled/plain per table above) |
| Approved + future-dated (pending) | `NOT YET EFFECTIVE` |
| Obsolete / superseded / expired / cancelled | `OBSOLETE` |
| Draft (any pre-approval state) | download blocked — no PDF exists before QA approval |

## QA status stamp

The base PDF embeds a stamp image next to the `{{ qa_stamp }}` tag: **approved** while the document is active or pending, **rejected** once it is obsolete (re-rendered on retirement, guarded so a temporary LibreOffice outage can never block the lifecycle).

## Integrity

- The controlled source file is renamed to the document ID and its **SHA-256 hash** is stored on the record (`file_integrity_hash`).
- Base-PDF regeneration (if the file disappears from disk) is manager-only and automatic on the next controlled download by a manager.
