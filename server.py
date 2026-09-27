#!/usr/bin/env python3
"""
PVHS Dashboard Server
- Firebase Auth (Google sign-in) verification
- Canvas LMS API proxy (GET/POST/PUT/DELETE)
- Batch operations (grades, comments)
- Static file serving
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse
import http.client
import ssl
import json
import os
import sys
import mimetypes
import time
import base64
import gzip

sys.stdout.reconfigure(line_buffering=True)

PORT         = int(os.environ.get('PORT', 8080))
CANVAS_HOST  = 'smjuhsd.instructure.com'
CANVAS_TOKEN = os.environ.get('CANVAS_TOKEN', '').strip()
API_KEY      = os.environ.get('API_KEY', '')
SERVE_DIR    = os.path.dirname(os.path.abspath(__file__))

FIREBASE_PROJECT_ID = 'girl-scouts-silva'
ALLOWED_EMAILS = [
    e.strip().lower()
    for e in os.environ.get('ALLOWED_EMAILS', '').split(',')
    if e.strip()
]

# Auto-load roster data from ROSTER_DATA env var (base64+gzip compressed)
_roster_env = os.environ.get('ROSTER_DATA', '')
if _roster_env:
    _roster_path = os.path.join(SERVE_DIR, 'roster_data.json')
    if not os.path.exists(_roster_path):
        try:
            raw = gzip.decompress(base64.b64decode(_roster_env))
            with open(_roster_path, 'wb') as f:
                f.write(raw)
            data = json.loads(raw)
            print(f'[roster] Auto-loaded from env: {len(data["students"])} students, {len(data["sections"])} sections')
        except Exception as e:
            print(f'[roster] Failed to load from env: {e}')

# ---------------------------------------------------------------------------
# Firebase ID-token verification (lightweight, no Admin SDK needed)
# ---------------------------------------------------------------------------

_cert_cache = {'data': None, 'exp': 0}

def _fetch_google_certs():
    """Fetch Google's public certs for Firebase token verification, cached."""
    import urllib.request as req
    now = time.time()
    if _cert_cache['data'] and now < _cert_cache['exp']:
        return _cert_cache['data']
    url = ('https://www.googleapis.com/robot/v1/metadata/x509/'
           'securetoken@system.gserviceaccount.com')
    with req.urlopen(req.Request(url), timeout=10) as r:
        _cert_cache['data'] = json.loads(r.read())
        _cert_cache['exp'] = now + 3600
    return _cert_cache['data']


