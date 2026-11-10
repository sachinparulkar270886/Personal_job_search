# Local job-search agent (starter)

This starter discovers public Greenhouse and Lever listings, filters role titles, uses Ollama to assess match and remote eligibility for a Poland-based candidate, stores matches in SQLite, and exports CSV. Only jobs whose posting explicitly confirms eligibility in Poland, Europe/EEA, or worldwide are included; unclear and UK-only roles are excluded. Each scan's CSV contains only that scan's qualifying jobs. Its optional `apply.py` helper can fill supported application forms and submit one selected application only after explicit confirmation; it stops when it cannot safely identify or complete the form.

## macOS setup

1. Install Python 3.11+ and Ollama from https://ollama.com/download/mac
2. Run `ollama pull llama3.2:3b` and verify `ollama list`.
3. `cd job_agent_starter`
4. `python3 -m venv .venv && source .venv/bin/activate`
5. `pip install -r requirements.txt`
6. `python -m playwright install chromium`
7. Copy `config.example.json` to `config.json` and `profile.example.txt` to `profile.txt`. Add employer board slugs to `config.json` and replace the example profile with accurate information. Local config, profile, applicant data, resumes, and scan output are ignored by Git.
8. `python agent.py scan`
9. Review `matches.csv`, particularly Poland eligibility and salary (AI output may be wrong).
10. Run `python agent.py open` to choose a job from `matches.csv` and open its application page in your default browser. You can also open a specific link directly with `python agent.py open --url 'https://example.com/actual-job-link'`. After opening, answer `yes` if you applied; the job is recorded as completed/applied and excluded from scans for 30 days. Answer `no` to leave it unmarked. Complete any site's human-verification checks yourself.

## Local web dashboard

With dependencies installed, run `./.venv/bin/python dashboard.py`. It opens a local webpage with a **Start scan** button and a live log panel. The flow is **Start scan → View matches → click a job → Yes or No**. After the scan finishes, **Start scan** is disabled and faded; viewing matches is required before applying. Click a job title or URL in the matches table to open its HTTPS page in a new tab and confirm whether you applied in a dialog. Confirming Yes records the job as completed and removes it from the displayed matches; No leaves it in the list. While a scan or application confirmation is in progress, other actions show a notice with the current status and ask you to wait; finish the application's Yes/No confirmation to continue. The scan runs in the background using the same Python environment. The dashboard listens only on this computer at `http://127.0.0.1:8765`; Ollama must be running for scoring.

Greenhouse board slugs appear in `job-boards.greenhouse.io/<slug>` URLs; Lever site names appear in `jobs.lever.co/<site>`. An API request may fail if a company uses a different ATS or no longer has that board. These are discovery sources, not global job search.

Do not upload your resume to unknown websites. Never bypass CAPTCHA, impersonate others, or make false eligibility declarations. Add automatic form filling and submission only for explicitly supported employer workflows after validating required fields and permission to automate. Public Greenhouse GET job listing endpoints do not grant access to its authenticated application POST API.

## Extension: resume-aware safe autofill and durable application tracking

This version adds `apply.py`, `applicant.example.json`, and `pypdf`.

1. On Apple Silicon: `brew install ollama` (or install Ollama app), `ollama pull llama3.2:3b`; run `ollama serve` only if the app is not already running.
2. Update dependencies: `pip install -r requirements.txt && python -m playwright install chromium`.
3. Copy `applicant.example.json` to `applicant.json`; enter your own identity/contact information. Never commit this file.
4. Place your real **text-based** CV at `resume.pdf` (or change `resume_path` in `config.json` to a `.txt` file). The tool validates local resume extraction but **does not yet generate tailored answers from it**; the existing `scan` command uses `profile.txt` for Ollama matching.
5. `python agent.py scan` to refresh jobs and export `matches.csv`.
6. Add truthful answers you have personally verified to `application_answers` in `applicant.json`, using each site's exact question label as the key. Never guess work authorization, salary, legal, or other personal answers.
7. Run `python apply.py prepare` to process one selected job, or `python apply.py prepare --all` to process the CSV jobs sequentially. The batch mode skips jobs already tracked as submitted or awaiting receipt confirmation.
8. On the first run, close all Chrome windows so the script can safely copy your default Chrome profile, including its local browser data, into a separate automation-compatible profile. The copy is isolated from the live profile; changes are not shared between them, although Chrome Sync may sync data according to your existing settings. For each job, the script opens a separate tab in that copied profile, follows one unambiguous apply link when present, and fills only labeled fields matched to your profile or configured answers. The CDP connection preserves the browser's default download behavior for compatibility with Chrome versions that do not support Playwright's download override. Tabs remain open after processing, including jobs skipped for human verification, missing/unsupported required fields, or ambiguous forms, so you can review them manually. Resume uploads and consent controls must be handled manually; verify sensitive answers yourself.
9. Review each completed form in Chrome. Type `APPLY` in the terminal to submit that one application, or press Enter to cancel it. If the employer confirms receipt, type `YES` so the tracker records it as submitted. This confirmation is required separately for every job; batch mode does not submit without it.
10. Use `python apply.py mark --status submitted` only if you need to record an application you submitted manually; choose the job from `matches.csv` when prompted, or pass its URL with `--url`.
11. After you have reviewed a job and finished applying (or decided you are done with it), mark it complete with `python apply.py mark --status completed`. Choose it from `matches.csv`, or pass its URL with `--url 'https://example.com/job-url'`. Completed jobs are excluded from scans for 30 days, then can appear again if still listed. To reopen one sooner, mark it with another status or wait until the 30 days pass.
12. `python apply.py history` shows the append-only event log for a selected job. Use `--status interview`, `rejected`, `offer`, or `withdrawn` to update status.

