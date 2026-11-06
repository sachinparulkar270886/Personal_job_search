"""Prepare one job application and submit only after explicit confirmation."""
import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from agent import select_job_url
from playwright.sync_api import Error as PlaywrightError, sync_playwright
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'jobs.sqlite3'
PRIVATE = ROOT / 'applicant.json'
CHROME_DEBUG_PORT = 9223
CHROME_DEBUG_URL = f'http://127.0.0.1:{CHROME_DEBUG_PORT}'
CHROME_USER_DATA = Path.home() / 'Library' / 'Application Support' / 'Google' / 'Chrome'
CHROME_PROFILE_NAME = 'Default'
CHROME_PROFILE = Path.home() / 'Library' / 'Application Support' / 'job_agent_starter' / 'chrome-profile'


def now():
    return datetime.now(timezone.utc).isoformat()


def setup_db(con):
    con.execute('CREATE TABLE IF NOT EXISTS applications (job_url TEXT PRIMARY KEY, status TEXT NOT NULL, last_updated TEXT NOT NULL, notes TEXT DEFAULT "")')
    con.execute('CREATE TABLE IF NOT EXISTS application_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_url TEXT NOT NULL, event TEXT NOT NULL, timestamp TEXT NOT NULL, detail TEXT DEFAULT "")')
    con.commit()


def record(con, url, event, detail=''):
    con.execute('INSERT INTO application_events(job_url,event,timestamp,detail) VALUES (?,?,?,?)', (url,event,now(),detail))
    con.commit()


def status(con, url, value, notes=''):
    con.execute('INSERT INTO applications(job_url,status,last_updated,notes) VALUES (?,?,?,?) ON CONFLICT(job_url) DO UPDATE SET status=excluded.status,last_updated=excluded.last_updated,notes=excluded.notes', (url,value,now(),notes))
    con.commit()


def recently_completed(last_updated):
    completed_at = datetime.fromisoformat(last_updated)
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    return completed_at >= datetime.now(timezone.utc) - timedelta(days=30)


def resume_text(path):
    if not path.exists():
        raise FileNotFoundError(f'Add your actual resume at {path}')
    if path.suffix.lower() == '.pdf':
        return '\n'.join((page.extract_text() or '') for page in PdfReader(str(path)).pages)
    if path.suffix.lower() == '.txt':
        return path.read_text(encoding='utf-8')
    raise ValueError('Resume must be .pdf or .txt')


def safe_url(url):
    u = urlparse(url)
    if u.scheme != 'https' or not u.hostname or u.username or u.password:
        raise ValueError('A normal HTTPS job URL is required')
    if u.hostname in {'localhost','127.0.0.1'} or u.hostname.endswith('.local'):
        raise ValueError('Local URLs not supported')
    return url


def fill_if_found(page, label, value):
    if not value:
        return False
    loc = page.get_by_label(re.compile(r'^\s*'+re.escape(label)+r'\s*(?:\*)?\s*$',re.I))
    try:
        if loc.count() == 1 and loc.first.is_visible():
            tag = loc.first.evaluate('(e) => e.tagName.toLowerCase()')
            typ = loc.first.get_attribute('type') or ''
            if tag == 'select':
                loc.first.select_option(label=str(value), timeout=2500)
                return True
            if tag in {'input','textarea'} and typ in {'','text','email','tel','url','search'}:
                loc.first.fill(str(value), timeout=2500)
                return True
    except PlaywrightError as exc:
        print(f'Could not inspect or fill "{label}": {exc}', file=sys.stderr)
    return False


def normalize_label(label):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', label.lower())).strip()


def profile_values(applicant):
    values = {}
    aliases = {
        'first_name': ('first name', 'given name'),
        'last_name': ('last name', 'family name', 'surname'),
        'full_name': ('full name', 'name'),
        'email': ('email', 'email address'),
        'phone': ('phone', 'phone number', 'mobile phone', 'telephone'),
        'linkedin': ('linkedin', 'linkedin profile', 'linkedin url'),
        'github': ('github', 'github profile', 'github url'),
    }
    for key, labels in aliases.items():
        value = applicant.get(key)
        if value:
            for label in labels:
                values[normalize_label(label)] = value
    answers = applicant.get('application_answers', {})
    if isinstance(answers, dict):
        for label, value in answers.items():
            if isinstance(label, str) and isinstance(value, (str, int, float)) and str(value).strip():
                values[normalize_label(label)] = value
    return values


