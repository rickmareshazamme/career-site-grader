"""
Persistence layer for the Shazamme Career Site Grader.

SQLite on a Railway volume (DATA_DIR=/data). Degrades gracefully: if the data
directory isn't writable the whole module no-ops so grading never breaks.

Stores:
  - grades   : every grade run  -> benchmark dataset, percentile, history
  - leads    : captured emails   -> sales pipeline
  - monitors : monthly re-score subscriptions
"""

import os
import json
import sqlite3
import threading
import time
from typing import Optional, List, Dict

_DATA_DIR = os.environ.get('DATA_DIR', '')
if not _DATA_DIR or not os.path.isdir(_DATA_DIR):
    # Fall back to /tmp locally / when no volume is mounted (non-persistent).
    _DATA_DIR = os.environ.get('DATA_DIR_FALLBACK', '/tmp')

DB_PATH = os.path.join(_DATA_DIR, 'grader.db')
_LOCK = threading.Lock()
_ENABLED = True


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=15000')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def init_db():
    global _ENABLED
    try:
        with _LOCK, _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS grades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT, domain TEXT, mode TEXT,
                    overall INTEGER, grade TEXT,
                    pillars_json TEXT,
                    created_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_grades_mode ON grades(mode);
                CREATE INDEX IF NOT EXISTS idx_grades_domain ON grades(domain, mode, created_at);

                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT, url TEXT, mode TEXT,
                    overall INTEGER, created_at REAL
                );

                CREATE TABLE IF NOT EXISTS monitors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT, url TEXT, mode TEXT,
                    active INTEGER DEFAULT 1,
                    created_at REAL, last_run REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_unique ON monitors(email, url, mode);

                CREATE TABLE IF NOT EXISTS cwv (
                    url TEXT, mode TEXT,
                    cwv_json TEXT,
                    created_at REAL,
                    PRIMARY KEY (url, mode)
                );

                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY,
                    url TEXT, mode TEXT,
                    report_json TEXT,
                    created_at REAL
                );

                CREATE TABLE IF NOT EXISTS authority (
                    domain TEXT PRIMARY KEY,
                    data_json TEXT,
                    created_at REAL
                );

                CREATE TABLE IF NOT EXISTS outcomes (
                    domain TEXT, mode TEXT, metric TEXT,
                    value REAL, created_at REAL,
                    PRIMARY KEY (domain, mode, metric)
                );
                """
            )
        return True
    except Exception:
        _ENABLED = False
        return False


def enabled() -> bool:
    return _ENABLED


def save_grade(url: str, domain: str, mode: str, overall: int, grade: str,
               pillar_scores: Dict[str, int]) -> Optional[int]:
    if not _ENABLED:
        return None
    try:
        with _LOCK, _connect() as conn:
            cur = conn.execute(
                'INSERT INTO grades (url, domain, mode, overall, grade, pillars_json, created_at) '
                'VALUES (?,?,?,?,?,?,?)',
                (url, domain, mode, overall, grade, json.dumps(pillar_scores), time.time()),
            )
            return cur.lastrowid
    except Exception:
        return None


def percentile(mode: str, score: int, domain: str = '') -> Optional[Dict]:
    """Where this score sits among OTHER sites of the same mode. Dedupes to the
    latest grade per domain (so repeat grades of one site don't skew the field)
    and excludes the site being graded."""
    if not _ENABLED:
        return None
    try:
        # Peer distribution = latest grade per domain, excluding this domain.
        sql = (
            'SELECT overall FROM grades WHERE mode=? AND domain<>? AND id IN '
            '(SELECT MAX(id) FROM grades WHERE mode=? AND domain<>? GROUP BY domain)'
        )
        with _LOCK, _connect() as conn:
            rows = conn.execute(sql, (mode, domain, mode, domain)).fetchall()
        overalls = [r['overall'] for r in rows if r['overall'] is not None]
        n = len(overalls)
        if n < 8:  # not enough peers to be meaningful yet
            return {'sample': n, 'ready': False}
        below = sum(1 for o in overalls if o < score)
        pct = round(below / n * 100)
        avg = round(sum(overalls) / n)
        return {'sample': n, 'ready': True, 'percentile': pct, 'average': avg, 'beats_pct': pct}
    except Exception:
        return None


def history(domain: str, mode: str, limit: int = 24) -> List[Dict]:
    if not _ENABLED:
        return []
    try:
        with _LOCK, _connect() as conn:
            rows = conn.execute(
                'SELECT overall, grade, created_at FROM grades '
                'WHERE domain=? AND mode=? ORDER BY created_at DESC LIMIT ?',
                (domain, mode, limit)).fetchall()
        return [{'overall': r['overall'], 'grade': r['grade'], 'at': r['created_at']} for r in reversed(rows)]
    except Exception:
        return []


def url_graded(url: str, mode: str) -> bool:
    """Has this exact URL been graded? Gate for the billable /api/cwv PSI spawn.
    Degrades open (True) if the DB is unavailable."""
    if not _ENABLED:
        return True
    try:
        with _LOCK, _connect() as conn:
            r = conn.execute('SELECT 1 FROM grades WHERE url=? AND mode=? LIMIT 1',
                             (url, mode)).fetchone()
        return bool(r)
    except Exception:
        return True


def save_lead(email: str, url: str, mode: str, overall: Optional[int]) -> bool:
    if not _ENABLED:
        return False
    try:
        with _LOCK, _connect() as conn:
            conn.execute('INSERT INTO leads (email, url, mode, overall, created_at) VALUES (?,?,?,?,?)',
                         (email, url, mode, overall, time.time()))
        return True
    except Exception:
        return False


def add_monitor(email: str, url: str, mode: str) -> bool:
    if not _ENABLED:
        return False
    try:
        with _LOCK, _connect() as conn:
            conn.execute(
                'INSERT INTO monitors (email, url, mode, active, created_at) VALUES (?,?,?,1,?) '
                'ON CONFLICT(email, url, mode) DO UPDATE SET active=1',
                (email, url, mode, time.time()))
        return True
    except Exception:
        return False


def active_monitors() -> List[Dict]:
    if not _ENABLED:
        return []
    try:
        with _LOCK, _connect() as conn:
            rows = conn.execute(
                'SELECT id, email, url, mode FROM monitors WHERE active=1').fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def mark_monitor_run(monitor_id: int):
    if not _ENABLED:
        return
    try:
        with _LOCK, _connect() as conn:
            conn.execute('UPDATE monitors SET last_run=? WHERE id=?', (time.time(), monitor_id))
    except Exception:
        pass


def save_cwv(url: str, mode: str, cwv: Dict) -> bool:
    """Cache the last successful Core Web Vitals for a URL so a later timed-out
    PageSpeed run can fall back to it instead of showing nothing."""
    if not _ENABLED or not cwv:
        return False
    try:
        with _LOCK, _connect() as conn:
            conn.execute(
                'INSERT INTO cwv (url, mode, cwv_json, created_at) VALUES (?,?,?,?) '
                'ON CONFLICT(url, mode) DO UPDATE SET cwv_json=excluded.cwv_json, created_at=excluded.created_at',
                (url, mode, json.dumps(cwv), time.time()))
        return True
    except Exception:
        return False


def get_cwv(url: str, mode: str) -> Optional[Dict]:
    if not _ENABLED:
        return None
    try:
        with _LOCK, _connect() as conn:
            row = conn.execute('SELECT cwv_json, created_at FROM cwv WHERE url=? AND mode=?',
                               (url, mode)).fetchone()
        if not row:
            return None
        cwv = json.loads(row['cwv_json'])
        cwv['stale'] = True
        cwv['measured_at'] = row['created_at']
        return cwv
    except Exception:
        return None


def save_report(report_id: str, url: str, mode: str, report: Dict) -> bool:
    if not _ENABLED:
        return False
    try:
        with _LOCK, _connect() as conn:
            conn.execute(
                'INSERT INTO reports (id, url, mode, report_json, created_at) VALUES (?,?,?,?,?) '
                'ON CONFLICT(id) DO NOTHING',
                (report_id, url, mode, json.dumps(report), time.time()))
        return True
    except Exception:
        return False


def get_report(report_id: str) -> Optional[Dict]:
    if not _ENABLED:
        return None
    try:
        with _LOCK, _connect() as conn:
            row = conn.execute('SELECT report_json FROM reports WHERE id=?', (report_id,)).fetchone()
        return json.loads(row['report_json']) if row else None
    except Exception:
        return None


def last_overall(domain: str, mode: str, before_latest: bool = True) -> Optional[int]:
    """The previous overall score for a domain (second-most-recent), for digests."""
    if not _ENABLED:
        return None
    try:
        with _LOCK, _connect() as conn:
            rows = conn.execute(
                'SELECT overall FROM grades WHERE domain=? AND mode=? ORDER BY created_at DESC LIMIT 2',
                (domain, mode)).fetchall()
        if before_latest:
            return rows[1]['overall'] if len(rows) >= 2 else None
        return rows[0]['overall'] if rows else None
    except Exception:
        return None


def save_authority(domain: str, data: Dict) -> bool:
    """Cache off-site authority per domain so repeat grades / monitoring /
    competitor lookups don't re-charge the backlink provider."""
    if not _ENABLED or not data:
        return False
    try:
        with _LOCK, _connect() as conn:
            conn.execute(
                'INSERT INTO authority (domain, data_json, created_at) VALUES (?,?,?) '
                'ON CONFLICT(domain) DO UPDATE SET data_json=excluded.data_json, created_at=excluded.created_at',
                (domain, json.dumps(data), time.time()))
        return True
    except Exception:
        return False


def get_authority(domain: str, max_age_days: float = 7) -> Optional[Dict]:
    if not _ENABLED:
        return None
    try:
        with _LOCK, _connect() as conn:
            row = conn.execute('SELECT data_json, created_at FROM authority WHERE domain=?',
                               (domain,)).fetchone()
        if not row:
            return None
        if time.time() - row['created_at'] > max_age_days * 86400:
            return None  # stale — refetch
        return json.loads(row['data_json'])
    except Exception:
        return None


def save_outcome(domain: str, mode: str, metric: str, value: float) -> bool:
    """Record a real-world outcome (e.g. GSC clicks, GA4 sessions, applications)
    for a domain — used to calibrate pillar weights against reality."""
    if not _ENABLED:
        return False
    try:
        with _LOCK, _connect() as conn:
            conn.execute(
                'INSERT INTO outcomes (domain, mode, metric, value, created_at) VALUES (?,?,?,?,?) '
                'ON CONFLICT(domain, mode, metric) DO UPDATE SET value=excluded.value, created_at=excluded.created_at',
                (domain, mode, metric, float(value), time.time()))
        return True
    except Exception:
        return False


def calibration_rows(mode: str, metric: str) -> List[Dict]:
    """Latest pillar scores + the outcome value for every domain that has both —
    the dataset for calibrating weights against real traffic/conversions."""
    if not _ENABLED:
        return []
    try:
        with _LOCK, _connect() as conn:
            rows = conn.execute(
                'SELECT g.pillars_json pj, o.value val FROM grades g '
                'JOIN outcomes o ON o.domain=g.domain AND o.mode=g.mode '
                'WHERE g.mode=? AND o.metric=? AND g.id IN '
                '(SELECT MAX(id) FROM grades WHERE mode=? GROUP BY domain)',
                (mode, metric, mode)).fetchall()
        out = []
        for r in rows:
            try:
                out.append({'pillars': json.loads(r['pj']), 'outcome': r['val']})
            except Exception:
                continue
        return out
    except Exception:
        return []


def stats() -> Dict:
    if not _ENABLED:
        return {'enabled': False}
    try:
        with _LOCK, _connect() as conn:
            grades = conn.execute('SELECT COUNT(*) n FROM grades').fetchone()['n']
            leads = conn.execute('SELECT COUNT(*) n FROM leads').fetchone()['n']
            monitors = conn.execute('SELECT COUNT(*) n FROM monitors WHERE active=1').fetchone()['n']
            cwv = conn.execute('SELECT COUNT(*) n FROM cwv').fetchone()['n']
        return {'enabled': True, 'grades': grades, 'leads': leads, 'monitors': monitors,
                'cwv_cached': cwv, 'db': DB_PATH}
    except Exception:
        return {'enabled': False}
