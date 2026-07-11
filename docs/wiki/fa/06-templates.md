# قالب‌ها، تگ‌ها و بوکمارک‌ها

فایل مبدأ بارگذاری‌شده (docx از طریق `docxtpl`، و همچنین xlsx و vsdx) یک **قالب** است: هنگام تأیید QA، تگ‌های Jinja با داده‌های زنده سند، امضاها و مهر QA جایگزین می‌شوند. فایل بارگذاری‌شده اولیه به‌عنوان منبع رندرِ تغییرناپذیر حفظ می‌شود تا رندرهای بعدی همیشه تگ‌های اصلی را ببینند.

## استفاده از تگ در فایل Word

تگ‌ها را در متن، سرصفحه یا پاصفحه فایل docx قرار دهید:

```
Document ID: {{ docname }}
Title: {{ document_name_en }}
Version: {{ version_number }}
Effective Date: {{ effective_date }}      Expiry Date: {{ expiry_date }}
Reason for Change: {{ reason_for_change }}

Prepared by: {{ prepared_by_name }}   {{ preparer_signature }}
Reviewed by: {{ reviewed_by_name }}   {{ reviewer_signature }}
QA Approved by: {{ approved_by_name }} {{ qa_signature }}
QA Stamp: {{ qa_stamp }}
```

> هر تگ را یک‌جا تایپ کنید (حرف‌به‌حرف ویرایش نکنید) تا Word آن را در یک Run نگه دارد و تگ نشکند.

## فهرست تگ‌های بومی

**شناسه و نام‌ها:** `docname` (= `name`)، `document_name_fa`، `document_name_en`
**طبقه‌بندی:** `document_type` (برچسب)، `document_type_code`، `department`، `department_name`، `document_owner`، `document_owner_name`، `gmp_impact`، `validity_period`
**چرخه عمر:** `effective_date`، `expiry_date`، `next_revision_date`، `version_number`، `is_active`، `requires_training`، `workflow_status`
**کنترل تغییر:** `reason_for_change`
**افراد:** `prepared_by`، `prepared_by_name`، `reviewer`، `reviewer_name`، `qa_approver`، `qa_approver_name`، `reviewed_by`، `reviewed_by_name`، `reviewed_on`، `approved_by`، `approved_by_name`، `approved_on`
**تصاویر (فقط در PDF امضاشده رندر می‌شوند و در نسخه تمیز خالی‌اند):** `preparer_signature`، `reviewer_signature`، `qa_signature`، `qa_stamp`

## تگ‌های سفارشی (GMP Word Template)

هر سند به یک رکورد **GMP Word Template** متصل است که جدول «Field Mappings» آن، تگ‌های سفارشی را به فیلدهای سیستم نگاشت می‌کند — مثلاً `my_title` ← `document_name_en` تا قالب با `{{ my_title }}` نوشته شود. نگاشت‌ها افزودنی‌اند؛ تگ‌های بومی همچنان کار می‌کنند. یک نگاشت می‌تواند به تگ امضا هم اشاره کند.

## امضاها

تصویر امضا از فیلد **Employee → Signature (PNG)** (`custom_signature_image`) کاربرِ اقدام‌کننده/تعیین‌شده خوانده و با عرض ثابت درج می‌شود. تصویر مهر QA بر اساس وضعیت (تأیید/رد) در لحظه رندر انتخاب می‌گردد.

---
📄 نسخه انگلیسی: [/dms/templates](/dms/templates)
