#!/usr/bin/env python3
"""
PVHS Canvas Missing Assignments Dashboard -- proxy server.
Serves the dashboard HTML and proxies Canvas API requests to avoid CORS.
Includes POST /grade endpoint for AI-assisted rubric grading via Gemini.
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse
import http.client
import ssl
import json
import os
import mimetypes
import base64
import urllib.request
import urllib.error
import time

PORT        = int(os.environ.get('PORT', 8080))
CANVAS_HOST = 'smjuhsd.instructure.com'
GEMINI_KEY  = os.environ.get('GEMINI_API_KEY', '')
COURSE_ID   = '111811'
SERVE_DIR   = os.path.dirname(os.path.abspath(__file__))

GEMINI_MODEL = 'gemini-2.5-flash'

def gemini_url():
    return (
        'https://generativelanguage.googleapis.com/v1beta/models/'
        + GEMINI_MODEL + ':generateContent?key=' + GEMINI_KEY
)

def gemini_list_url():
    return 'https://generativelanguage.googleapis.com/v1beta/models?key=' + GEMINI_KEY

# Teacher voice -- English
VOICE_EN = (
    "You are writing grading feedback on behalf of Mr. Silva, "
    "an experienced professional photographer and educator at Pioneer Valley High School. "
    "Write in his voice: firm, motivational, and friendly. He is passionate about the art of photography. "
    "Use direct, clear language at a 5th grade reading level. "
    "Be specific about what you see in the student's submitted images. "
    "Praise what works. Explain what needs improvement and why it matters as a photographer. "
    "Sound like a real teacher wrote this, not a form letter. Vary your sentence structure. "
    "Never use em dashes under any circumstances."
)

# Teacher voice -- Spanish (full Spanish for flagged students)
VOICE_ES = (
    "Eres el asistente de calificacion del senor Silva, "
    "un fotografo y educador profesional con mucha experiencia en Pioneer Valley High School. "
    "Escribe todo el comentario en espanol usando el tuteo. "
    "Su tono es firme, motivador y amigable. Es apasionado por el arte de la fotografia. "
    "Usa lenguaje directo y claro, facil de entender para un estudiante de preparatoria. "
    "Se especifico sobre lo que ves en las imagenes enviadas por el estudiante. "
    "Felicita lo que funciona bien. Explica lo que necesita mejorar y por que importa como fotografo. "
    "Escribe como un maestro real, no como una carta generica. Varia la estructura de las oraciones. "
    "Nunca uses guiones largos bajo ninguna circunstancia."
)


def build_criteria_block(criteria):
    """Convert rubric criteria list to a readable text block for the prompt."""
    lines = []
    for c in criteria:
        lines.append(f'Criterion ID: {c["id"]}')
        lines.append(f'  Name: {c["description"]}')
        lines.append(f'  Max Points: {c["points"]}')
        lines.append('  Ratings:')
        for r in c.get('ratings', []):
            desc = r.get('long_description') or r.get('description', '')
            lines.append(f'    {r["description"]} ({r["points"]} pts): {desc}')
        lines.append('')
    return '\n'.join(lines)


class Handler(BaseHTTPRequestHandler):

    # ── Routing ──────────────────────────────────────────────────────────────

    def do_POST(self):
        if self.path == '/grade':
            self.handle_grade()
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path == '/list-models':
            self.handle_list_models()
        elif self.path.startswith('/api/'):
            self.proxy_to_canvas()
        elif self.path in ('/', ''):
            self.path = '/index.html'
            self.serve_file()
        else:
            self.serve_file()

    def handle_list_models(self):
        try:
            req = urllib.request.Request(gemini_list_url(), headers={'User-Agent': 'PVHS/1.0'})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            names = [m.get('name','') for m in data.get('models', [])]
            self.json_response(200, {'models': names, 'key_prefix': GEMINI_KEY[:8] + '...'})
        except urllib.error.HTTPError as e:
            self.json_response(e.code, {'error': e.read().decode('utf-8', errors='replace')})
        except Exception as e:
            self.json_error(500, str(e))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Max-Age', '86400')
        self.end_headers()

    # ── Grade Endpoint ────────────────────────────────────────────────────────

    def handle_grade(self):
        self._last_gemini_error = None
        try:
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length)
            data   = json.loads(body)

            assignment_id = str(data.get('assignment_id', ''))
            student_id    = str(data.get('student_id', ''))
            student_name  = data.get('student_name', 'Student')
            rubric_id     = str(data.get('rubric_id', ''))
            spanish       = bool(data.get('spanish', False))
            auth          = self.headers.get('Authorization', '')

            if not all([assignment_id, student_id, rubric_id, auth]):
                self.json_error(400, 'Missing required fields')
                return

            if not GEMINI_KEY:
                self.json_error(503, 'Gemini API key not configured')
                return

            # 1. Fetch rubric
            rubric = self.canvas_get(
                f'/courses/{COURSE_ID}/rubrics/{rubric_id}', auth
            )
            # Canvas returns criteria under 'data' key on rubric objects
            criteria = None
            if isinstance(rubric, dict):
                criteria = rubric.get('data') or rubric.get('criteria')
            if not criteria:
                print(f'[grade] Rubric response keys: {list(rubric.keys()) if isinstance(rubric, dict) else type(rubric)}')
                self.json_error(500, 'Could not fetch rubric criteria')
                return

            # 2. Fetch submission
            submission = self.canvas_get(
                f'/courses/{COURSE_ID}/assignments/{assignment_id}'
                f'/submissions/{student_id}?include[]=submission_comments',
                auth
            )

            attachments    = submission.get('attachments', []) or []
            workflow_state = submission.get('workflow_state', 'unsubmitted')
            is_missing     = (workflow_state == 'unsubmitted') or not attachments

            if is_missing:
                result = self.generate_missing_comment(
                    student_name, student_id, submission, spanish
                )
            else:
                images = self.fetch_images(attachments, auth)
                if not images:
                    self.json_error(500, 'Could not download submission images')
                    return
                result = self.grade_submission(
                    student_name, images, criteria, spanish
                )

            if result is None:
                err = getattr(self, '_last_gemini_error', 'Gemini grading failed')
                self.json_error(500, err)
                return

            # 3. Post back to Canvas
            canvas_resp = self.post_to_canvas(assignment_id, student_id, result, criteria, auth)

            self.json_response(200, {
                'success': True,
                'missing': is_missing,
                'overall_comment': result.get('overall_comment', ''),
                'total_score': result.get('total_score', 0),
                'canvas_posted': canvas_resp is not None,
                'canvas_grade': canvas_resp.get('grade') if isinstance(canvas_resp, dict) else None
            })

        except Exception as e:
            print(f'[grade] Unhandled error: {e}')
            import traceback; traceback.print_exc()
            self.json_error(500, str(e))

    # ── Gemini: Grade Submitted Work ─────────────────────────────────────────

    def grade_submission(self, student_name, images, criteria, spanish):
        voice         = VOICE_ES if spanish else VOICE_EN
        criteria_text = build_criteria_block(criteria)
        lang_note     = (
            'Respond ENTIRELY in Spanish using tú. No English at all.'
            if spanish else
            'Respond in English.'
        )

        # Build criterion ID list so Gemini uses exact IDs
        crit_ids = [c['id'] for c in criteria]
        crit_ids_json = json.dumps(crit_ids)

        prompt = f"""{voice}

