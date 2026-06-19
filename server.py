import asyncio
import json
import os
import queue
import threading
import re
import time
import secrets
from collections import defaultdict, deque
from flask import Flask, request, Response, send_from_directory, jsonify
from grader import CareerSiteGrader
import db
import emailer

app = Flask(__name__, static_folder='public')
db.init_db()

VALID_MODES = ('recruitment', 'career_site', 'general')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL',
                                 'https://career-site-grader-production.up.railway.app')

# --- Simple in-memory per-IP rate limiting (per worker) ---
_rl = defaultdict(deque)
_rl_lock = threading.Lock()


def _client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    return fwd.split(',')[0].strip() if fwd else (request.remote_addr or 'unknown')


def _rate_ok(bucket: str, limit: int, window: int = 3600) -> bool:
    now = time.time()
    key = f'{bucket}:{_client_ip()}'
    with _rl_lock:
        dq = _rl[key]
        while dq and dq[0] < now - window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


def _report_link(report_id):
    return f'{PUBLIC_BASE_URL}/r/{report_id}' if report_id else PUBLIC_BASE_URL


def _pillar_scores(complete_event):
    return {k: v['score'] for k, v in complete_event.get('pillars', {}).items()}


def _persist_and_enrich(event, fallback_url, mode):
    """Save the grade, attach benchmark, and cache / restore Core Web Vitals.
    Best-effort: never raises into the grading path."""
    domain = event.get('domain', '')
    graded_url = event.get('url', fallback_url)
    try:
        prior = db.history(domain, mode)
        db.save_grade(graded_url, domain, mode, event.get('overall_score', 0),
                      event.get('grade', ''), _pillar_scores(event))
        bench = db.percentile(mode, event.get('overall_score', 0))
        if bench:
            event['benchmark'] = bench
        if prior:
            event['history'] = prior
    except Exception:
        pass
    # Core Web Vitals: cache a good result, or fall back to the last good one.
    try:
        cwv = event.get('core_web_vitals')
        if cwv and cwv.get('perf_score') is not None:
            db.save_cwv(graded_url, mode, cwv)
        elif event.get('cwv_attempted'):
            cached = db.get_cwv(graded_url, mode)
            if cached:
                event['core_web_vitals'] = cached
    except Exception:
        pass
    if not event.get('report_id'):
        event['report_id'] = secrets.token_urlsafe(8)
    return event


async def _grade_competitor(url: str, mode: str) -> dict:
    """Light grade (no PageSpeed) of a competitor — returns headline + pillar scores."""
    try:
        g = CareerSiteGrader(url, mode=mode, light=True)
        final = None
        async for ev in g.grade():
            if ev.get('type') == 'error':
                return {'url': url, 'domain': g.parsed.netloc, 'error': ev.get('message', 'failed')}
            if ev.get('type') == 'complete':
                final = ev
        if not final:
            return {'url': url, 'domain': g.parsed.netloc, 'error': 'no result'}
        return {
            'url': url,
            'domain': final['domain'],
            'overall_score': final['overall_score'],
            'grade': final['grade'],
            'pillars': {k: v['score'] for k, v in final['pillars'].items()},
            'authority': final.get('authority'),
        }
    except Exception as e:
        return {'url': url, 'error': str(e)}


async def _build_comparison(competitor_urls, mode, target_event) -> dict:
    results = await asyncio.gather(*[_grade_competitor(u, mode) for u in competitor_urls])
    target = {
        'domain': target_event['domain'],
        'overall_score': target_event['overall_score'],
        'grade': target_event['grade'],
        'pillars': {k: v['score'] for k, v in target_event['pillars'].items()},
        'authority': target_event.get('authority'),
    }
    scored = [r for r in results if 'overall_score' in r]
    rank = 1 + sum(1 for r in scored if r['overall_score'] > target['overall_score'])
    return {'target': target, 'competitors': results, 'rank': rank, 'field_size': len(scored) + 1}


