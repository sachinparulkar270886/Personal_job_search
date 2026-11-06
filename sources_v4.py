"""Additional public job sources and optional Brave indexed search.
No login, captcha bypass, or restricted-site scraping.
"""
import os
import re
import sys
import time
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import requests
from bs4 import BeautifulSoup

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PersonalJobSearchAgent/0.4 (personal job discovery)"})

def get_json(url, params=None, headers=None):
    r = SESSION.get(url, params=params, headers=headers, timeout=25)
    r.raise_for_status()
    return r.json()

def plain(html):
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)

def canonical(url):
    p = urlparse(url)
    if p.scheme not in ("https", "http") or not p.hostname:
        return ""
    params = [(k,v) for k,v in parse_qsl(p.query) if not k.lower().startswith("utm_")]
    return urlunparse((p.scheme,p.netloc.lower(),p.path.rstrip("/"),"",urlencode(params),""))

def himalayas(cfg):
    out=[]
    for term in cfg.get("search_terms", []):
        for page in range(1, min(int(cfg.get("himalayas_pages", 2)),5)+1):
            for country in ("PL",):
                try:
                    data=get_json("https://himalayas.app/jobs/api/search",
                        {"q":term,"country":country,"sort":"recent","page":page})
                    jobs=data.get("jobs", [])
                    for j in jobs:
                        url=j.get("applicationLink") or j.get("guid") or ""
                        if not canonical(url): continue
                        loc=j.get("locationRestrictions") or []
                        out.append({
                            "id":"himalayas:"+str(j.get("guid") or canonical(url)),
                            "company":j.get("companyName") or "",
                            "title":j.get("title") or "",
                            "location":", ".join(str(x) for x in loc) if loc else "Remote; eligibility verify",
                            "url":url,
                            "description":plain(j.get("description") or j.get("excerpt")) +
                              " Salary: "+str(j.get("currency") or "")+" "+
                              str(j.get("minSalary") or "")+"-"+str(j.get("maxSalary") or "")+
                              " per "+str(j.get("salaryPeriod") or "annual")
                        })
                    if not jobs: break
                except Exception as e:
                    print(f"Himalayas {term} page {page}: {e}",file=sys.stderr)
                    break
            time.sleep(0.25)
    return out

def ashby(cfg):
    out=[]
    for board in cfg.get("ashby_boards", []):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+",board): continue
        try:
            data=get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}",
                          {"includeCompensation":"true"})
            for j in data.get("jobs", []):
                url=j.get("jobUrl") or j.get("applyUrl") or ""
                if not canonical(url): continue
                out.append({"id":"ashby:"+board+":"+str(j.get("id") or canonical(url)),
                  "company":board, "title":j.get("title") or "",
                  "location":j.get("location") or "",
                  "url":url,
                  "description":plain(j.get("descriptionHtml") or j.get("descriptionPlain") or "")+
                      " Compensation: "+str(j.get("compensation") or "")})
        except Exception as e:
            print(f"Ashby {board}: {e}",file=sys.stderr)
    return out

def indexed_search(cfg):
    """Optional Brave Search API: indexed links only, no direct LinkedIn scraping.
    Configure BRAVE_SEARCH_API_KEY in environment; free allowance depends on plan.
    """
    key=os.environ.get("BRAVE_SEARCH_API_KEY","").strip()
    if not key: return []
    out=[]
    sites=cfg.get("indexed_sites",[])
    terms=cfg.get("search_terms",[])
    max_queries=max(0,min(int(cfg.get("max_web_queries",12)),30))
    count=0
    for site in sites:
        if not re.fullmatch(r"[a-zA-Z0-9.-]+",site): continue
        for term in terms:
            if count>=max_queries: return out
            count+=1
            try:
                data=get_json("https://api.search.brave.com/res/v1/web/search",
                    {"q":f'site:{site} "{term}" remote',"count":20},
                    {"X-Subscription-Token":key,"Accept":"application/json"})
                for result in data.get("web",{}).get("results",[]):
                    url=canonical(result.get("url",""))
                    if not url or not urlparse(url).hostname.endswith(site): continue
                    title=plain(result.get("title",""))
                    desc=plain(result.get("description",""))
                    out.append({"id":"indexed:"+url,"company":site,
                      "title":title,"location":"Remote eligibility unverified",
                      "url":url,"description":desc})
            except Exception as e:
                print(f"Indexed search {site} {term}: {e}",file=sys.stderr)
            time.sleep(0.3)
    return out

def discover_extra(cfg):
    jobs=[]
    if cfg.get("enable_himalayas",True): jobs.extend(himalayas(cfg))
    jobs.extend(ashby(cfg))
    jobs.extend(indexed_search(cfg))
    return jobs
