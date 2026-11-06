"""No-key public job feed integrations. Respect providers' terms and rate limits."""
import hashlib
import re
import sys
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'PersonalJobSearchAgent/0.5 (personal noncommercial research)', 'Accept': 'application/json'})


def fetch(url, params=None):
    r = SESSION.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def strip_html(value):
    return BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)


def entry(source, title, company, location, url, description, identity=None):
    if not isinstance(url, str) or urlparse(url).scheme != 'https' or not urlparse(url).hostname:
        return None
    return {'id': f'{source}:{identity or hashlib.sha256(url.encode()).hexdigest()[:24]}',
            'company': str(company or source), 'title': strip_html(title),
            'location': strip_html(location), 'url': url, 'description': strip_html(description)}


def remoteok(cfg):
    data = fetch('https://remoteok.com/api')
    out = []
    if not isinstance(data, list): return out
    for j in data:
        if not isinstance(j, dict) or not j.get('position'): continue  # first entry is metadata
        item = entry('remoteok', j.get('position'), j.get('company'), j.get('location') or 'Remote; country eligibility unverified',
                     j.get('url') or j.get('apply_url'),
                     f"{j.get('description','')} Tags: {j.get('tags', [])} Salary: {j.get('salary_min','')} - {j.get('salary_max','')}", j.get('id'))
        if item: out.append(item)
    return out


def remotive(cfg):
    out=[]
    for term in cfg.get('free_api_search_terms', ['devops','site reliability','platform']):
        data=fetch('https://remotive.com/api/remote-jobs', {'search':term, 'limit':min(100,int(cfg.get('free_api_limit',50)))})
        for j in data.get('jobs',[]):
            item=entry('remotive',j.get('title'),j.get('company_name'),j.get('candidate_required_location') or 'Remote; eligibility unverified',
                j.get('url'), f"{j.get('description','')} Salary: {j.get('salary','')}",j.get('id'))
            if item: out.append(item)
        time.sleep(.25)
    return out


def jobicy(cfg):
    out=[]
    # Public API uses 'count' and 'tag' query params.
    for tag in cfg.get('jobicy_tags',['devops','engineering']):
        data=fetch('https://jobicy.com/api/v2/remote-jobs',{'count':min(50,int(cfg.get('free_api_limit',50))),'tag':tag})
        jobs=data.get('jobs',[]) if isinstance(data,dict) else []
        for j in jobs:
            item=entry('jobicy',j.get('jobTitle'),j.get('companyName'),j.get('jobGeo') or 'Remote; eligibility unverified',
                j.get('url'), f"{j.get('jobDescription','')} Salary: {j.get('annualSalaryMin','')} - {j.get('annualSalaryMax','')} {j.get('salaryCurrency','')}",j.get('id'))
            if item: out.append(item)
        time.sleep(.25)
    return out


def arbeitnow(cfg):
    out=[]
    pages=max(1,min(3,int(cfg.get('arbeitnow_pages',2))))
    for page in range(1,pages+1):
        data=fetch('https://www.arbeitnow.com/api/job-board-api',{'page':page})
        for j in data.get('data',[]):
            item=entry('arbeitnow',j.get('title'),j.get('company_name'),j.get('location') or 'Location unverified',
                j.get('url'), f"{j.get('description','')} Remote: {j.get('remote',False)} Tags: {j.get('tags',[])}",j.get('slug'))
            if item: out.append(item)
        if not data.get('data'): break
        time.sleep(.25)
    return out


def discover_free(cfg):
    providers={'remoteok':remoteok,'remotive':remotive,'jobicy':jobicy,'arbeitnow':arbeitnow}
    out=[]
    for name in cfg.get('free_sources',list(providers)):
        if name not in providers: continue
        try:
            items=providers[name](cfg)
            print(f'{name}: {len(items)} postings fetched')
            out.extend(items)
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            print(f'{name} failed: {exc}',file=sys.stderr)
    return out
