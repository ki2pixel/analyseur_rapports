#!/usr/bin/env bash

# Small helper to launch the service dashboard (Python/Flask UI with start buttons).
# Usage:
#   ./scripts/launch_services.sh
# Optional environment overrides (forwarded to the dashboard):
#   SERVICE_DASHBOARD_HOST=0.0.0.0
#   SERVICE_DASHBOARD_PORT=5500
#   SERVICE_DASHBOARD_BROWSER=0
#
# Secrets are loaded from `${PROJECT_ROOT}/.env` when present
# (ADMIN_TOKEN, FLASK_SECRET_KEY, …). Copy `.env.example` if needed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Activation automatique de l'environnement virtuel si présent
if [ -f "${PROJECT_ROOT}/venv/bin/activate" ]; then
    echo "Activation de l'environnement virtuel..."
    set +u
    source "${PROJECT_ROOT}/venv/bin/activate"
    set -u
fi

# Charge les variables locales (.env) sans écraser un export déjà défini
# uniquement pour les clés absentes : on source puis on respecte les exports
# préexistants via un snapshot avant chargement.
load_dotenv_file() {
    local env_file="$1"
    if [ ! -f "${env_file}" ]; then
        return 0
    fi
    echo "Chargement de ${env_file}..."
    local prior_admin="${ADMIN_TOKEN-__unset__}"
    local prior_secret="${FLASK_SECRET_KEY-__unset__}"
    set +u
    set -a
    # shellcheck disable=SC1090
    source "${env_file}"
    set +a
    set -u
    if [ "${prior_admin}" != "__unset__" ]; then
        export ADMIN_TOKEN="${prior_admin}"
    fi
    if [ "${prior_secret}" != "__unset__" ]; then
        export FLASK_SECRET_KEY="${prior_secret}"
    fi
}

load_dotenv_file "${PROJECT_ROOT}/.env"

if [ -z "${ADMIN_TOKEN:-}" ]; then
    echo "ADMIN_TOKEN manquant." >&2
    echo "Copiez .env.example vers .env et renseignez ADMIN_TOKEN / FLASK_SECRET_KEY." >&2
    exit 1
fi

# Function to kill processes on a specific port
kill_port() {
    local port=$1
    local pids
    pids=$(lsof -ti:"$port" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "Killing processes on port $port: $pids"
        # shellcheck disable=SC2086
        kill $pids 2>/dev/null || true
        sleep 1
    fi
}

# Kill any existing processes on the ports we use
kill_port 5500  # Dashboard port
kill_port 5010  # Reporting port
kill_port 5000  # BDC converter port
kill_port 5030  # Reconciliation port

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 est requis pour lancer le tableau de bord." >&2
  exit 1
fi

cd "${PROJECT_ROOT}"
exec python3 scripts/service_dashboard.py