def required_fields(page):
    controls = page.locator(
        'input[required]:visible, textarea[required]:visible, select[required]:visible, '
        '[aria-required="true"]:visible'
    )
    fields = []
    for index in range(controls.count()):
        control = controls.nth(index)
        details = control.evaluate("""el => {
            const labels = [...(el.labels || [])].map(label => label.innerText.trim()).filter(Boolean);
            const labelledBy = (el.getAttribute('aria-labelledby') || '')
                .split(/\\s+/).map(id => document.getElementById(id)?.innerText.trim())
                .filter(Boolean);
            return {
                label: labels[0] || labelledBy[0] || el.getAttribute('aria-label')
                    || el.getAttribute('placeholder') || '',
                type: (el.getAttribute('type') || el.tagName).toLowerCase(),
                value: el.value || '',
                checked: !!el.checked
            };
        }""")
        fields.append((control, details))
    return fields


def find_application_form(page, values):
    forms = page.locator('form:visible')
    matches = []
    labels = sorted(values)
    for form_index in range(forms.count()):
        form = forms.nth(form_index)
        matching_labels = 0
        for label in labels:
            pattern = re.compile(r'^\s*'+re.escape(label)+r'\s*(?:\*)?\s*$', re.I)
            control = form.get_by_label(pattern)
            if control.count() == 1 and control.first.is_visible():
                matching_labels += 1
        if matching_labels >= 2:
            matches.append(form)
    if len(matches) == 1:
        return matches[0]
    return None


def has_human_verification(page):
    title = page.title().lower()
    url = page.url.lower()
    body = page.locator('body').inner_text(timeout=3000).lower()
    challenge_selectors = (
        '#challenge-form, .cf-browser-verification, '
        'iframe[src*="challenges.cloudflare.com"], iframe[src*="recaptcha"], '
        'iframe[src*="hcaptcha"], iframe[src*="turnstile"]'
    )
    return (
        'challenge' in url
        or 'cdn-cgi' in url
        or any(phrase in title for phrase in ('just a moment', 'security verification'))
        or any(phrase in body for phrase in (
            'verifying you are human',
            'performing security verification',
            'verify you are human',
        ))
        or page.locator(challenge_selectors).count() > 0
    )


def application_link(page):
    links = page.locator('a').evaluate_all("""elements => elements
        .filter(el => {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        })
        .map(el => ({
            text: (el.innerText || el.getAttribute('aria-label') || '').trim(),
            href: el.href
        }))""")
    label_pattern = re.compile(
        r'^(apply|apply now|apply for this job|apply on company website|'
        r'apply on company site|apply externally)$',
        re.I,
    )
    matches = [link for link in links if label_pattern.fullmatch(link['text'])]
    if len(matches) != 1:
        return None
    return safe_url(matches[0]['href'])


