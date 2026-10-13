# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Where DMS keeps the files it owns.

Every GMP Document owns a few files: the source template the preparer
uploaded, the clean deliverable and the signed base PDF rendered at approval,
and the signature images frozen when a stage is completed. They are stored
here — on the local disk, or in the bucket of the frappe_s3_attachment app —
and recorded in **DMS Stored File** with their SHA-256.

They are deliberately not File rows. Frappe lets everyone who can read a
document download every File attached to it (/private/files/... checks nothing
else), which gave department members the unwatermarked PDF and the clean Word
file. Stored files are served only by the DMS endpoints, under DMS's own rules
(gmp_document.document_file, download_watermarked_pdf, download_word_document).

What this module guarantees:

* **Integrity.** Every read is checked against the recorded SHA-256. A file
  changed behind DMS's back — on the disk or in the bucket — is refused, never
  rendered or served.
* **Writes follow the transaction.** A new version goes to a new location and
  the registry row is pointed at it. The old version is removed only after the
  commit; the new one is removed if the transaction rolls back. A failed
  approval or re-render never leaves a row pointing at bytes it did not record.
* **Reads follow the file.** A row records where its file is, so changing the
  storage in DMS Settings never strands a file: new files go to the selected
  storage, existing ones are read where they are until move_stored_files()
  moves them.

Rendering needs real files on disk (LibreOffice, docxtpl, reportlab), so
local_path() hands out a path: the stored file itself for local storage, a
verified copy in a temporary cache for S3.
"""

import hashlib
import os
import tempfile

from urllib.parse import parse_qs, quote, urlsplit

import frappe
from frappe import _
from frappe.utils import cint, get_files_path

REGISTRY = "DMS Stored File"
LOCAL = "Local"
S3 = "S3"

# The endpoint that serves a stored file; its URL is what the document's
# Attach / Attach Image fields hold.
FILE_ENDPOINT = "/api/method/dms.dms.doctype.gmp_document.gmp_document.document_file"

# S3 downloads are cached on the local disk for rendering and watermarking.
# Entries are named by SHA-256, so a cached copy can never be stale.
CACHE_LIMIT_BYTES = 512 * 1024 * 1024


class StoredFileMissing(frappe.DoesNotExistError):
    """No stored file by that name, or its bytes are gone."""


class StoredFileIntegrityError(frappe.ValidationError):
    """The stored bytes do not match the recorded SHA-256."""


# ---------------------------------------------------------------------- #
#  URLs                                                                  #
# ---------------------------------------------------------------------- #


def file_url(docname, file_name):
    return f"{FILE_ENDPOINT}?docname={quote(docname)}&file={quote(file_name)}"


def parse_file_url(url):
    """``(docname, file_name)`` of a stored-file URL, or None for any other
    URL (an upload, a File row, an external link)."""
    if not url or not url.startswith(FILE_ENDPOINT + "?"):
        return None
    query = parse_qs(urlsplit(url).query)
    docname = (query.get("docname") or [""])[0]
    file_name = (query.get("file") or [""])[0]
    if not (docname and file_name):
        return None
    return docname, file_name


# ---------------------------------------------------------------------- #
#  Storage selection                                                     #
# ---------------------------------------------------------------------- #


def s3_module():
    """The frappe_s3_attachment storage API, or None when the app (or a
    version with the API) is not installed on this site."""
    if "frappe_s3_attachment" not in frappe.get_installed_apps():
        return None
    try:
        from frappe_s3_attachment import storage as s3
    except ImportError:
        return None
    return s3


def s3_unavailable_reason():
    """Why S3 cannot be used right now, or None when it can."""
    s3 = s3_module()
    if not s3:
        return _(
            "The frappe_s3_attachment app (version 16.3.0 or later) is not installed on this site."
        )
    if not s3.is_configured():
        return _("No bucket is configured in S3 File Attachment.")
    return None


def target_storage():
    """Where new files go: the storage selected in DMS Settings."""
    if not cint(frappe.db.get_single_value("DMS Settings", "store_files_on_s3")):
        return LOCAL
    reason = s3_unavailable_reason()
    if reason:
        frappe.throw(
            _("DMS Settings stores files on S3, but S3 cannot be used: {0}").format(reason),
            title=_("File Storage Unavailable"),
        )
    return S3


# ---------------------------------------------------------------------- #
#  Registry rows                                                         #
# ---------------------------------------------------------------------- #


def get_row(docname, file_name):
    return frappe.db.get_value(
        REGISTRY,
        {"document": docname, "file_name": file_name},
        ["name", "document", "file_name", "storage", "storage_key", "sha256", "file_size"],
        as_dict=True,
    )


def exists(docname, file_name):
    return bool(get_row(docname, file_name))


def _require_row(docname, file_name):
    row = get_row(docname, file_name)
    if not row:
        raise StoredFileMissing(_("{0} of {1} is not stored.").format(file_name, docname))
    return row


# ---------------------------------------------------------------------- #
#  Writing                                                               #
# ---------------------------------------------------------------------- #


def put(docname, file_name, content):
    """Store ``content`` as ``file_name`` of ``docname``, replacing any earlier
    version, and return the URL the document's fields hold."""
    sha = _sha256(content)
    row = get_row(docname, file_name)
    if row and row.sha256 == sha and _object_present(row):
        return file_url(docname, file_name)  # these bytes are already stored

    storage = target_storage()
    key = _new_key(storage, docname, file_name, sha)
    _write(storage, key, content, docname, file_name, sha)

    previous = (row.storage, row.storage_key) if row else None
    if previous != (storage, key):
        # Undone if the transaction is: the row keeps pointing at the old file.
        frappe.db.after_rollback.add(lambda: _remove_quietly(storage, key))

    values = {"storage": storage, "storage_key": key, "sha256": sha, "file_size": len(content)}
    if row:
        frappe.db.set_value(REGISTRY, row.name, values)
        if previous != (storage, key):
            frappe.db.after_commit.add(lambda: _remove_quietly(*previous))
    else:
        frappe.get_doc(
            {"doctype": REGISTRY, "document": docname, "file_name": file_name, **values}
        ).insert(ignore_permissions=True)
    return file_url(docname, file_name)


