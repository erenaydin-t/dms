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
# Bootstraps the QA Manager role referenced by GMP Document permissions.
before_install = "dms.install.before_install"

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
        "filters": [["role_name", "in", ["QA Manager"]]],
    },
]

# ------------------------------------------------------------------------- #
#  Document events                                                          #
# ------------------------------------------------------------------------- #
# Lifecycle for GMP Document is handled inside the controller class; nothing
# is wired here on purpose to keep the hook surface auditable.
doc_events = {}

# ------------------------------------------------------------------------- #
#  Scheduled tasks                                                          #
# ------------------------------------------------------------------------- #
# Daily sweep that flags documents whose next_revision_date is reached.
# Implementation is left as an extension point - wire a method in the
# controller (e.g. `notify_documents_due_for_revision`) and uncomment.
# scheduler_events = {
#     "daily": [
#         "dms.dms.doctype.gmp_document.gmp_document.notify_documents_due_for_revision",
#     ],
# }

# ------------------------------------------------------------------------- #
#  Required apps                                                            #
# ------------------------------------------------------------------------- #
required_apps = ["frappe", "erpnext", "hrms"]  # hrms supplies Employee + Department