def run_grader_in_thread(url: str, mode: str, competitors, q: queue.Queue):
    """Run the async grader in a background thread and push events to queue."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def collect():
        grader = CareerSiteGrader(url, mode=mode)
        async for event in grader.grade():
            if event.get('type') == 'complete':
                _persist_and_enrich(event, url, mode)
                if competitors:
                    q.put({'type': 'status', 'message': f'Benchmarking against {len(competitors)} competitor(s)...', 'progress': 99})
                    try:
                        event['comparison'] = await _build_comparison(competitors, mode, event)
                    except Exception:
                        pass
                try:
                    db.save_report(event['report_id'], event.get('url', url), mode, event)
                except Exception:
                    pass
            q.put(event)
        q.put(None)  # sentinel

    try:
        loop.run_until_complete(collect())
    except Exception as e:
        q.put({'type': 'error', 'message': str(e)})
        q.put(None)
    finally:
        loop.close()


@app.route('/')
def index():
    return send_from_directory('public', 'index.html')


@app.route('/grade')
def grade():
    url = (request.args.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL parameter required'}), 400

    mode = (request.args.get('mode') or '').strip()
    if mode not in VALID_MODES:
        return jsonify({'error': f'mode parameter required. Must be one of: {", ".join(VALID_MODES)}'}), 400

    if not _rate_ok('grade', limit=40):
        return jsonify({'error': 'Rate limit reached — please try again later.'}), 429

    # Optional competitor benchmarking (comma-separated URLs, max 3)
    raw_comp = (request.args.get('competitors') or '').strip()
    competitors = [c.strip() for c in raw_comp.split(',') if c.strip()][:3] if raw_comp else []

    def generate():
        q: queue.Queue = queue.Queue()
        t = threading.Thread(target=run_grader_in_thread, args=(url, mode, competitors, q), daemon=True)
        t.start()

        while True:
            try:
                item = q.get(timeout=120)
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Timeout waiting for analysis'})}\n\n"
                break

            if item is None:
                break

            yield f"data: {json.dumps(item)}\n\n"

        t.join(timeout=5)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Access-Control-Allow-Origin': '*',
        },
    )


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'Shazamme Career Site Grader'})


@app.route('/methodology')
def methodology():
    return send_from_directory('public', 'methodology.html')


@app.route('/lead', methods=['POST'])
def lead():
    """Capture an email for the full report and (optionally) monthly monitoring."""
    data = request.get_json(silent=True) or request.form
    email = (data.get('email') or '').strip().lower()
    url = (data.get('url') or '').strip()
    mode = (data.get('mode') or '').strip()
    overall = data.get('overall')
    want_monitor = str(data.get('monitor', '')).lower() in ('1', 'true', 'yes', 'on')
    if not _EMAIL_RE.match(email):
        return jsonify({'ok': False, 'error': 'Please enter a valid email address.'}), 400
    try:
        overall = int(overall) if overall is not None else None
    except (TypeError, ValueError):
        overall = None
    db.save_lead(email, url, mode, overall)
    monitoring = False
    if want_monitor and url and mode in VALID_MODES:
        monitoring = db.add_monitor(email, url, mode)

    # Email the report (best-effort; needs SENDGRID_API_KEY)
    emailed = False
    report_id = (data.get('report_id') or '').strip()
    grade = (data.get('grade') or '').strip()
    top_fixes = None
    if report_id:
        stored = db.get_report(report_id)
        if stored:
            overall = stored.get('overall_score', overall)
            grade = stored.get('grade', grade)
            es = stored.get('executive_summary') or {}
            top_fixes = es.get('top_opportunities')
    if emailer.enabled() and overall is not None:
        emailed = emailer.send_report_email(email, url, mode, overall, grade,
                                             _report_link(report_id), top_fixes)
    return jsonify({'ok': True, 'monitoring': monitoring, 'emailed': emailed})


@app.route('/api/history')
def api_history():
    domain = (request.args.get('domain') or '').strip()
    mode = (request.args.get('mode') or 'recruitment').strip()
    if not domain:
        return jsonify({'error': 'domain required'}), 400
    return jsonify({'domain': domain, 'mode': mode, 'history': db.history(domain, mode)})


_cwv_jobs = set()
_cwv_lock = threading.Lock()


def _measure_cwv_bg(url, mode):
    """Background Core Web Vitals measurement — runs the slow PageSpeed pass and
    caches the result so polling requests stay instant."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def run():
            g = CareerSiteGrader(url, mode=mode)
            g.psi_timeouts = (75, 55)
            await g._fetch_pagespeed()
            return g.pagespeed

        try:
            cwv = loop.run_until_complete(run())
        finally:
            loop.close()
        if cwv and cwv.get('perf_score') is not None:
            db.save_cwv(url, mode, cwv)
    except Exception:
        pass
    finally:
        with _cwv_lock:
            _cwv_jobs.discard((url, mode))


@app.route('/api/cwv')
def api_cwv():
    """Fast, poll-friendly Core Web Vitals. Returns cached data immediately if we
    have it; otherwise kicks off a background measurement and returns 'pending'
    so the browser can poll without holding a long connection open (which proxies
    and browsers cut off)."""
    url = (request.args.get('url') or '').strip()
    mode = (request.args.get('mode') or 'recruitment').strip()
    if not url:
        return jsonify({'error': 'url required'}), 400

    cached = db.get_cwv(url, mode)
    status = 'ready' if cached else 'pending'
    if not cached:
        key = (url, mode)
        with _cwv_lock:
            if key not in _cwv_jobs:
                _cwv_jobs.add(key)
                threading.Thread(target=_measure_cwv_bg, args=(url, mode), daemon=True).start()
    resp = jsonify({'status': status, 'core_web_vitals': cached})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