def delete(docname, file_name):
    row = get_row(docname, file_name)
    if row:
        _delete_row(row)


def delete_document_files(docname):
    for row in frappe.get_all(
        REGISTRY,
        filters={"document": docname},
        fields=["name", "storage", "storage_key"],
    ):
        _delete_row(row)


def _delete_row(row):
    frappe.db.delete(REGISTRY, {"name": row.name})
    frappe.db.after_commit.add(lambda: _remove_quietly(row.storage, row.storage_key))


# ---------------------------------------------------------------------- #
#  Reading                                                               #
# ---------------------------------------------------------------------- #


def read(docname, file_name):
    """The verified bytes of a stored file."""
    row = _require_row(docname, file_name)
    content = _read_object(row)
    _verify(row, content)
    return content


def local_path(docname, file_name):
    """A path on the local disk holding the verified bytes of a stored file —
    for LibreOffice, docxtpl and reportlab, which need a file. Callers must
    only read it: for local storage it is the stored file itself."""
    row = _require_row(docname, file_name)
    if row.storage == LOCAL:
        path = _local_path(row.storage_key)
        if not os.path.exists(path):
            raise StoredFileMissing(
                _("{0} of {1} is missing from the disk.").format(file_name, docname)
            )
        with open(path, "rb") as fh:
            _verify(row, fh.read())
        return path

    cached = _cache_path(row.sha256, os.path.splitext(file_name)[1])
    if os.path.exists(cached):
        with open(cached, "rb") as fh:
            if _sha256(fh.read()) == row.sha256:
                os.utime(cached)  # recently used: evicted last
                return cached
        os.remove(cached)  # damaged copy; fetch again

    content = _read_object(row)
    _verify(row, content)
    _write_atomically(cached, content)
    _evict_cache()
    return cached