STUDENT NAME: {student_name}
ASSIGNMENT: 18 - Editing & Final Contact Sheet

WHAT THE STUDENT SUBMITTED:
Two 6-up contact sheet JPG pages exported from Lightroom at 300 ppi.
Photos 1 through 6 = Composition focus.
Photos 7 through 12 = Camera Control focus.
Each photo should have camera settings (ISO, shutter, aperture) labeled underneath.

RUBRIC CRITERIA (use these EXACT criterion IDs in your response):
{criteria_text}

CRITERION IDs TO USE: {crit_ids_json}

{lang_note}

Look carefully at both contact sheet pages. Score each criterion based on what you actually see.
Write 2 to 3 sentences of specific feedback per criterion referencing what is visible in the work.
Write a 3 to 5 sentence overall comment to the student.

Return ONLY valid JSON in this exact structure, nothing else:
{{
  "scores": {{
    "{crit_ids[0] if crit_ids else 'c1'}": {{"points": 0, "comment": "..."}},
    "{crit_ids[1] if len(crit_ids) > 1 else 'c2'}": {{"points": 0, "comment": "..."}},
    "{crit_ids[2] if len(crit_ids) > 2 else 'c3'}": {{"points": 0, "comment": "..."}},
    "{crit_ids[3] if len(crit_ids) > 3 else 'c4'}": {{"points": 0, "comment": "..."}}
  }},
  "overall_comment": "..."
}}"""

        parts = [{"text": prompt}]
        for img in images:
            parts.append({
                "inline_data": {
                    "mime_type": img['mime'],
                    "data": img['b64']
                }
            })

        return self.call_gemini(parts, temperature=0.7, is_json=True)

    # ── Gemini: Missing / No Submission ──────────────────────────────────────

    def generate_missing_comment(self, student_name, student_id, submission, spanish):
        voice     = VOICE_ES if spanish else VOICE_EN
        lang_note = (
            'Write ENTIRELY in Spanish using tú. No English at all.'
            if spanish else
            'Write in English.'
        )

        prompt = f"""{voice}

