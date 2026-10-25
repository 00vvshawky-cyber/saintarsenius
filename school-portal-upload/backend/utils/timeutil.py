from datetime import datetime, timezone, timedelta, time, date
from zoneinfo import ZoneInfo

CAIRO_TZ = ZoneInfo("Africa/Cairo")

def now_utc():
    return datetime.now(timezone.utc)

def to_cairo(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CAIRO_TZ)

def week_key(dt=None):
    if dt is None:
        dt = now_utc()
    cairo_dt = to_cairo(dt)
    cairo_date = cairo_dt.date()
    days_since_thursday = (cairo_date.weekday() - 3) % 7
    thursday_date = cairo_date - timedelta(days=days_since_thursday)
    return thursday_date.strftime("%Y-%m-%d")

def closes_at_for(week_key_str):
    thursday_date = date.fromisoformat(week_key_str)
    wednesday_date = thursday_date + timedelta(days=6)
    cairo_dt = datetime.combine(wednesday_date, time(23, 59, 59), tzinfo=CAIRO_TZ)
    utc_dt = cairo_dt.astimezone(timezone.utc)
    return utc_dt.isoformat()

def friday_to_week_key(d):
    if isinstance(d, str):
        d = date.fromisoformat(d)
    thursday_date = d - timedelta(days=1)
    return thursday_date.strftime("%Y-%m-%d")
