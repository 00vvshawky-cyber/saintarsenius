"""Audit trail (spec §12) — every sensitive mutation is recorded."""
import json

from backend.db import get_db, db_transaction
from backend.utils.timeutil import now_utc


def log_action(action, actor_id=None, target_type=None, target_id=None, details=None):
    """Append an entry to audit_log. Never raises into the caller."""
    try:
        with db_transaction() as conn:
            conn.cursor().execute(
                """
                INSERT INTO audit_log (actor_id, action, target_type, target_id, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_id,
                    action,
                    target_type,
                    target_id,
                    json.dumps(details, ensure_ascii=False) if details else None,
                    now_utc().isoformat(),
                ),
            )
    except Exception:
        pass