def _verify(row, content):
    if _sha256(content) == row.sha256:
        return
    frappe.log_error(
        title="DMS: stored file failed its integrity check",
        message=(
            f"{row.file_name} of {row.document} ({row.storage}: {row.storage_key}) "
            f"does not match its recorded SHA-256 {row.sha256}. It was changed "
            f"outside DMS and has not been used."
        ),
    )
    frappe.throw(
        _(
            "{0} of {1} does not match its recorded checksum: it was changed outside "
            "the DMS and will not be used. Ask a DMS Manager to check the Error Log."
        ).format(row.file_name, row.document),
        StoredFileIntegrityError,
        title=_("File Integrity Check Failed"),
    )


# ---------------------------------------------------------------------- #
#  Ordinary File rows (uploads, Employee signatures, the watermark font)  #
# ---------------------------------------------------------------------- #


def is_s3_file(file_doc):
    s3 = s3_module()
    return bool(s3 and s3.is_s3_file(file_doc))


def read_frappe_file(file_doc):
    """The bytes of a File row, whether frappe_s3_attachment moved it to the
    bucket or it is on the local disk. Raises FileNotFoundError for a local
    file that is missing."""
    if is_s3_file(file_doc):
        return s3_module().read_file(file_doc)
    path = file_doc.get_full_path()
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "rb") as fh:
        return fh.read()


def frappe_file_path(file_doc):
    """A local path holding a File row's bytes: the file itself, or a cached
    copy of a file in the bucket (object keys there are never reused)."""
    if not is_s3_file(file_doc):
        path = file_doc.get_full_path()
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        return path

    key = s3_module().get_s3_key(file_doc)
    ext = os.path.splitext(file_doc.file_name or key)[1]
    cached = _cache_path("file-" + hashlib.sha256(key.encode()).hexdigest(), ext)
    if not os.path.exists(cached):
        _write_atomically(cached, s3_module().read_file(file_doc))
        _evict_cache()
    else:
        os.utime(cached)
    return cached


# ---------------------------------------------------------------------- #
#  Moving between storages                                               #
# ---------------------------------------------------------------------- #


def storage_summary():
    """``{storage: {"files": n, "bytes": n}}`` over every stored file."""
    from frappe.query_builder.functions import Count, Sum

    table = frappe.qb.DocType(REGISTRY)
    rows = (
        frappe.qb.from_(table)
        .select(table.storage, Count(table.name).as_("files"), Sum(table.file_size).as_("bytes"))
        .groupby(table.storage)
        .run(as_dict=True)
    )
    return {row.storage: {"files": cint(row.files), "bytes": cint(row.bytes)} for row in rows}


def move_stored_files(target, document=None):
    """Move every stored file that is not in ``target`` there (only those of
    ``document`` when given).

    One file at a time, each committed on its own: the copy is written, read
    back and checked against the recorded SHA-256 before the row is switched,
    and the old copy is removed only after that commit. A file that fails is
    left where it was and logged; re-running picks it up again.

    Returns ``{"moved": n, "failed": n}``."""
    if target not in (LOCAL, S3):
        frappe.throw(_("Unknown storage {0}.").format(target))
    if target == S3:
        reason = s3_unavailable_reason()
        if reason:
            frappe.throw(reason)

    filters = {"storage": ["!=", target]}
    if document:
        filters["document"] = document
    moved = failed = 0
    for name in frappe.get_all(REGISTRY, filters=filters, pluck="name"):
        row = frappe.db.get_value(
            REGISTRY,
            name,
            ["name", "document", "file_name", "storage", "storage_key", "sha256"],
            as_dict=True,
        )
        key = None
        try:
            content = _read_object(row)
            _verify(row, content)
            key = _new_key(target, row.document, row.file_name, row.sha256)
            _write(target, key, content, row.document, row.file_name, row.sha256)
            _verify(row, _read_object(frappe._dict(row, storage=target, storage_key=key)))
            frappe.db.set_value(REGISTRY, row.name, {"storage": target, "storage_key": key})
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            if key:
                _remove_quietly(target, key)
            frappe.log_error(
                frappe.get_traceback(),
                f"DMS: moving {row.file_name} of {row.document} to {target} failed",
            )
            failed += 1
            continue
        _remove_quietly(row.storage, row.storage_key)
        moved += 1
    return {"moved": moved, "failed": failed}


