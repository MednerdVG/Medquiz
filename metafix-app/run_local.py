"""One-command local launcher (Windows / Mac / Linux).

    python run_local.py

Uses a local SQLite file, sends no real messages (fake mode), and opens the browser.
"""
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///./metafix_local.db")
os.environ.setdefault("DEV_LOGIN", "1")
os.environ.setdefault("INTEGRATIONS_MODE", "fake")
os.environ.setdefault("STORAGE_DIR", str(HERE / "storage"))
os.environ.setdefault("BASE_URL", "http://127.0.0.1:8000")
sys.path.insert(0, str(HERE))

first_run = not (HERE / "metafix_local.db").exists()
from app.db import SessionLocal, create_all  # noqa: E402
from app.seed import seed_demo, seed_users  # noqa: E402

create_all()
db = SessionLocal()
seed_users(db)
if first_run:
    seed_demo(db)
db.close()

print("\nMetaFix is starting at http://127.0.0.1:8000")
print("Sign in with 'Dev sign-in' using one of:")
print("  coordinator@metafix.clinic  (admin — bookings inbox)")
print("  vishal@metafix.clinic       (owner / doctor)")
print("  shubham@metafix.clinic      (doctor)")
print("Press Ctrl+C to stop.\n")
threading.Thread(target=lambda: (time.sleep(2), webbrowser.open("http://127.0.0.1:8000")), daemon=True).start()
subprocess.run([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"])