Tracking is persisted in `jobs.sqlite3` in `applications` and `application_events` tables. The script refuses to prepare a job already marked submitted/interview/offer. Closing the browser never marks an application submitted. Note: event logging is durable locally but is not tamper-proof; back up the SQLite file.

**Safety:** Chrome remote debugging is enabled only on localhost for the dedicated profile. Every submission, including in batch mode, requires typing `APPLY` after reviewing that application's page. No CAPTCHA bypass, automated login, resume upload, or guessed answers. Only use sites where automation is permitted. Sensitive applicant data stays in a local ignored JSON file; values and configured answers are sent to the employer if you submit. No guarantee of support for every ATS. For multi-page forms, custom dropdowns, or sites that label fields differently, complete the form manually.


## v3: Resume-aware screening answer drafts (local Ollama)

This generates draft answers from a text-based PDF/TXT resume and a job description you save as a TXT file.
It does NOT scrape jobs, auto-fill answers into websites, or submit applications.

1. Put your actual `resume.pdf` in this folder.
2. Paste a job description into `job.txt` and screening questions (one per line) into `questions.txt`.
3. Run `ollama pull llama3.2:3b` (or use another locally installed model).
4. Run:

```bash
python answers.py --resume resume.pdf --job job.txt --questions questions.txt --model llama3.2:3b
```

Review `draft_answers.json`. `needs_review` indicates that exact evidence excerpts were not verified.
Even when evidence is present, model-generated wording may be inaccurate: verify all claims.
Sensitive questions are intentionally left for manual completion. Never put personal secrets into job descriptions.
For stronger results on Apple Silicon with enough memory, consider `ollama pull qwen2.5:7b`
and `--model qwen2.5:7b`.


## v4: broader discovery

`python agent.py scan` now also queries Himalayas (Poland-eligible search) and
configured Ashby boards. Greenhouse and Lever remain enabled.

Optional indexed web discovery for LinkedIn, Truly Remote Work, No Fluff Jobs,
Ashby and Workday uses the **Brave Search API**, which requires a separately
obtained API key (its current pricing/free allowance depends on your plan).
This searches indexed pages and **does not log into or scrape LinkedIn**.

Set `brave_search_api_key` in your ignored local `config.json`, or set the
`BRAVE_SEARCH_API_KEY` environment variable. The environment variable takes
precedence when both are configured. Never put a real key in
`config.example.json` or commit it.

```json
"brave_search_api_key": "your_brave_search_api_key"
```

Then run `python agent.py scan`.

Without a Brave API key in `config.json` or the environment, the agent still
searches Greenhouse, Lever, Himalayas and Ashby; indexed web discovery is skipped.

Modify `search_terms`, `indexed_sites`, `ashby_boards`, and `max_web_queries`
in `config.json`. A search result is not proof that a listing is live,
that the candidate can work from Poland, or that salary matches your target.
The existing title filter expects senior/staff/principal/lead/head wording,
so generic listings are intentionally skipped. The Ollama model assesses
each candidate, but can be wrong: verify eligibility, salary and source links.

This is a discovery tool, not an automatic application submitter. Search
providers may change their API, terms, quotas and response schemas.
Himalayas source: https://himalayas.app/docs/remote-jobs-api
Ashby source: https://developers.ashbyhq.com/docs/public-job-posting-api


## v5: no-key job APIs

Run `python agent.py scan` with **no Brave key** to discover listings from
Remote OK, Remotive, Jobicy, Arbeitnow, Himalayas, and configured employer boards.
Sources are configured under `free_sources` and `free_api_search_terms` in
`config.json`. Some feeds may change their schema, terms, or limits; errors
from individual sources are reported without stopping the scan.

For 16 GB Apple Silicon, v5 defaults to `qwen2.5:7b`. Install it with
`ollama pull qwen2.5:7b`, or change `model` back to `llama3.2:3b`.

**LinkedIn, Truly Remote Work, and No Fluff Jobs are NOT searched without**
**the optional Brave API key.** There is no unrestricted no-key universal
search API. If you provide `BRAVE_SEARCH_API_KEY`, v4's indexed web search
continues to run as well.

The first scan can take several minutes because Ollama evaluates up to
`max_jobs_to_score` postings sequentially. The agent saves only jobs that
pass its score and eligibility filters. It never submits applications.
