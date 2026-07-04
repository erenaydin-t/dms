from . import __version__ as app_version

app_name = "dms"
app_title = "DMS"
app_publisher = "ErenAydin"
app_description = (
    "GMP / 21 CFR Part 11 compliant Document Management System for ERPNext v16. "
    "Provides controlled-document lifecycle, file integrity (SHA-256), Word "
    "template rendering, versioned amendments, and dynamically watermarked PDFs."
)
app_email = "aydineren1986@gmail.com"
app_license = "MIT"

# ------------------------------------------------------------------------- #
#  Installation hooks                                                       #
# ------------------------------------------------------------------------- #
# Bootstraps the QA Manager role and the Department.custom_abbr field that
# GMP Document depends on. after_migrate re-asserts the field on every
# migrate so a missing column can't silently break autoname().
before_install = "dms.install.before_install"
after_install = "dms.install.after_install"
after_migrate = ["dms.install.after_migrate"]

# ------------------------------------------------------------------------- #
#  Fixtures                                                                 #
# ------------------------------------------------------------------------- #
# Ships the Department.custom_abbr field that GMP Document.autoname depends on.
fixtures = [
    {
        "dt": "Custom Field",
        "filters": [
            ["dt", "=", "Department"],
            ["fieldname", "=", "custom_abbr"],
        ],
    },
    {
        "dt": "Role",
        "filters": [["role_name", "in", ["QA Manager", "DMS Manager"]]],
    },
]

# ------------------------------------------------------------------------- #
#  Document events                                                          #
# ------------------------------------------------------------------------- #
# Lifecycle for GMP Document is handled inside the controller class; nothing
# is wired here on purpose to keep the hook surface auditable.
doc_events = {}

# ------------------------------------------------------------------------- #
#  Permissions                                                              #
# ------------------------------------------------------------------------- #
# Department-scoped, read-only visibility for plain members; full access for
# the DMS Manager (module owner), QA Manager and System Manager roles. The
# query-conditions hook filters lists/reports/search; the has_permission hook
# gates opening a single document and the PDF-download methods. See the
# controller for the model and docs/PERMISSIONS.md for configuration.
permission_query_conditions = {
    "GMP Document": "dms.dms.doctype.gmp_document.gmp_document.get_permission_query_conditions",
}

has_permission = {
    "GMP Document": "dms.dms.doctype.gmp_document.gmp_document.has_permission",
}

# ------------------------------------------------------------------------- #
#  Scheduled tasks                                                          #
# ------------------------------------------------------------------------- #
# Daily sweep that obsoletes documents past their expiry_date and re-stamps
# their base PDF with the QA-rejected stamp. See expire_gmp_documents().
scheduler_events = {
    "daily": [
        "dms.dms.doctype.gmp_document.gmp_document.expire_gmp_documents",
    ],
}

# ------------------------------------------------------------------------- #
#  Required apps                                                            #
# ------------------------------------------------------------------------- #
required_apps = ["frappe", "erpnext"]
