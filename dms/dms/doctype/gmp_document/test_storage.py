# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Tests for the DMS file storage (dms.dms.storage).

The local backend runs for real. The S3 backend runs against an in-memory
stand-in for the frappe_s3_attachment API, so these tests need neither the app
nor a bucket; TestRealBucket repeats the essentials against a real
S3-compatible server when site_config.json has a ``dms_test_s3`` entry:

    "dms_test_s3": {"endpoint_url": "http://s3:8333", "bucket": "dms-test",
                    "key": "...", "secret": "...", "region": "us-east-1"}

Writes are committed where the behaviour under test is what happens at commit
or rollback; every test removes what it stored."""

import hashlib
import os
import unittest

from io import BytesIO
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms.dms import storage


class FakeS3:
    """The frappe_s3_attachment.storage API, in memory."""

    def __init__(self):
        self.objects = {}
        self.gets = 0

    def is_configured(self):
        return True

    def object_key(self, *parts):
        return "/".join(["site-files", *parts])

    def put_object(self, key, content, content_type=None, metadata=None):
        self.objects[key] = (bytes(content), dict(metadata or {}))

    def get_object(self, key):
        self.gets += 1
        if key not in self.objects:
            frappe.throw("NoSuchKey")
        return self.objects[key][0]

    def object_exists(self, key):
        return key in self.objects

    def delete_object(self, key):
        self.objects.pop(key, None)

    def get_s3_key(self, file_doc):
        return None

    def is_s3_file(self, file_doc):
        return False

    def read_file(self, file_doc):
        raise AssertionError("not an S3 file")


class StorageTestCase(FrappeTestCase):
    def setUp(self):
        self.docname = f"TST-STO-{frappe.generate_hash(length=6).upper()}-1"
        self.addCleanup(self._remove_stored)
        self._set_s3_setting(0)

    def _remove_stored(self):
        frappe.db.rollback()
        storage.delete_document_files(self.docname)
        frappe.db.commit()

    def _set_s3_setting(self, value):
        # Committed both ways: tests commit, and must not leave it switched.
        previous = frappe.db.get_single_value("DMS Settings", "store_files_on_s3")
        frappe.db.set_single_value("DMS Settings", "store_files_on_s3", value)
        frappe.db.commit()

        def restore():
            frappe.db.set_single_value("DMS Settings", "store_files_on_s3", previous)
            frappe.db.commit()

        self.addCleanup(restore)

    def _row(self, file_name="a.pdf"):
        return storage.get_row(self.docname, file_name)

    def _path(self, row):
        return storage._local_path(row.storage_key)


class TestLocalStorage(StorageTestCase):
    def test_round_trip(self):
        url = storage.put(self.docname, "a.pdf", b"%PDF-1.4 one")

        self.assertEqual(storage.parse_file_url(url), (self.docname, "a.pdf"))
        self.assertEqual(storage.read(self.docname, "a.pdf"), b"%PDF-1.4 one")
        row = self._row()
        self.assertEqual(row.storage, storage.LOCAL)
        self.assertEqual(row.sha256, hashlib.sha256(b"%PDF-1.4 one").hexdigest())
        self.assertEqual(row.file_size, len(b"%PDF-1.4 one"))
        self.assertTrue(row.storage_key.startswith(f"dms/{self.docname}/"))
        with open(storage.local_path(self.docname, "a.pdf"), "rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 one")

    def test_other_urls_are_not_stored_files(self):
        self.assertIsNone(storage.parse_file_url("/private/files/a.pdf"))
        self.assertIsNone(storage.parse_file_url(None))
        self.assertIsNone(storage.parse_file_url(storage.FILE_ENDPOINT + "?docname=X"))

    def test_a_file_changed_on_disk_is_refused(self):
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 original")
        with open(self._path(self._row()), "wb") as fh:
            fh.write(b"%PDF-1.4 altered")

        with self.assertRaises(storage.StoredFileIntegrityError):
            storage.read(self.docname, "a.pdf")
        with self.assertRaises(storage.StoredFileIntegrityError):
            storage.local_path(self.docname, "a.pdf")

    def test_a_vanished_file_is_reported_missing(self):
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 gone")
        os.remove(self._path(self._row()))

        with self.assertRaises(storage.StoredFileMissing):
            storage.read(self.docname, "a.pdf")
        with self.assertRaises(storage.StoredFileMissing):
            storage.read(self.docname, "never-stored.pdf")

    def test_the_old_version_goes_only_when_the_new_one_commits(self):
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 v1")
        frappe.db.commit()
        old = self._path(self._row())

        storage.put(self.docname, "a.pdf", b"%PDF-1.4 v2")
        new = self._path(self._row())
        self.assertNotEqual(old, new)
        self.assertTrue(os.path.exists(old), "the committed version must survive until commit")

        frappe.db.commit()
        self.assertFalse(os.path.exists(old))
        self.assertEqual(storage.read(self.docname, "a.pdf"), b"%PDF-1.4 v2")

    def test_a_rolled_back_write_leaves_the_previous_version(self):
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 v1")
        frappe.db.commit()

        storage.put(self.docname, "a.pdf", b"%PDF-1.4 v2")
        new = self._path(self._row())
        frappe.db.rollback()

        self.assertFalse(os.path.exists(new), "the rolled-back version must not be left behind")
        self.assertEqual(storage.read(self.docname, "a.pdf"), b"%PDF-1.4 v1")

    def test_storing_the_same_bytes_again_keeps_the_file(self):
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 same")
        frappe.db.commit()
        key = self._row().storage_key

        storage.put(self.docname, "a.pdf", b"%PDF-1.4 same")
        frappe.db.commit()
        self.assertEqual(self._row().storage_key, key)
        self.assertEqual(storage.read(self.docname, "a.pdf"), b"%PDF-1.4 same")

    def test_deleting_removes_the_file_after_commit(self):
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 bye")
        frappe.db.commit()
        path = self._path(self._row())

        storage.delete_document_files(self.docname)
        self.assertTrue(os.path.exists(path))
        frappe.db.commit()
        self.assertFalse(os.path.exists(path))
        self.assertFalse(storage.exists(self.docname, "a.pdf"))

    def test_stored_files_are_not_downloadable_as_files(self):
        """/private/files/... serves only File rows; a stored file has none."""
        from frappe.core.doctype.file.utils import find_file_by_url

        storage.put(self.docname, "a.pdf", b"%PDF-1.4 private")
        url = "/private/files/" + self._row().storage_key
        self.assertIsNone(find_file_by_url(url))


class TestS3Storage(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.s3 = FakeS3()
        patcher = patch.object(storage, "s3_module", return_value=self.s3)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._remove_stored)  # while the fake bucket is in place
        self._set_s3_setting(1)

    def test_new_files_go_to_the_bucket(self):
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 cloud")

        row = self._row()
        self.assertEqual(row.storage, storage.S3)
        self.assertTrue(row.storage_key.startswith(f"site-files/dms/{self.docname}/"))
        content, metadata = self.s3.objects[row.storage_key]
        self.assertEqual(content, b"%PDF-1.4 cloud")
        self.assertEqual(metadata["sha256"], row.sha256)
        self.assertFalse(os.path.exists(storage._local_path(row.storage_key)))

        self.assertEqual(storage.read(self.docname, "a.pdf"), b"%PDF-1.4 cloud")

    def test_rendering_gets_a_verified_cached_copy(self):
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 cached")

        path = storage.local_path(self.docname, "a.pdf")
        self.assertTrue(path.endswith(".pdf"))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 cached")
        gets = self.s3.gets
        self.assertEqual(storage.local_path(self.docname, "a.pdf"), path)
        self.assertEqual(self.s3.gets, gets, "a valid cached copy is not fetched again")

        with open(path, "wb") as fh:
            fh.write(b"damaged")
        with open(storage.local_path(self.docname, "a.pdf"), "rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 cached", "a damaged copy is fetched again")

    def test_an_object_changed_in_the_bucket_is_refused(self):
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 original")
        key = self._row().storage_key
        self.s3.objects[key] = (b"%PDF-1.4 altered", {})

        with self.assertRaises(storage.StoredFileIntegrityError):
            storage.read(self.docname, "a.pdf")

    def test_a_rolled_back_upload_is_removed_from_the_bucket(self):
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 v1")
        frappe.db.commit()
        old = self._row().storage_key

        storage.put(self.docname, "a.pdf", b"%PDF-1.4 v2")
        new = self._row().storage_key
        frappe.db.rollback()

        self.assertNotIn(new, self.s3.objects)
        self.assertIn(old, self.s3.objects)

    def test_s3_selected_but_unusable_is_refused(self):
        self.s3.is_configured = lambda: False
        with self.assertRaises(frappe.ValidationError):
            storage.put(self.docname, "a.pdf", b"%PDF-1.4 nowhere")

    def test_moving_to_the_bucket_and_back(self):
        self._set_s3_setting(0)
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 local first")
        storage.put(self.docname, "b.png", b"\x89PNG signature")
        frappe.db.commit()
        local_paths = [self._path(self._row(n)) for n in ("a.pdf", "b.png")]

        result = storage.move_stored_files(storage.S3, self.docname)
        self.assertEqual(result, {"moved": 2, "failed": 0})
        for name, path in zip(("a.pdf", "b.png"), local_paths):
            row = self._row(name)
            self.assertEqual(row.storage, storage.S3)
            self.assertIn(row.storage_key, self.s3.objects)
            self.assertFalse(os.path.exists(path), "the local copy goes once the move committed")
        self.assertEqual(storage.read(self.docname, "a.pdf"), b"%PDF-1.4 local first")

        result = storage.move_stored_files(storage.LOCAL, self.docname)
        self.assertEqual(result, {"moved": 2, "failed": 0})
        self.assertEqual(self._row("a.pdf").storage, storage.LOCAL)
        self.assertFalse(self.s3.objects)
        self.assertEqual(storage.read(self.docname, "b.png"), b"\x89PNG signature")

    def test_a_damaged_file_is_not_moved(self):
        self._set_s3_setting(0)
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 fine")
        frappe.db.commit()
        with open(self._path(self._row()), "wb") as fh:
            fh.write(b"tampered")

        result = storage.move_stored_files(storage.S3, self.docname)

        self.assertEqual(result, {"moved": 0, "failed": 1})
        self.assertEqual(self._row().storage, storage.LOCAL)
        self.assertFalse(self.s3.objects)


_S3_SETTINGS_FIELDS = ("bucket_name", "aws_key", "region_name", "endpoint_url", "folder_name")


def use_real_bucket():
    """Point S3 File Attachment at the ``dms_test_s3`` server, in a folder of
    its own; returns a function that puts the previous settings back."""
    from frappe_s3_attachment import storage as s3

    conf = frappe._dict(frappe.conf.dms_test_s3)
    settings = frappe.get_doc("S3 File Attachment")
    previous = {field: settings.get(field) for field in _S3_SETTINGS_FIELDS}
    settings.update(
        {
            "bucket_name": conf.bucket,
            "aws_key": conf.key,
            "aws_secret": conf.secret,
            "region_name": conf.region or "us-east-1",
            "endpoint_url": conf.endpoint_url,
            "folder_name": f"dms-tests-{frappe.generate_hash(length=6)}",
        }
    )
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    s3.reset_client()
    try:
        s3._operations().S3_CLIENT.create_bucket(Bucket=conf.bucket)
    except Exception:
        pass  # already there

    folder = settings.folder_name

    def restore():
        # Empty the test folder: uploads the File hook moved there are kept by
        # the bucket when "Delete file from cloud" is off.
        client = s3._operations().S3_CLIENT
        listed = client.list_objects_v2(Bucket=conf.bucket, Prefix=folder + "/")
        for obj in listed.get("Contents", []):
            client.delete_object(Bucket=conf.bucket, Key=obj["Key"])
        settings = frappe.get_doc("S3 File Attachment")
        settings.update(previous)
        settings.aws_secret = None
        settings.save(ignore_permissions=True)
        frappe.db.commit()
        s3.reset_client()

    return restore


@unittest.skipUnless(frappe.conf.get("dms_test_s3"), "no dms_test_s3 entry in site_config.json")
class TestRealBucket(StorageTestCase):
    """The same guarantees against a real S3-compatible server, through the
    real frappe_s3_attachment app."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._restore_settings = staticmethod(use_real_bucket())

    @classmethod
    def tearDownClass(cls):
        cls._restore_settings()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self._set_s3_setting(1)
        from frappe_s3_attachment import storage as s3

        self.s3 = s3

    def test_round_trip_through_the_bucket(self):
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 real bucket")
        frappe.db.commit()
        row = self._row()

        self.assertEqual(row.storage, storage.S3)
        self.assertTrue(self.s3.object_exists(row.storage_key))
        self.assertEqual(storage.read(self.docname, "a.pdf"), b"%PDF-1.4 real bucket")
        with open(storage.local_path(self.docname, "a.pdf"), "rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 real bucket")

        storage.delete_document_files(self.docname)
        frappe.db.commit()
        self.assertFalse(self.s3.object_exists(row.storage_key))

    def test_tampering_in_the_bucket_is_refused(self):
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 genuine")
        frappe.db.commit()
        self.s3.put_object(self._row().storage_key, b"%PDF-1.4 forged")

        with self.assertRaises(storage.StoredFileIntegrityError):
            storage.read(self.docname, "a.pdf")

    def test_moving_to_the_bucket_and_back(self):
        self._set_s3_setting(0)
        storage.put(self.docname, "a.pdf", b"%PDF-1.4 travelling")
        frappe.db.commit()
        local = self._path(self._row())

        self.assertEqual(storage.move_stored_files(storage.S3, self.docname)["moved"], 1)
        key = self._row().storage_key
        self.assertTrue(self.s3.object_exists(key))
        self.assertFalse(os.path.exists(local))

        self.assertEqual(storage.move_stored_files(storage.LOCAL, self.docname)["moved"], 1)
        self.assertFalse(self.s3.object_exists(key))
        self.assertEqual(storage.read(self.docname, "a.pdf"), b"%PDF-1.4 travelling")
