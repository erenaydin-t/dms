# Follow-up / Backlog

Tracked infrastructure work that is **not** a release blocker but should be
picked up as the next improvements. Added after the v1.2.4 release-readiness
audit.

---

## 1. Pin the stack to stable Frappe v16 (blocked on upstream)

**Status:** waiting on an official stable Frappe `v16` release/tag.

**Context.** The intended application stack is **ERPNext 16 / HRMS 16**. However,
`docker/apps.json` and the `Dockerfile` currently track `frappe@version-16`,
which at the time of the audit resolves to **`17.0.0-dev` (Python 3.14)** — an
upstream branch/tag drift. This frappe-17-dev vs erpnext/hrms-16 mismatch is an
**environmental inconsistency**, not a stack we want to adopt. It is what makes
ERPNext's own test bootstrap unreliable in CI (see item 3).

**Action — once a stable Frappe `v16` tag exists:**
1. Pin Frappe to that tag in:
   - `docker/apps.json` → the `frappe` entry's `branch` (use the stable tag).
   - `docker/Dockerfile` → `ARG FRAPPE_BRANCH` (default to the stable tag).
   - `.github/workflows/ci.yml` — no Python pin is needed there anymore once the
     stack is built via Docker, but confirm the base image resolves to v16.
2. Align local/dev/runtime environments to the same Frappe v16 tag.
3. Rebuild the image and re-validate (see item 4).

## 2. Remove the temporary CI/test workarounds (do together with item 1)

These exist **only** because of the current Frappe 17-dev mismatch and should be
removed once the stack is pinned to stable v16 and re-validated:

- **`IGNORE_TEST_RECORD_DEPENDENCIES`** in
  `dms/dms/doctype/gmp_document/test_gmp_document.py`
  (`["Employee", "Department", "Company", "User"]`). It exists because importing
  ERPNext's Company test module runs `erpnext.tests.utils` at import time
  (`BootStrapTestData` → Item opening stock → Stock Entry submit), which fails on
  the mismatched bench. On a correct frappe-16 + erpnext-16 stack this should no
  longer be necessary — verify, then remove.
- **Per-module test invocation in CI.** `.github/workflows/ci.yml` runs each DMS
  test module explicitly instead of `bench run-tests --app dms`, because the
  app-wide run triggers the same ERPNext test-record bootstrap. Once item 1 is
  done, try reverting to a single `--app dms` run.
- Re-check whether `--skip-before-tests` is still required.

## 3. Re-validate the full DMS suite after pinning

After items 1–2:
- Run the full DMS suite on a fresh site (`bench run-tests --app dms`) and
  confirm all tests pass without the workarounds.
- Confirm `bench migrate` is clean and the install/patch path still seeds the
  `DMS Manager` role, workflow `allow_edit`, and document types.

## 4. (Done in this change) CI Docker build caching

Implemented: the CI runtime-test job builds the image with Buildx + GitHub
Actions layer cache (`cache-from/to: type=gha`) and a stable `CACHEBUST`, so
Docker layers, the base image, and the Python/bench/app dependencies installed
during `bench init` are reused across runs. The checked-out DMS code is still
overlaid onto the image before the tests run, so coverage is unchanged. If the
gha cache is evicted (size limits), the job simply rebuilds.
