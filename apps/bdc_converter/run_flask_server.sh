#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="/home/kidpixel6/convert_bdc"
VENV_PATH="/mnt/venv_ext4/venv_convert_bdc"

if [[ ! -d "$PROJECT_ROOT" ]]; then
  echo "Projet introuvable à l'emplacement attendu: $PROJECT_ROOT" >&2
  exit 1
fi

if [[ ! -d "$VENV_PATH" ]]; then
  echo "L'environnement virtuel n'existe pas: $VENV_PATH" >&2
  echo "Créez-le avec : python3 -m venv $VENV_PATH" >&2
  exit 1
fi

source "$VENV_PATH/bin/activate"

export FLASK_APP=app
export FLASK_RUN_HOST=0.0.0.0
export FLASK_RUN_PORT=5000

cd "$PROJECT_ROOT"
exec flask run