STUDENT NAME: {student_name}
ASSIGNMENT: 18 - Editing & Final Contact Sheet
STATUS: This student has not submitted this assignment.
DUE DATE: February 25 at 10 PM. This assignment is now past due.

LATE POINT POLICY for Timeliness of Submission criterion (out of 8 pts):
On Time: 8 pts
One Day Late: 6 pts
A Few Days Late (2 to 3 days): 4 pts
Several Days Late (4 to 7 days): 2 pts
Several Weeks Late or Not Submitted: 1 pt

{lang_note}

Write a comment directly to the student. The comment should:
1. Acknowledge the assignment is missing
2. Tell them they can still earn partial credit if they submit now
3. Mention specifically what they can still earn on the timeliness score if they turn it in soon
4. Encourage them to complete and submit the work because their photography matters
5. Be firm but supportive, not scolding

Keep it to 4 sentences. Make it feel personal and specific to this assignment, not a copied template.
Return only the comment text, no JSON."""

        parts = [{"text": prompt}]
        result_text = self.call_gemini(parts, temperature=0.9, is_json=False)
        if result_text is None:
            return None
        return {
            'missing': True,
            'overall_comment': result_text,
            'scores': {},
            'total_score': 0
        }

    # ── Gemini HTTP Call ─────────────────────────────────────────────────────

    def call_gemini(self, parts, temperature=0.7, is_json=False):
        gen_config = {
            "temperature": temperature,
            "maxOutputTokens": 2048
        }
        if is_json:
            gen_config["responseMimeType"] = "application/json"
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": gen_config
        }
        req_body = json.dumps(payload).encode('utf-8')

        # Retry up to 3 times with backoff for 429 rate limit errors
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    gemini_url(),
                    data=req_body,
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                with urllib.request.urlopen(req, timeout=90) as r:
                    response = json.loads(r.read())

                text = response['candidates'][0]['content']['parts'][0]['text'].strip()

                if not is_json:
                    return text

                # Extract JSON robustly
                import re
                # Strip markdown fences
                text = re.sub(r'```[a-z]*\n?', '', text)
                text = re.sub(r'\n?```', '', text)
                # Find the outermost JSON object
                start = text.find('{')
                end   = text.rfind('}')
                if start != -1 and end != -1:
                    text = text[start:end+1]
                text = text.strip()
                # Fix literal newlines/tabs inside JSON string values
                # which cause "Unterminated string" errors
                def fix_json_strings(s):
                    result = []
                    in_string = False
                    escape = False
                    for ch in s:
                        if escape:
                            result.append(ch)
                            escape = False
                        elif ch == '\\':
                            result.append(ch)
                            escape = True
                        elif ch == '"':
                            in_string = not in_string
                            result.append(ch)
                        elif in_string and ch == '\n':
                            result.append('\\n')
                        elif in_string and ch == '\r':
                            result.append('\\r')
                        elif in_string and ch == '\t':
                            result.append('\\t')
                        else:
                            result.append(ch)
                    return ''.join(result)
                text = fix_json_strings(text)

                parsed = json.loads(text, strict=False)
                total = sum(
                    v.get('points', 0)
                    for v in parsed.get('scores', {}).values()
                )
                parsed['total_score'] = total
                return parsed

            except urllib.error.HTTPError as e:
                body = e.read().decode('utf-8', errors='replace')
                print(f'[gemini] HTTP {e.code} (attempt {attempt+1}): {body[:300]}')
                if e.code == 429 and attempt < 2:
                    wait = (attempt + 1) * 20  # 20s, then 40s
                    print(f'[gemini] Rate limited -- waiting {wait}s before retry')
                    time.sleep(wait)
                    continue
                self._last_gemini_error = f'Gemini HTTP {e.code}: {body[:200]}'
                return None
            except Exception as e:
                print(f'[gemini] Error: {e}')
                import traceback; traceback.print_exc()
                self._last_gemini_error = str(e)
                return None

        self._last_gemini_error = 'Gemini rate limit -- quota exceeded, try again later'
        return None

    # ── Canvas: Post Grade + Rubric + Comment ─────────────────────────────────

    def post_to_canvas(self, assignment_id, student_id, result, criteria, auth):
        if result.get('missing'):
            payload = {
                'comment': {'text_comment': result['overall_comment']}
            }
        else:
            rubric_assessment = {}
            for c in criteria:
                cid        = c['id']
                score_data = result.get('scores', {}).get(cid, {})
                points     = score_data.get('points', 0)
                comment    = score_data.get('comment', '')
                rubric_assessment[cid] = {'points': points, 'comments': comment}

            payload = {
                'submission': {
                    'posted_grade': str(result.get('total_score', 0))
                },
                'rubric_assessment': rubric_assessment,
                'comment': {'text_comment': result.get('overall_comment', '')}
            }

        ctx     = ssl.create_default_context()
        conn    = http.client.HTTPSConnection(CANVAS_HOST, context=ctx)
        headers = {
            'Authorization':  auth,
            'Content-Type':   'application/json',
            'User-Agent':     'PVHS-Dashboard/1.0'
        }
        path    = (
            f'/api/v1/courses/{COURSE_ID}/assignments/{assignment_id}'
            f'/submissions/{student_id}'
        )
        body    = json.dumps(payload).encode('utf-8')
        try:
            print(f'[canvas] PUT {path}')
            print(f'[canvas] Payload: {json.dumps(payload)[:500]}')
            conn.request('PUT', path, body=body, headers=headers)
            resp = conn.getresponse()
            raw  = resp.read()
            print(f'[canvas] PUT submission {student_id} -> {resp.status}')
            if resp.status >= 400:
                print(f'[canvas] Error response: {raw.decode("utf-8", errors="replace")[:500]}')
            return json.loads(raw)
        except Exception as e:
            print(f'[canvas] POST error: {e}')
            import traceback; traceback.print_exc()
            return None
        finally:
            conn.close()

    # ── Canvas: Authenticated Image Fetch ────────────────────────────────────

    def fetch_images(self, attachments, auth):
        images = []
        for att in attachments:
            url = att.get('url', '')
            if not url:
                continue
            # Canvas uses content_type (underscore), not content-type (hyphen)
            mime = (att.get('content_type') or att.get('content-type') or
                    att.get('mime_class') or 'image/jpeg')
            if not mime.startswith('image/'):
                mime = 'image/jpeg'
            try:
                # Canvas attachment URLs are pre-signed S3 URLs.
                # Sending Authorization header to S3 causes a 400 error.
                # First try without auth (works for pre-signed URLs).
                # Fall back with auth if that fails (for non-S3 URLs).
                fetched = False
                for headers in [
                    {'User-Agent': 'PVHS-Dashboard/1.0'},
                    {'Authorization': auth, 'User-Agent': 'PVHS-Dashboard/1.0'}
                ]:
                    try:
                        req = urllib.request.Request(url, headers=headers)
                        with urllib.request.urlopen(req, timeout=30) as r:
                            raw = r.read()
                        b64 = base64.b64encode(raw).decode('utf-8')
                        images.append({'b64': b64, 'mime': mime})
                        print(f'[grade] Fetched {att.get("display_name","?")} ({len(raw)} bytes, {mime})')
                        fetched = True
                        break
                    except urllib.error.HTTPError as e:
                        if e.code in (400, 403) and headers.get('Authorization'):
                            continue
                        raise
                if not fetched:
                    print(f'[grade] Could not fetch {att.get("display_name","?")}')
            except Exception as e:
                print(f'[grade] Image fetch failed: {e}')
        return images

    # ── Canvas: Generic GET ───────────────────────────────────────────────────

    def canvas_get(self, path, auth):
        ctx  = ssl.create_default_context()
        conn = http.client.HTTPSConnection(CANVAS_HOST, context=ctx)
        headers = {'Authorization': auth, 'User-Agent': 'PVHS-Dashboard/1.0'}
        if '?' not in path:
            path += '?per_page=100'
        else:
            path += '&per_page=100'
        try:
            conn.request('GET', f'/api/v1{path}', headers=headers)
            resp = conn.getresponse()
            return json.loads(resp.read())
        finally:
            conn.close()

    # ── File Server ───────────────────────────────────────────────────────────

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
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    # ── Canvas Proxy (GET /api/*) ─────────────────────────────────────────────

    def proxy_to_canvas(self):
        auth = self.headers.get('Authorization', '')
        ctx  = ssl.create_default_context()
        conn = http.client.HTTPSConnection(CANVAS_HOST, context=ctx)
        headers = {'User-Agent': 'PVHS-Dashboard/1.0'}
        if auth:
            headers['Authorization'] = auth

        try:
            conn.request('GET', self.path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()

            self.send_response(resp.status)
            ct = resp.getheader('Content-Type', 'application/json')
            self.send_header('Content-Type', ct)

            link = resp.getheader('Link', '')
            if link:
                link = link.replace(f'https://{CANVAS_HOST}', '')
                self.send_header('Link', link)

            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            msg = json.dumps({'error': str(e)}).encode()
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', len(msg))
            self.end_headers()
            self.wfile.write(msg)
        finally:
            conn.close()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def json_response(self, status, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def json_error(self, status, message):
        self.json_response(status, {'error': message})

    def log_message(self, format, *args):
        msg = format % args
        if any(x in msg for x in ('/api/', '/grade', 'gemini', 'canvas')):
            print(f'  [server] {msg}')


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

if __name__ == '__main__':
    print(f'PVHS Dashboard Server running on port {PORT}')
    if not GEMINI_KEY:
        print('  WARNING: GEMINI_API_KEY not set -- grading will not work')
    server = ThreadedHTTPServer(('0.0.0.0', PORT), Handler)
    server.serve_forever()
