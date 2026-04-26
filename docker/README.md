# DMS — Docker Compose Test Stack

Self-contained docker-compose stack that spins up ERPNext v16 + HRMS + DMS
on a fresh site. Use this for local testing and demos.

## Layout

```
docker/
├── Dockerfile           # builds custom-erpnext:latest with DMS baked in
├── apps.json            # apps to install (frappe / erpnext / hrms / dms)
├── docker-compose.yml   # full stack: backend / frontend / db / redis / queues
├── .env.example         # copy to .env and edit as needed
├── update-dms.sh        # build + restart + migrate (run after every code change)
└── create-site.sh       # one-time site provisioning
```

## Prerequisites

- Docker Engine 24+ with BuildKit
- `docker compose` v2

## First-time setup

```bash
cd docker
cp .env.example .env
# (Optional) edit .env — change SITE_NAME, ADMIN_PASSWORD, ports, DMS branch/tag

./update-dms.sh          # builds the image and brings the stack up (~15 min first time)
./create-site.sh         # creates the site and installs ERPNext + HRMS + DMS

# Add to /etc/hosts so the browser resolves the site
echo "127.0.0.1 dms.localhost" | sudo tee -a /etc/hosts
```

Then open <http://dms.localhost:8080> and log in as `Administrator` with
the password you set in `.env`.

## Updating after a code change

After editing the DMS source (or pulling a new release):

```bash
./update-dms.sh
```

This rebuilds the image, restarts containers, and runs `bench migrate` on
the site.

## Pinning a specific DMS release

`apps.json` controls which DMS version is baked into the image. To pin a
release tag:

```json
{
  "url": "https://github.com/erenaydin-t/dms",
  "branch": "v0.1.0"
}
```

Then rebuild:

```bash
./update-dms.sh
```

The same approach works for `erpnext` and `hrms` if you need to pin them.

## Troubleshooting

### `ModuleNotFoundError: No module named 'dms'`

The image was built but Python can't import `dms`. Most common cause: the
clone went into a directory whose name doesn't match the package name.
Verify:

```bash
docker compose exec backend ls apps/
docker compose exec backend cat sites/apps.txt
```

`apps/dms/` must exist and `dms` must be in `apps.txt`.

### LibreOffice not found at runtime

The `download_watermarked_pdf` endpoint needs `soffice` on PATH. The
Dockerfile installs `libreoffice-core` + `libreoffice-writer`. Verify:

```bash
docker compose exec backend which soffice && docker compose exec backend soffice --version
```

### Reset everything

```bash
docker compose down -v        # removes containers AND volumes (data!)
./update-dms.sh
./create-site.sh
```

## Resource expectations

- First image build: ~15 minutes, ~3 GB image
- Running stack: ~2 GB RAM, idle ~5% CPU
- Re-builds with cached layers: ~2 minutes

## CI / releases

Tagging `vX.Y.Z` on `main` triggers `.github/workflows/release.yml`,
which publishes a GitHub Release. Users can pin to that tag in `apps.json`
to install a known-good version.
