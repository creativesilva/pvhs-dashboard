#!/usr/bin/env python3
"""
PVHS Canvas Missing Assignments Dashboard — proxy server.
Serves the dashboard HTML and proxies Canvas API requests to avoid CORS.
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import http.client
import ssl
import json
import os
import mimetypes

PORT = int(os.environ.get('PORT', 8080))
CANVAS_HOST = 'smjuhsd.instructure.com'
SERVE_DIR = os.path.dirname(os.path.abspath(__file__))


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path.startswith('/api/'):
            self.proxy_to_canvas()
        elif self.path in ('/', ''):
            self.path = '/index.html'
            self.serve_file()
        else:
            self.serve_file()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Max-Age', '86400')
        self.end_headers()

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

    def proxy_to_canvas(self):
        auth = self.headers.get('Authorization', '')
        ctx = ssl.create_default_context()
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

    def log_message(self, format, *args):
        msg = format % args
        if '/api/' in msg:
            print(f"  [proxy] {msg}")


if __name__ == '__main__':
    print(f"PVHS Dashboard Server running on port {PORT}")
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    server.serve_forever()
