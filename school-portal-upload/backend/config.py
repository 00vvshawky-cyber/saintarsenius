import os
import secrets
from dotenv import load_dotenv

load_dotenv()

# Never ship a published default: fall back to a per-process random key
# (sessions die on restart, but session forgery becomes impossible).
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
SERVER_MODE = os.getenv("SERVER_MODE", "school")
DB_PATH = os.getenv("DB_PATH", "school.db")
TZ = os.getenv("TZ", "Africa/Cairo")
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")

if not os.path.exists(BACKUP_DIR):
    os.makedirs(BACKUP_DIR)