def prepare_application(page, applicant, allow_follow=True):
    if has_human_verification(page):
        return [], ['Human verification detected; complete it yourself in Chrome and rerun.']

    values = profile_values(applicant)
    form = find_application_form(page, values)
    if form is None:
        target = application_link(page) if allow_follow else None
        if target:
            page.goto(target, wait_until='domcontentloaded', timeout=60000)
            return prepare_application(page, applicant, allow_follow=False)
        return [], ['Could not identify one application form with at least two labeled, configured fields.']

    filled = []
    for label, value in (
        ('First Name', applicant.get('first_name')),
        ('Given Name', applicant.get('first_name')),
        ('Last Name', applicant.get('last_name')),
        ('Family Name', applicant.get('last_name')),
        ('Full Name', applicant.get('full_name')),
        ('Name', applicant.get('full_name')),
        ('Email', applicant.get('email')),
        ('Email Address', applicant.get('email')),
        ('Phone', applicant.get('phone')),
        ('Phone Number', applicant.get('phone')),
        ('LinkedIn', applicant.get('linkedin')),
        ('LinkedIn Profile', applicant.get('linkedin')),
        ('GitHub', applicant.get('github')),
        ('GitHub Profile', applicant.get('github')),
    ):
        if fill_if_found(form, label, value):
            filled.append(label)

    answers = applicant.get('application_answers', {})
    if isinstance(answers, dict):
        for label, value in answers.items():
            if isinstance(label, str) and fill_if_found(form, label, value):
                filled.append(label)

    missing = []
    controls = required_fields(form)
    for _, details in controls:
        has_value = (
            details['checked']
            if details['type'] in {'checkbox', 'radio'}
            else bool(details['value'].strip())
        )
        if has_value:
            continue
        label = details['label']
        answer = values.get(normalize_label(label))
        if (
            answer is not None
            and details['type'] in {'input', 'text', 'email', 'tel', 'url', 'search', 'textarea', 'select'}
            and fill_if_found(form, label, answer)
        ):
            filled.append(label)
            continue
        missing.append(label or f'unlabeled {details["type"]} field')

    return filled, missing


def submit_application(form):
    buttons = form.locator('button:visible, input[type="submit"]:visible')
    submit_pattern = re.compile(
        r'^(submit|submit application|apply|apply now|send application|send my application)$',
        re.I,
    )
    candidates = []
    for index in range(buttons.count()):
        button = buttons.nth(index)
        label = (
            button.get_attribute('value')
            if button.evaluate('(el) => el.tagName.toLowerCase() === "input"')
            else button.inner_text()
        ) or ''
        label = label.strip()
        if submit_pattern.fullmatch(label):
            candidates.append(button)
    if len(candidates) != 1:
        return False, 'Could not identify exactly one clearly named submit button.'

    button = candidates[0]
    valid = button.evaluate('(el) => !el.form || el.form.checkValidity()')
    if not valid:
        return False, 'The form reports unanswered or invalid required fields.'

    print('Required fields appear complete. Review the form in Chrome before confirming.')
    confirmation = input('Type APPLY to submit this application, or press Enter to cancel: ')
    if confirmation != 'APPLY':
        return False, 'Submission cancelled; no application was sent.'
    button.click(timeout=10000)
    return True, 'Submission button clicked. Verify the employer site confirms receipt.'


def load_match_jobs():
    matches_path = ROOT / 'matches.csv'
    if not matches_path.is_file():
        raise FileNotFoundError('matches.csv not found; run "python agent.py scan" first')
    with matches_path.open(newline='', encoding='utf-8') as file:
        jobs = [row for row in csv.DictReader(file) if row.get('url')]
    if not jobs:
        raise ValueError('matches.csv has no jobs with URLs')

    unique_jobs = []
    seen = set()
    for job in jobs:
        if job['url'] not in seen:
            seen.add(job['url'])
            unique_jobs.append(job)
    return unique_jobs


def process_job(page, job, applicant, con, index, total):
    url = safe_url(job['url'])
    print(f'\n[{index}/{total}] {job.get("title", "Untitled")} — {job.get("company", "Unknown company")}')
    previous = con.execute(
        'SELECT status,last_updated FROM applications WHERE job_url=?', (url,)
    ).fetchone()
    if previous and (
        previous[0] in ('submitted', 'interview', 'offer', 'submission_confirmation_pending')
        or previous[0] == 'completed' and recently_completed(previous[1])
    ):
        print(f'Skipping; tracker status is {previous[0]}.')
        return 'skipped'

    page.goto(url, wait_until='domcontentloaded', timeout=60000)
    filled, missing = prepare_application(page, applicant)
    record(con, url, 'fields_prefilled', ','.join(filled))
    print('Prefilled:', filled)
    if missing:
        reason = 'Required fields need manual review: ' + '; '.join(missing)
        status(con, url, 'needs_manual_review', reason)
        record(con, url, 'application_skipped', reason)
        print(reason)
        print('No application was submitted; this job was skipped.')
        return 'skipped'

    form = find_application_form(page, profile_values(applicant))
    if form is None:
        reason = 'Application form could not be located for submission.'
        status(con, url, 'review_required', reason)
        record(con, url, 'application_not_submitted', reason)
        print(reason)
        return 'skipped'

    submitted, message = submit_application(form)
    if not submitted:
        status(con, url, 'review_required', message)
        record(con, url, 'application_not_submitted', message)
        print(message)
        return 'skipped'

    status(con, url, 'submission_confirmation_pending', message)
    record(con, url, 'submission_clicked', message)
    print(message)
    if input('Did the employer confirm receipt? Type YES to continue: ') == 'YES':
        status(con, url, 'submitted', 'User confirmed employer receipt')
        record(con, url, 'manual_status', 'submitted; user confirmed employer receipt')
        print('Recorded: submitted')
        return 'submitted'

    print('Not marked submitted. Verify the employer page before retrying this application.')
    return 'pending'


