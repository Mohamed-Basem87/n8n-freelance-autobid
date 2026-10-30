# Portfolio images

Every proposal attaches exactly **four** images from the folder the backend is
configured to read (`LOKI_ATTACHMENTS_DIR`, default `<data>/attachments`).

Put your own screenshots here — dashboards, reports, before/after cleaning —
then copy them to the server:

```bash
cp *.png /opt/freelance-autobid/data/attachments/
```

Rules the service enforces before it opens a browser:

- exactly four files
- `.jpg`, `.jpeg`, `.png` or `.webp`
- at most 10 MB each
- inside the configured directory

Nothing in this repo ships with real portfolio images; use your own work.
