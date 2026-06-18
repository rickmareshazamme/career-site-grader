import asyncio
import json
import os
import queue
import threading
from flask import Flask, request, Response, send_from_directory, jsonify
from grader import CareerSiteGrader

app = Flask(__name__, static_folder='public')


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
            if event.get('type') == 'complete' and competitors:
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

    VALID_MODES = ('recruitment', 'career_site', 'general')
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7070))
    print(f"Shazamme Career Site Grader running on http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