def process_batch(context, jobs, applicant, con):
    submitted_count = skipped_count = pending_count = 0
    for index, job in enumerate(jobs, start=1):
        previous = con.execute(
            'SELECT status,last_updated FROM applications WHERE job_url=?', (job['url'],)
        ).fetchone()
        if previous and (
            previous[0] in ('submitted', 'interview', 'offer', 'submission_confirmation_pending')
            or previous[0] == 'completed' and recently_completed(previous[1])
        ):
            print(
                f'\n[{index}/{len(jobs)}] Skipping already tracked job: '
                f'{job.get("title", "Untitled")} — {previous[0]}'
            )
            skipped_count += 1
            continue

        page = context.new_page()
        try:
            result = process_job(page, job, applicant, con, index, len(jobs))
        except (PlaywrightError, ValueError) as exc:
            job_url = job['url']
            message = f'Job preparation failed: {exc}'
            status(con, job_url, 'application_error', message)
            record(con, job_url, 'application_error', message)
            print(message, file=sys.stderr)
            result = 'skipped'
        if result == 'submitted':
            submitted_count += 1
        elif result == 'pending':
            pending_count += 1
        else:
            skipped_count += 1
    return submitted_count, pending_count, skipped_count


def chrome_binary():
    executable = shutil.which('google-chrome') or shutil.which('chrome')
    candidates = [
        Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        Path.home() / 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    ]
    if executable:
        candidates.insert(0, Path(executable))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError('Google Chrome was not found. Install Chrome or set up its executable path.')


