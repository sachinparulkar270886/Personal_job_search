import csv
from html import escape
import json
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import urlparse

from agent import mark_application_completed, recently_completed_job_urls


ROOT = Path(__file__).resolve().parent
HOST = '127.0.0.1'
PORT = 8765
MAX_LOG_LINES = 5000

state_lock = threading.Lock()
state = {
    'running': False,
    'exit_code': None,
    'logs': [],
    'first_index': 0,
    'run_id': 0,
    'pending_applies': {},
}


def append_log(line):
    with state_lock:
        state['logs'].append(line.rstrip('\r\n'))
        if len(state['logs']) > MAX_LOG_LINES:
            dropped = len(state['logs']) - MAX_LOG_LINES
            del state['logs'][:dropped]
            state['first_index'] += dropped


def read_scan_output(process):
    try:
        for line in process.stdout:
            append_log(line)
        exit_code = process.wait()
    except Exception as exc:
        append_log(f'Failed while reading scan output: {exc}')
        exit_code = process.poll()
    finally:
        with state_lock:
            state['exit_code'] = exit_code
            state['running'] = False


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/':
            try:
                applicant = json.loads((ROOT / 'applicant.json').read_text(encoding='utf-8'))
                if not isinstance(applicant, dict):
                    raise ValueError('Applicant profile must be a JSON object')
                first_name = applicant.get('first_name')
                if not isinstance(first_name, str) or not first_name.strip():
                    raise ValueError('Applicant profile must include a first_name')
                page = (ROOT / 'dashboard.html').read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                print(f'Could not load applicant profile for dashboard: {exc}')
                self.send_error(500, 'Could not load applicant profile')
                return

            page = page.replace('{{first_name}}', escape(first_name.strip()))
            page = page.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return

        if path == '/api/status':
            try:
                after = max(0, int(urlparse(self.path).query.partition('after=')[2].split('&')[0] or 0))
            except ValueError:
                self.send_json(400, {'error': 'Invalid log cursor'})
                return

            with state_lock:
                logs = state['logs']
                first_index = state['first_index']
                response = {
                    'running': state['running'],
                    'exit_code': state['exit_code'],
                    'run_id': state['run_id'],
                    'first_index': first_index,
                    'logs': [
                        {'index': index, 'text': logs[index - first_index]}
                        for index in range(max(after, first_index), first_index + len(logs))
                    ],
                    'next_index': first_index + len(logs),
                }
            self.send_json(200, response)
            return

        if path == '/api/matches':
            matches_path = ROOT / 'matches.csv'
            try:
                with matches_path.open(newline='', encoding='utf-8') as matches_file:
                    reader = csv.DictReader(matches_file)
                    completed_urls = recently_completed_job_urls()
                    response = {
                        'headers': reader.fieldnames or [],
                        'rows': [
                            row for row in reader
                            if row.get('url') and row['url'] not in completed_urls
                        ],
                    }
            except OSError as exc:
                print(f'Could not read matches.csv: {exc}')
                self.send_json(500, {'error': 'Could not read matches.csv'})
                return
            except sqlite3.Error as exc:
                print(f'Could not read application history: {exc}')
                self.send_json(500, {'error': 'Could not check application history'})
                return

            self.send_json(200, response)
            return

        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/apply':
            try:
                payload = self.read_json_body()
                job_number = payload.get('job_number')
                if isinstance(job_number, bool) or not isinstance(job_number, int) or job_number < 1:
                    self.send_json(400, {'error': 'Enter a valid positive job number'})
                    return

                with (ROOT / 'matches.csv').open(newline='', encoding='utf-8') as matches_file:
                    completed_urls = recently_completed_job_urls()
                    jobs = [
                        row for row in csv.DictReader(matches_file)
                        if row.get('url') and row['url'] not in completed_urls
                    ]
            except (OSError, csv.Error) as exc:
                print(f'Could not load a job for application: {exc}')
                self.send_json(500, {'error': 'Could not read matches.csv'})
                return
            except sqlite3.Error as exc:
                print(f'Could not read application history: {exc}')
                self.send_json(500, {'error': 'Could not check application history'})
                return
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                self.send_json(400, {'error': f'Invalid request: {exc}'})
                return

            if job_number > len(jobs):
                self.send_json(404, {'error': f'Job number must be between 1 and {len(jobs)}'})
                return

            job = jobs[job_number - 1]
            parsed_url = urlparse(job['url'])
            if parsed_url.scheme != 'https' or not parsed_url.hostname:
                self.send_json(400, {'error': 'This job does not have a valid HTTPS application link'})
                return

            apply_token = token_urlsafe(24)
            with state_lock:
                state['pending_applies'][apply_token] = job['url']
            self.send_json(200, {
                'token': apply_token,
                'url': job['url'],
                'title': job.get('title', 'Untitled'),
                'company': job.get('company', 'Unknown company'),
            })
            return

        if path == '/api/apply/confirm':
            try:
                payload = self.read_json_body()
                apply_token = payload.get('token')
                applied = payload.get('applied')
                if not isinstance(apply_token, str) or not apply_token or not isinstance(applied, bool):
                    self.send_json(400, {'error': 'A valid confirmation is required'})
                    return
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                self.send_json(400, {'error': f'Invalid request: {exc}'})
                return

            with state_lock:
                job_url = state['pending_applies'].get(apply_token)
                if job_url is None:
                    self.send_json(404, {'error': 'Application confirmation expired; select the job again'})
                    return
                del state['pending_applies'][apply_token]
                if not applied:
                    self.send_json(200, {'message': 'Not marked as applied'})
                    return

            try:
                mark_application_completed(job_url)
            except sqlite3.Error as exc:
                with state_lock:
                    state['pending_applies'][apply_token] = job_url
                print(f'Could not record completed application: {exc}')
                self.send_json(500, {'error': 'Could not record the application; please try again'})
                return

            self.send_json(200, {'message': 'Recorded as completed and applied'})
            return

        if path != '/api/scan':
            self.send_error(404)
            return

        with state_lock:
            if state['running']:
                self.send_json(409, {'error': 'A scan is already running'})
                return

            state['running'] = True
            state['exit_code'] = None
            state['logs'] = []
            state['first_index'] = 0
            state['run_id'] += 1

        try:
            process = subprocess.Popen(
                [sys.executable, '-u', 'agent.py', 'scan'],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
            )
        except OSError as exc:
            with state_lock:
                state['running'] = False
                state['exit_code'] = -1
            append_log(f'Could not start scan: {exc}')
            self.send_json(500, {'error': 'Could not start scan'})
            return

        threading.Thread(target=read_scan_output, args=(process,), daemon=True).start()
        self.send_json(202, {'started': True})

    def read_json_body(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length < 1 or length > 4096:
            raise ValueError('Request body is missing or too large')
        payload = json.loads(self.rfile.read(length).decode('utf-8'))
        if not isinstance(payload, dict):
            raise ValueError('Request body must be a JSON object')
        return payload

    def send_json(self, status_code, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        print(f'{self.log_date_time_string()} {format_string % args}')


def main():
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f'Job scan dashboard: http://{HOST}:{PORT}')
    print('Only connections from this computer are accepted.')
    webbrowser.open(f'http://{HOST}:{PORT}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopping dashboard.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
