-- NyaySutra AI — the private case-files bucket accepts the additional document formats the app now reads:
-- CSV/TSV, Excel (.xlsx, .xls), OpenDocument (.odt, .ods), RTF, PowerPoint (.pptx), BMP and GIF images.
-- The app still checks every upload's content signature and active content (macros, embedded objects, external
-- links) before storing it; this list is the storage layer's own second check. Idempotent.

update storage.buckets
set allowed_mime_types = array[
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/msword',
    'application/rtf',
    'application/vnd.oasis.opendocument.text',
    'text/plain',
    'text/markdown',
    'text/csv',
    'text/tab-separated-values',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-excel',
    'application/vnd.oasis.opendocument.spreadsheet',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'image/png',
    'image/jpeg',
    'image/tiff',
    'image/webp',
    'image/bmp',
    'image/gif'
]
where id = 'case-files';

insert into public.meta(key, value) values ('schema_version', '5')
on conflict (key) do update set value = excluded.value;
