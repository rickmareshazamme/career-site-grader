import asyncio
import json
import os
import queue
import threading
from flask import Flask, request, Response, send_from_directory, jsonify
from grader import CareerSiteGrader

app = Flask(__name__, static_folder='public')


def run_grader_in_thread(url: str, q: queue.Queue):
    """Run the async grader in a background thread and push events to queue."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def collect():
        grader = CareerSiteGrader(url)
        async for event in grader.grade():
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

    def generate():
        q: queue.Queue = queue.Queue()
        t = threading.Thread(target=run_grader_in_thread, args=(url, q), daemon=True)
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
