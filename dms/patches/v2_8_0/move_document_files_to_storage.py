# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""2.8.0: GMP Documents keep their files in the DMS storage.

Until 2.7.x a document's source template, rendered deliverable, signed base PDF
and signature snapshots were File rows attached to it — so everyone who could
read the document could download all of them from the attachments, around the
watermark and the managers-only Word download. They now live in the DMS storage
(dms.dms.storage), served only by the DMS endpoints.

This copies each document's files there, repoints its fields and removes the
old File rows; the old bytes are deleted after each document's commit. Files go
to the local disk (DMS Settings › Store DMS Files on S3 is off after the
upgrade); Move Stored Files takes them to S3 later.

One document at a time, each in its own transaction: a document that fails is
logged and left as it was, and is adopted again the next time one of its files
is needed (see _adopt_legacy_files)."""

import frappe

from dms.dms.doctype.gmp_document.gmp_document import _adopt_legacy_files


def execute():
    failed = []
    for name in frappe.get_all("GMP Document", pluck="name", order_by="creation asc"):
        try:
            _adopt_legacy_files(frappe.get_doc("GMP Document", name))
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            failed.append(name)
            frappe.log_error(frappe.get_traceback(), f"DMS 2.8.0: files of {name} were not moved")
    if failed:
        print(f"DMS 2.8.0: files of {len(failed)} GMP Documents were not moved; see the Error Log.")
