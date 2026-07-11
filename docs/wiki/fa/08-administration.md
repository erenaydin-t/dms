# راهبری و استقرار

## نصب / به‌روزرسانی (بنچ داکری)

```bash
# داخل کانتینر backend
cd /home/frappe/frappe-bench/apps/dms
git fetch upstream --tags && git reset --hard vX.Y.Z     # یا upstream/main
cd /home/frappe/frappe-bench
bench --site <site> migrate
bench build --app dms
# سپس ری‌استارت کانتینرهای backend + workers + scheduler
```

همان `git reset` را در کانتینر **frontend** هم اجرا کنید تا درخت اپ یکسان بماند. `after_migrate` به‌صورت idempotent این موارد را بازتنظیم می‌کند: فیلدهای سفارشی (`Department.custom_abbr`، `Employee.custom_signature_image`)، مرجع انواع سند، قاعده نام‌گذاری Amend، و گردش‌کار (افزودن وضعیت/گذارهای جدید و بازتنظیم شرط‌ها، نقش‌ها و پرچم self-approval).

## چک‌لیست راه‌اندازی الزامی

1. **نقش‌ها** — نویسندگان/تأییدکنندگان: **QA Manager**؛ مالکان ماژول: **DMS Manager**. (Administrator همه گیت‌ها را رد می‌کند — گردش‌کار را همیشه با کاربر *واقعی* آزمایش کنید.)
2. **واحدها** — روی هر Department صاحب سند، `custom_abbr` را تنظیم کنید (مثل QA، HR)؛ بدون آن نام‌گذاری خطا می‌دهد.
3. **امضاها** — هر تهیه‌کننده/بررسی‌کننده/تأییدکننده QA باید رکورد Employee با `user_id` متصل و تصویر PNG/JPG در «Signature (PNG)» داشته باشد.
4. **گردش‌کار** — `GMP Document Workflow` باید **Active** باشد (غیرفعال‌شدنش همه گذارها را با «Workflow not found» می‌شکند).
5. **قالب Word** — دست‌کم یک رکورد `GMP Word Template` (حتی با نگاشت خالی).
6. **LibreOffice** — دستور `soffice` باید در PATH کانتینرهای backend/worker باشد (تبدیل PDF).
7. **زمان‌بندها فعال** — کارهای روزانه `activate_effective_documents` (اول) و `expire_gmp_documents`.

## استثنای الزامی S3 / آفلود پیوست‌ها

اگر `frappe_s3_attachment` (یا مشابه) نصب است، فایل‌های DMS **باید روی دیسک محلی بمانند** (رندر، هش‌گیری، واترمارک و مهر مجدد در درخواست‌های جداگانه از دیسک می‌خوانند). در `site_config.json`:

```json
"ignore_s3_upload_for_doctype": ["Data Import", "GMP Document", "GMP Word Template", "Employee"]
```

نشانه‌های نبود این استثنا: خطای *«Attached file is missing on disk»* هنگام ذخیره، خطای یافتن امضا، یا شکست رندر هنگام تأیید.

## سطح API (REST)

| Endpoint | کاربرد |
|---|---|
| `POST /api/method/frappe.model.workflow.apply_workflow` | اجرای گذارها (`doc` به‌صورت JSON + `action`) |
| `POST …gmp_document.gmp_document.create_revision` | `docname` و `reason_for_change` ← نام پیش‌نویس جدید |
| `GET …gmp_document.gmp_document.download_watermarked_pdf` | `docname` + پارامتر اختیاری `variant` = `controlled` \| `uncontrolled` \| `plain` |
| `GET …gmp_document.gmp_document.download_word_document` | مدیران: فایل مبدأ تمیز |

## تأییدشده با آزمون خودکار سرتاسری (۱۸ تیر ۱۴۰۵ / 2026-07-09)

مجموعه آزمون زنده (مبتنی بر REST با راستی‌آزمایی OCR و لایه متن هر PDF — **۱۳۶/۱۳۶ بررسی موفق**) پوشش می‌دهد: ایجاد و نام‌گذاری، گردش‌کار کامل تأیید، هر سه نوع PDF (واترمارک‌ها، پاورقی شمسی، امضاها، مهر QA)، بازنگری بدون ابطال (اعتبار نسخه قبلی؛ منسوخ‌سازی خودکار؛ انتقال ارجاع‌ها)، بازنگری لغوشده (نگهداری + نام‌گذاری تلاش بعدی)، قاعده تک‌بازنگری باز، تاریخ اجرای آینده/گذشته با زمان‌بند فعال‌سازی، مجوزهای فردی گردش‌کار و سناریوهای خطای API.

---
📄 نسخه انگلیسی: [/dms/administration](/dms/administration)
