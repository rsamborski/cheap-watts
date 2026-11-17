#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/pstryk-pilot}"
IMAGE="${IMAGE:-ghcr.io/rsamborski/pstryk-pilot:latest}"
CONTAINER_UID="${CONTAINER_UID:-1000}"
CONTAINER_GID="${CONTAINER_GID:-1000}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker CLI is not installed or not in PATH." >&2
  exit 1
fi

run_priv() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  elif [[ -w "$(dirname "${INSTALL_DIR}")" ]] && { [[ ! -e "${INSTALL_DIR}" ]] || [[ -w "${INSTALL_DIR}" ]]; }; then
    "$@"
  else
    sudo "$@"
  fi
}

HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

echo "==> Bootstrapping pstryk-pilot host directory at ${INSTALL_DIR}..."
run_priv mkdir -p "${INSTALL_DIR}/data" "${INSTALL_DIR}/logs"
run_priv chown "${HOST_UID}:${HOST_GID}" "${INSTALL_DIR}"

# Container runs as UID/GID 1000 (pilot) and reads/writes data/ and logs/.
run_priv chown -R "${CONTAINER_UID}:${CONTAINER_GID}" "${INSTALL_DIR}/data" "${INSTALL_DIR}/logs"
run_priv chmod 700 "${INSTALL_DIR}/data"
run_priv chmod 750 "${INSTALL_DIR}/logs"

if [[ -f "${INSTALL_DIR}/data/cardata_tokens.json" ]]; then
  run_priv chown "${CONTAINER_UID}:${CONTAINER_GID}" "${INSTALL_DIR}/data/cardata_tokens.json"
  run_priv chmod 600 "${INSTALL_DIR}/data/cardata_tokens.json"
fi

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  run_priv touch "${INSTALL_DIR}/.env"
  run_priv chown "${HOST_UID}:${HOST_GID}" "${INSTALL_DIR}/.env"
  run_priv chmod 600 "${INSTALL_DIR}/.env"
  ENV_CREATED=1
else
  run_priv chmod 600 "${INSTALL_DIR}/.env"
  ENV_CREATED=0
fi

echo "==> Pulling ${IMAGE}..."
if docker pull "${IMAGE}"; then
  PULL_OK=1
else
  PULL_OK=0
  echo "Warning: failed to pull ${IMAGE}." >&2
  echo "If the GHCR package is private, log in first with a read:packages PAT:" >&2
  echo "  docker login ghcr.io -u <github-username>" >&2
fi

cat <<EOF

Setup complete for ${INSTALL_DIR}.
Directory layout:
  ${INSTALL_DIR}/.env   (mode 0600, host-owned)
  ${INSTALL_DIR}/data   (mode 0700, owner ${CONTAINER_UID}:${CONTAINER_GID})
  ${INSTALL_DIR}/logs   (mode 0755, owner ${CONTAINER_UID}:${CONTAINER_GID})

Next steps:
  1. $([[ "${ENV_CREATED}" -eq 1 ]] && echo "Populate ${INSTALL_DIR}/.env from .env.example before running." || echo "Verify ${INSTALL_DIR}/.env is up to date.")
$(if [[ "${PULL_OK}" -eq 0 ]]; then
  echo "  2. Authenticate Docker to GHCR and pull the image:"
  echo "       docker login ghcr.io -u <github-username>"
  echo "       docker pull ${IMAGE}"
fi)
  3. Provision BMW CarData tokens (skip if data/cardata_tokens.json was copied):
       docker run -it --rm \\
         --env-file ${INSTALL_DIR}/.env \\
         -v ${INSTALL_DIR}/data:/app/data \\
         ${IMAGE} cardata-auth

  4. Verify connectivity and dry-run pipeline:
       docker run --rm --network host \\
         --env-file ${INSTALL_DIR}/.env \\
         -v ${INSTALL_DIR}/data:/app/data \\
         -v ${INSTALL_DIR}/logs:/app/logs \\
         ${IMAGE} probe

       docker run --rm --network host \\
         --env-file ${INSTALL_DIR}/.env \\
         -v ${INSTALL_DIR}/data:/app/data \\
         -v ${INSTALL_DIR}/logs:/app/logs \\
         ${IMAGE} run --dry-run

  5. Install cron jobs from deploy/crontab.example:
       crontab -e
EOF