# ---------------------------------------------------------------------- #
#  Backends                                                              #
# ---------------------------------------------------------------------- #


def _sha256(content):
    return hashlib.sha256(content).hexdigest()


def _safe(part):
    """A key segment: letters, digits, dot, dash and underscore only."""
    return "".join(c if c.isascii() and (c.isalnum() or c in "._-") else "_" for c in part)


def _new_key(storage, docname, file_name, sha):
    # Versioned by content, so a new version never overwrites the bytes the
    # registry still points at until the transaction commits.
    parts = ("dms", _safe(docname), f"{sha[:16]}-{_safe(file_name)}")
    if storage == S3:
        return s3_module().object_key(*parts)
    return "/".join(parts)


def _local_path(key):
    return get_files_path(*key.split("/"), is_private=1)


def _write(storage, key, content, docname, file_name, sha):
    if storage == S3:
        s3_module().put_object(
            key, content, metadata={"sha256": sha, "document": docname, "file-name": file_name}
        )
    else:
        _write_atomically(_local_path(key), content)


def _read_object(row):
    if row.storage == S3:
        s3 = s3_module()
        if not s3:
            frappe.throw(
                _("{0} of {1} is stored on S3, but the frappe_s3_attachment app is not installed.").format(
                    row.file_name, row.document
                )
            )
        if not s3.object_exists(row.storage_key):
            raise StoredFileMissing(
                _("{0} of {1} is missing from the bucket.").format(row.file_name, row.document)
            )
        return s3.get_object(row.storage_key)

    path = _local_path(row.storage_key)
    if not os.path.exists(path):
        raise StoredFileMissing(
            _("{0} of {1} is missing from the disk.").format(row.file_name, row.document)
        )
    with open(path, "rb") as fh:
        return fh.read()


def _object_present(row):
    if row.storage == S3:
        s3 = s3_module()
        return bool(s3 and s3.object_exists(row.storage_key))
    return os.path.exists(_local_path(row.storage_key))


def _remove_quietly(storage, key):
    """Remove an object no row points at. Never raises: it runs in commit and
    rollback callbacks, where an error would hide the real outcome."""
    try:
        if frappe.db.exists(REGISTRY, {"storage": storage, "storage_key": key}):
            return  # still in use (the same bytes were stored again)
        if storage == S3:
            s3 = s3_module()
            if s3:
                s3.delete_object(key)
        else:
            path = _local_path(key)
            if os.path.exists(path):
                os.remove(path)
            try:
                os.rmdir(os.path.dirname(path))  # the document's last file
            except OSError:
                pass
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"DMS: could not remove {storage} file {key}")


def _write_atomically(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _cache_dir():
    return os.path.join(tempfile.gettempdir(), "dms-cache", _safe(frappe.local.site))


def _cache_path(name, ext):
    return os.path.join(_cache_dir(), f"{name}{ext}")


def _evict_cache():
    """Keep the cache under CACHE_LIMIT_BYTES, dropping least recently used."""
    directory = _cache_dir()
    try:
        entries = [
            (entry.stat().st_mtime, entry.stat().st_size, entry.path)
            for entry in os.scandir(directory)
            if entry.is_file() and not entry.name.startswith(".tmp-")
        ]
    except FileNotFoundError:
        return
    total = sum(size for _mtime, size, _path in entries)
    for _mtime, size, path in sorted(entries):
        if total <= CACHE_LIMIT_BYTES:
            break
        try:
            os.remove(path)
            total -= size
        except FileNotFoundError:
            pass
