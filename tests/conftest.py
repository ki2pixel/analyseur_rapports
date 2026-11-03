import os
from pathlib import Path
import sys

# Security defaults for local pytest runs (must be set before app imports).
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
