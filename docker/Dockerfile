# syntax=docker/dockerfile:1.7
#
# Custom ERPNext v16 image with the DMS app baked in.
# Build with:
#   DOCKER_BUILDKIT=1 docker build \
#     --secret id=apps_json,src=apps.json \
#     -t custom-erpnext:latest .
#
# The list of apps to install (frappe, erpnext, hrms, dms) is read from
# apps.json mounted as a build secret.

ARG FRAPPE_BRANCH=version-16

# -------------------------------------------------------------------------- #
#  Builder stage                                                             #
# -------------------------------------------------------------------------- #
FROM frappe/build:${FRAPPE_BRANCH} AS builder

USER frappe
WORKDIR /home/frappe

RUN --mount=type=secret,id=apps_json,uid=1000,target=/home/frappe/apps.json,required=true \
    bench init \
        --frappe-branch=${FRAPPE_BRANCH} \
        --apps_path=/home/frappe/apps.json \
        --skip-redis-config-generation \
        --no-procfile \
        --no-backups \
        /home/frappe/frappe-bench

WORKDIR /home/frappe/frappe-bench

# Install system-level dependency required by GMP Document PDF generation.
USER root
RUN apt-get update && apt-get install --no-install-recommends -y \
        libreoffice-core \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*
USER frappe

# -------------------------------------------------------------------------- #
#  Runtime stage                                                             #
# -------------------------------------------------------------------------- #
FROM frappe/base:${FRAPPE_BRANCH}

USER root
RUN apt-get update && apt-get install --no-install-recommends -y \
        libreoffice-core \
        libreoffice-writer \
        fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

USER frappe
COPY --from=builder --chown=frappe:frappe /home/frappe/frappe-bench /home/frappe/frappe-bench
WORKDIR /home/frappe/frappe-bench

EXPOSE 8000 9000

CMD ["bench", "serve", "--port", "8000"]