def verify_firebase_token(id_token_str):
    """Verify a Firebase ID token. Returns decoded claims or None."""
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as g_requests
        claims = id_token.verify_firebase_token(
            id_token_str,
            g_requests.Request(),
            audience=FIREBASE_PROJECT_ID,
        )
        return claims
    except Exception as e:
        print(f'[auth] Token verification failed: {e}')
        return None


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    # -- Auth helpers -------------------------------------------------------

    def check_auth(self):
        """Verify the request is authenticated. Returns email or None."""
        # API-key auth (server-to-server, e.g. curriculum catalog)
        key = self.headers.get('X-API-Key', '')
        if API_KEY and key == API_KEY:
            return 'api-key'

        # Firebase ID token auth (browser)
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return None
        token = auth[7:]
        claims = verify_firebase_token(token)
        if not claims:
            return None
        email = (claims.get('email') or '').lower()
        if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
            print(f'[auth] Email not allowed: {email}')
            return None
        return email

    def require_auth(self):
        """Check auth; send 401 and return False if unauthenticated."""
        email = self.check_auth()
        if not email:
            self.json_response(401, {'error': 'Unauthorized'})
            return False
        return True

    # -- Routing ------------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith('/api/v1/'):
            if not self.require_auth():
                return
            self.proxy_canvas('GET')
        elif path == '/api/status':
            self.handle_status()
        elif path == '/api/roster':
            if not self.require_auth():
                return
            self.handle_roster()
        elif path in ('/', ''):
            self.path = '/index.html'
            self.serve_file()
        else:
            self.serve_file()

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.require_auth():
            return
        if path == '/api/auth/verify':
            self.handle_auth_verify()
        elif path == '/api/roster/upload':
            self.handle_roster_upload()
        elif path == '/api/batch/grades':
            self.handle_batch_grades()
        elif path == '/api/batch/comments':
            self.handle_bulk_comments()
        elif path.startswith('/api/v1/'):
            self.proxy_canvas('POST')
        else:
            self.send_error(404)

    def do_PUT(self):
        if not self.require_auth():
            return
        if urlparse(self.path).path.startswith('/api/v1/'):
            self.proxy_canvas('PUT')
        else:
            self.send_error(404)

    def do_DELETE(self):
        if not self.require_auth():
            return
        if urlparse(self.path).path.startswith('/api/v1/'):
            self.proxy_canvas('DELETE')
        else:
            self.send_error(404)

    # -- Auth verify --------------------------------------------------------

    def handle_auth_verify(self):
        """Return user info after successful auth check."""
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            self.json_response(401, {'error': 'No token'})
            return
        claims = verify_firebase_token(auth[7:])
        if not claims:
            self.json_response(401, {'error': 'Invalid token'})
            return
        email = (claims.get('email') or '').lower()
        if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
            self.json_response(403, {'error': 'Email not authorized'})
            return
        self.json_response(200, {
            'email': email,
            'name': claims.get('name', ''),
            'picture': claims.get('picture', ''),
            'uid': claims.get('sub', ''),
            'canvas_connected': bool(CANVAS_TOKEN),
        })

    # -- Status (unauthenticated) -------------------------------------------

    def handle_status(self):
        self.json_response(200, {
            'ok': True,
            'canvas_host': CANVAS_HOST,
            'canvas_connected': bool(CANVAS_TOKEN),
        })

    # -- Roster data (authenticated) ----------------------------------------

    def handle_roster(self):
        roster_path = os.path.join(SERVE_DIR, 'roster_data.json')
        if not os.path.exists(roster_path):
            self.json_response(404, {'error': 'No roster data uploaded'})
            return
        with open(roster_path, 'r') as f:
            data = json.load(f)
        self.json_response(200, data)

    def handle_roster_upload(self):
        length = int(self.headers.get('Content-Length', 0))
        if length > 5_000_000:
            self.json_response(413, {'error': 'Roster data too large'})
            return
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.json_response(400, {'error': 'Invalid JSON'})
            return
        if 'students' not in data or 'sections' not in data:
            self.json_response(400, {'error': 'Missing students or sections'})
            return
        roster_path = os.path.join(SERVE_DIR, 'roster_data.json')
        with open(roster_path, 'w') as f:
            json.dump(data, f)
        print(f'[roster] Uploaded: {len(data["students"])} students, {len(data["sections"])} sections')
        self.json_response(200, {
            'ok': True,
            'students': len(data['students']),
            'sections': len(data['sections']),
        })

    # -- Canvas proxy -------------------------------------------------------

    def proxy_canvas(self, method):
        """Proxy a request to Canvas, injecting the server-side token."""
        if not CANVAS_TOKEN:
            self.json_response(503, {'error': 'Canvas token not configured'})
            return

        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(CANVAS_HOST, context=ctx)
        headers = {
            'Authorization': f'Bearer {CANVAS_TOKEN}',
            'User-Agent': 'PVHS-Dashboard/2.0',
        }

        body = None
        if method in ('POST', 'PUT'):
            length = int(self.headers.get('Content-Length', 0))
            if length > 0:
                body = self.rfile.read(length)
            ct = self.headers.get('Content-Type', '')
            if ct:
                headers['Content-Type'] = ct

        try:
            conn.request(method, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            if resp.status >= 400:
                print(f'[canvas] {method} {self.path} -> {resp.status}: {resp_body[:200]}')

            self.send_response(resp.status)
            ct = resp.getheader('Content-Type', 'application/json')
            self.send_header('Content-Type', ct)

            link = resp.getheader('Link', '')
            if link:
                link = link.replace(f'https://{CANVAS_HOST}', '')
                self.send_header('Link', link)

            self._cors_headers()
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', len(resp_body))
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            self.json_response(502, {'error': str(e)})
        finally:
            conn.close()

    # -- Batch grade --------------------------------------------------------

    def handle_batch_grades(self):
        """Apply grades to multiple students for one assignment.

        Expects JSON body:
        {
          "course_id": "12345",
          "assignment_id": "67890",
          "grades": [
            {"student_id": "111", "score": 85, "comment": "Good work"},
            ...
          ]
        }
        """
        data = self._read_json_body()
        if data is None:
            return

        course_id = str(data.get('course_id', ''))
        assignment_id = str(data.get('assignment_id', ''))
        grades = data.get('grades', [])

        if not all([course_id, assignment_id, grades]):
            self.json_response(400, {'error': 'Missing course_id, assignment_id, or grades'})
            return

        results = []
        for g in grades:
            sid = str(g.get('student_id', ''))
            score = g.get('score')
            comment = g.get('comment', '')

            payload = {}
            if score is not None:
                payload['submission'] = {'posted_grade': str(score)}
            if comment:
                payload['comment'] = {'text_comment': comment}
            if not payload:
                results.append({'student_id': sid, 'ok': False, 'error': 'No score or comment'})
                continue

            resp = self._canvas_put(
                f'/api/v1/courses/{course_id}/assignments/{assignment_id}'
                f'/submissions/{sid}',
                payload,
            )
            results.append({
                'student_id': sid,
                'ok': resp is not None and isinstance(resp, dict),
                'grade': resp.get('grade') if isinstance(resp, dict) else None,
            })

        self.json_response(200, {'results': results})

    # -- Bulk comments ------------------------------------------------------

    def handle_bulk_comments(self):
        """Post the same comment to multiple students for one assignment.

        Expects JSON body:
        {
          "course_id": "12345",
          "assignment_id": "67890",
          "student_ids": ["111", "222", ...],
          "comment": "Please turn this in."
        }
        """
        data = self._read_json_body()
        if data is None:
            return

        course_id = str(data.get('course_id', ''))
        assignment_id = str(data.get('assignment_id', ''))
        student_ids = data.get('student_ids', [])
        comment = data.get('comment', '')

        if not all([course_id, assignment_id, student_ids, comment]):
            self.json_response(400, {'error': 'Missing required fields'})
            return

        results = []
        for sid in student_ids:
            sid = str(sid)
            payload = {'comment': {'text_comment': comment}}
            resp = self._canvas_put(
                f'/api/v1/courses/{course_id}/assignments/{assignment_id}'
                f'/submissions/{sid}',
                payload,
            )
            results.append({
                'student_id': sid,
                'ok': resp is not None,
            })

        self.json_response(200, {
            'comment': comment,
            'total': len(student_ids),
            'succeeded': sum(1 for r in results if r['ok']),
            'results': results,
        })

    # -- Canvas helpers -----------------------------------------------------

    def _canvas_put(self, path, payload):
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(CANVAS_HOST, context=ctx)
        headers = {
            'Authorization': f'Bearer {CANVAS_TOKEN}',
            'Content-Type': 'application/json',
            'User-Agent': 'PVHS-Dashboard/2.0',
        }
        body = json.dumps(payload).encode('utf-8')
        try:
            conn.request('PUT', path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                print(f'[canvas] PUT {path} -> {resp.status}: {raw[:300]}')
            return json.loads(raw)
        except Exception as e:
            print(f'[canvas] PUT error: {e}')
            return None
        finally:
            conn.close()

    # -- File server --------------------------------------------------------

    def serve_file(self):
        path = urlparse(self.path).path.lstrip('/')
        if '..' in path:
            self.send_error(403)
            return
        filepath = os.path.join(SERVE_DIR, path)
        if not os.path.isfile(filepath):
            self.send_error(404)
            return
        mime, _ = mimetypes.guess_type(filepath)
        if mime is None:
            mime = 'application/octet-stream'
        with open(filepath, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', len(body))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    # -- Utilities ----------------------------------------------------------

    def _read_json_body(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            return json.loads(self.rfile.read(length))
        except Exception as e:
            self.json_response(400, {'error': f'Invalid JSON: {e}'})
            return None

    def json_response(self, status, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self._cors_headers()
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                         'Authorization, Content-Type, X-API-Key')
        self.send_header('Access-Control-Max-Age', '86400')

    def log_message(self, fmt, *args):
        msg = fmt % args
        if '/api/' in msg or '/batch/' in msg or '/auth/' in msg:
            print(f'  [server] {msg}')


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == '__main__':
    print(f'PVHS Dashboard Server v2.0 on port {PORT}')
    print(f'  Canvas host: {CANVAS_HOST}')
    print(f'  Canvas token: {"configured (" + str(len(CANVAS_TOKEN)) + " chars)" if CANVAS_TOKEN else "NOT SET"}')
    print(f'  Allowed emails: {ALLOWED_EMAILS or "(any authenticated user)"}')
    print(f'  API key: {"configured" if API_KEY else "not set"}')
    if CANVAS_TOKEN:
        import urllib.request
        try:
            rq = urllib.request.Request(
                f'https://{CANVAS_HOST}/api/v1/users/self',
                headers={
                    'Authorization': f'Bearer {CANVAS_TOKEN}',
                    'User-Agent': 'PVHS-Dashboard/2.0',
                },
            )
            with urllib.request.urlopen(rq, timeout=10) as r:
                print(f'  Canvas token test: OK ({r.status})')
        except Exception as e:
            print(f'  Canvas token test: FAILED ({e})')
    server = ThreadedHTTPServer(('0.0.0.0', PORT), Handler)
    server.serve_forever()