def ensure_chrome_profile_copy():
    copied_state = CHROME_PROFILE / 'Local State'
    copied_profile = CHROME_PROFILE / CHROME_PROFILE_NAME
    if copied_state.is_file() and copied_profile.is_dir():
        return
    if CHROME_PROFILE.exists():
        raise RuntimeError(
            f'The copied Chrome profile at {CHROME_PROFILE} is incomplete. '
            'Resolve or remove that specific folder before retrying.'
        )

    source_state = CHROME_USER_DATA / 'Local State'
    source_profile = CHROME_USER_DATA / CHROME_PROFILE_NAME
    if not source_state.is_file() or not source_profile.is_dir():
        raise FileNotFoundError(
            f'Could not find the Chrome profile "{CHROME_PROFILE_NAME}" under {CHROME_USER_DATA}.'
        )

    processes = subprocess.run(
        ['ps', '-axo', 'command='],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    chrome_binary_marker = '/Google Chrome.app/Contents/MacOS/Google Chrome'
    if any(chrome_binary_marker in process for process in processes):
        raise RuntimeError(
            'Quit all Google Chrome windows before the first run so the Chrome profile '
            'can be copied safely, then run this command again.'
        )

    CHROME_PROFILE.mkdir(parents=True)
    shutil.copy2(source_state, copied_state)
    shutil.copytree(source_profile, copied_profile)


def chrome_debug_ready():
    try:
        response = requests.get(f'{CHROME_DEBUG_URL}/json/version', timeout=1)
        response.raise_for_status()
        return True
    except requests.RequestException:
        return False


def chrome_debug_uses_profile():
    processes = subprocess.run(
        ['ps', '-axo', 'command='],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    port_flag = f'--remote-debugging-port={CHROME_DEBUG_PORT}'
    profile_flag = f'--user-data-dir={CHROME_PROFILE}'
    chrome_binary_marker = '/Google Chrome.app/Contents/MacOS/Google Chrome'
    return any(
        chrome_binary_marker in process
        and port_flag in process
        and profile_flag in process
        for process in processes
    )


def connect_chrome(playwright):
    ensure_chrome_profile_copy()
    if not chrome_debug_ready():
        binary = chrome_binary()
        CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
        print('Starting the copied Chrome profile for job preparation...')
        subprocess.Popen(
            [
                str(binary),
                '--remote-debugging-address=127.0.0.1',
                f'--remote-debugging-port={CHROME_DEBUG_PORT}',
                f'--user-data-dir={CHROME_PROFILE}',
                f'--profile-directory={CHROME_PROFILE_NAME}',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(30):
            if chrome_debug_ready():
                break
            time.sleep(1)
        else:
            raise RuntimeError(
                f'Could not start the copied Chrome profile with remote debugging '
                f'on port {CHROME_DEBUG_PORT}.'
            )

    if not chrome_debug_uses_profile():
        raise RuntimeError(
            f'Chrome debugging port {CHROME_DEBUG_PORT} is already in use by a different '
            'Chrome profile. Close that Chrome session and retry.'
        )

    print('Connecting to the copied Chrome profile...')
    browser = playwright.chromium.connect_over_cdp(
        CHROME_DEBUG_URL, timeout=10000, no_defaults=True
    )
    if not browser.contexts:
        raise RuntimeError('The connected Chrome session has no browser context.')
    return browser, browser.contexts[0]


def main():
    parser = argparse.ArgumentParser(description='Prepare and submit selected job applications')
    parser.add_argument('action', choices=['prepare','mark','history'])
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--url', help='Job URL; omit to choose from matches.csv')
    target.add_argument('--all', action='store_true', help='Process all matches sequentially; prepare only')
    parser.add_argument(
        '--status',
        choices=['completed','submitted','interview','rejected','offer','withdrawn'],
    )
    parser.add_argument('--notes', default='')
    args = parser.parse_args()
    if args.all and args.action != 'prepare':
        parser.error('--all can only be used with prepare')
    if args.all:
        jobs = load_match_jobs()
    else:
        url = safe_url(args.url or select_job_url())
        jobs = [{'url': url}]

    con = sqlite3.connect(DB)
    setup_db(con)
    if args.action == 'history':
        for row in con.execute('SELECT timestamp,event,detail FROM application_events WHERE job_url=? ORDER BY event_id',(url,)):
            print(*row,sep=' | ')
        con.close()
        return
    if args.action == 'mark':
        if not args.status:
            parser.error('--status is required for mark')
        status(con,url,args.status,args.notes)
        record(con,url,'manual_status',args.status+' '+args.notes)
        print('Recorded:',args.status)
        con.close()
        return
    if not PRIVATE.exists():
        raise FileNotFoundError('Copy applicant.example.json to applicant.json and edit it')
    applicant = json.loads(PRIVATE.read_text(encoding='utf-8'))
    cfg = json.loads((ROOT/'config.json').read_text())
    resume = ROOT / cfg.get('resume_path','resume.pdf')
    text = resume_text(resume)
    if len(text.strip()) < 150:
        raise ValueError('Resume text is too short. If scanned, provide a text-based PDF or TXT.')
    print('Resume loaded locally:',len(text),'characters; SHA256:',hashlib.sha256(resume.read_bytes()).hexdigest()[:12])

    submitted_count = skipped_count = pending_count = 0
    with sync_playwright() as p:
        _, context = connect_chrome(p)
        submitted_count, pending_count, skipped_count = process_batch(
            context, jobs, applicant, con
        )
    print(
        f'\nBatch finished: {submitted_count} confirmed submitted, '
        f'{pending_count} need status verification, {skipped_count} skipped.'
    )
    con.close()

if __name__=='__main__':
    main()