@app.route('/api/grade')
def api_grade():
    """Non-streaming JSON grade — for the Client Portal and integrations."""
    url = (request.args.get('url') or '').strip()
    mode = (request.args.get('mode') or 'recruitment').strip()
    if not url:
        return jsonify({'error': 'url required'}), 400
    if mode not in VALID_MODES:
        return jsonify({'error': f'mode must be one of: {", ".join(VALID_MODES)}'}), 400

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run():
        grader = CareerSiteGrader(url, mode=mode)
        final = None
        async for ev in grader.grade():
            if ev.get('type') == 'error':
                return {'error': ev.get('message', 'failed')}
            if ev.get('type') == 'complete':
                final = ev
        return final

    try:
        result = loop.run_until_complete(run())
    finally:
        loop.close()
    if not result or 'error' in (result or {}):
        return jsonify(result or {'error': 'no result'}), 502
    _persist_and_enrich(result, url, mode)
    try:
        db.save_report(result['report_id'], result.get('url', url), mode, result)
    except Exception:
        pass
    resp = jsonify(result)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


@app.route('/api/report/<report_id>')
def api_report(report_id):
    report = db.get_report(report_id)
    if not report:
        return jsonify({'error': 'not found'}), 404
    resp = jsonify(report)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


@app.route('/r/<report_id>')
def shared_report(report_id):
    # Serves the SPA; the front-end detects /r/<id> and loads the stored report.
    return send_from_directory('public', 'index.html')


@app.route('/cron/rescore')
def cron_rescore():
    """Re-grade all active monitor subscriptions. Protect with ?token=CRON_TOKEN."""
    token = (request.args.get('token') or '').strip()
    expected = os.environ.get('CRON_TOKEN', '')
    if not expected or token != expected:
        return jsonify({'error': 'unauthorized'}), 401

    monitors = db.active_monitors()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def rescore_all():
        out = []
        for m in monitors:
            try:
                domain = ''
                g = CareerSiteGrader(m['url'], mode=m['mode'], light=True)
                final = None
                async for ev in g.grade():
                    if ev.get('type') == 'complete':
                        final = ev
                if final:
                    domain = final.get('domain', '')
                    previous = db.last_overall(domain, m['mode'], before_latest=False)
                    db.save_grade(final.get('url', m['url']), domain, m['mode'],
                                  final.get('overall_score', 0), final.get('grade', ''),
                                  _pillar_scores(final))
                    db.mark_monitor_run(m['id'])
                    if emailer.enabled():
                        emailer.send_monitor_digest(
                            m['email'], m['url'], final.get('overall_score', 0),
                            final.get('grade', ''), previous, _report_link(None))
                    out.append({'url': m['url'], 'overall': final.get('overall_score')})
            except Exception as e:
                out.append({'url': m['url'], 'error': str(e)})
        return out

    try:
        results = loop.run_until_complete(rescore_all())
    finally:
        loop.close()
    return jsonify({'rescored': len(results), 'results': results})


SEED_SITES = [
    'roberthalf.com', 'hays.com', 'michaelpage.com', 'adecco.com', 'randstad.com',
    'manpower.com', 'kellyservices.com', 'roberthalf.co.uk', 'reed.co.uk', 'morganmckinley.com',
    'hudson.com', 'gartner.com/en/careers', 'pagepersonnel.co.uk', 'sthree.com', 'roberthalf.com.au',
    'hays.com.au', 'seek.com.au', 'allegisgroup.com', 'kornferry.com', 'spencerstuart.com',
    'aerotek.com', 'teksystems.com', 'experis.com', 'roberthalf.de', 'gigroom.com',
]


def _seed_bg(mode):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def seed():
        for site in SEED_SITES:
            try:
                g = CareerSiteGrader(site, mode=mode, light=True)
                final = None
                async for ev in g.grade():
                    if ev.get('type') == 'complete':
                        final = ev
                if final:
                    db.save_grade(final.get('url', site), final.get('domain', ''), mode,
                                  final.get('overall_score', 0), final.get('grade', ''),
                                  _pillar_scores(final))
            except Exception:
                pass
    try:
        loop.run_until_complete(seed())
    finally:
        loop.close()


@app.route('/cron/seed')
def cron_seed():
    """Populate the benchmark dataset by light-grading a curated list so
    percentile benchmarking becomes meaningful. Runs in the background (25 sites
    take several minutes). Protect with ?token=CRON_TOKEN."""
    token = (request.args.get('token') or '').strip()
    expected = os.environ.get('CRON_TOKEN', '')
    if not expected or token != expected:
        return jsonify({'error': 'unauthorized'}), 401
    mode = (request.args.get('mode') or 'recruitment').strip()
    threading.Thread(target=_seed_bg, args=(mode,), daemon=True).start()
    return jsonify({'started': True, 'sites': len(SEED_SITES), 'mode': mode})


@app.route('/stats')
def stats():
    return jsonify(db.stats())


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7070))
    print(f"Shazamme Career Site Grader running on http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
