import asyncio
import json
import os
import queue
import threading
import re
from flask import Flask, request, Response, send_from_directory, jsonify
from grader import CareerSiteGrader
import db

app = Flask(__name__, static_folder='public')
db.init_db()

VALID_MODES = ('recruitment', 'career_site', 'general')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _pillar_scores(complete_event):
    return {k: v['score'] for k, v in complete_event.get('pillars', {}).items()}


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
                # Persist + benchmark + history (best-effort; never blocks grading)
                try:
                    domain = event.get('domain', '')
                    prior = db.history(domain, mode)
                    db.save_grade(event.get('url', url), domain, mode,
                                  event.get('overall_score', 0), event.get('grade', ''),
                                  _pillar_scores(event))
                    bench = db.percentile(mode, event.get('overall_score', 0))
                    if bench:
                        event['benchmark'] = bench
                    if prior:
                        event['history'] = prior
                except Exception:
                    pass
                if competitors:
                    q.put({'type': 'status', 'message': f'Benchmarking against {len(competitors)} competitor(s)...', 'progress': 99})
                    try:
                        event['comparison'] = await _build_comparison(competitors, mode, event)
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

    # Optional competitor benchmarking (comma-separated URLs, max 3)
    raw_comp = (request.args.get('competitors') or '').strip()
    competitors = [c.strip() for c in raw_comp.split(',') if c.strip()][:3] if raw_comp else []

    def generate():
        q: queue.Queue = queue.Queue()
        t = threading.Thread(target=run_grader_in_thread, args=(url, mode, competitors, q), daemon=True)
        t.start()

        while True:
            try:
                item = q.get(timeout=60)
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
    return jsonify({'ok': True, 'monitoring': monitoring})


@app.route('/api/history')
def api_history():
    domain = (request.args.get('domain') or '').strip()
    mode = (request.args.get('mode') or 'recruitment').strip()
    if not domain:
        return jsonify({'error': 'domain required'}), 400
    return jsonify({'domain': domain, 'mode': mode, 'history': db.history(domain, mode)})


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
    try:
        db.save_grade(result.get('url', url), result.get('domain', ''), mode,
                      result.get('overall_score', 0), result.get('grade', ''),
                      _pillar_scores(result))
        bench = db.percentile(mode, result.get('overall_score', 0))
        if bench:
            result['benchmark'] = bench
    except Exception:
        pass
    resp = jsonify(result)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


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
                g = CareerSiteGrader(m['url'], mode=m['mode'], light=True)
                final = None
                async for ev in g.grade():
                    if ev.get('type') == 'complete':
                        final = ev
                if final:
                    db.save_grade(final.get('url', m['url']), final.get('domain', ''), m['mode'],
                                  final.get('overall_score', 0), final.get('grade', ''),
                                  _pillar_scores(final))
                    db.mark_monitor_run(m['id'])
                    out.append({'url': m['url'], 'overall': final.get('overall_score')})
            except Exception as e:
                out.append({'url': m['url'], 'error': str(e)})
        return out

    try:
        results = loop.run_until_complete(rescore_all())
    finally:
        loop.close()
    return jsonify({'rescored': len(results), 'results': results})


@app.route('/stats')
def stats():
    return jsonify(db.stats())


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7070))
    print(f"Shazamme Career Site Grader running on http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
