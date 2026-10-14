"""
describe_agent.py
Stage 5: for each job, find the real posting and pull out the facts that
scoring needs (the "criteria"), not the whole description.

This is a small AGENT: instead of us deciding the steps, Claude (Haiku) looks
at the job and decides which tool to try next, until it has what it needs:

  1. fetch_page          open a job board page (Built In, Dice, Wellfound)
                         or a company careers page. Never LinkedIn.
  2. web_search          search "company + title" (runs on Anthropic's servers)
  3. fetch_ats_posting   read the posting from Greenhouse, Lever, or Ashby,
                         the public job APIs many companies use

The loop (run_agent) works like this:
  - send the job + tools to Claude
  - if Claude asks to use a tool, run it and send back the result
  - repeat until Claude answers with the final JSON, or we hit MAX_STEPS
"""

import difflib
import html
import json
from urllib.parse import urlparse

import requests

from extract import html_to_text

MAX_STEPS = 6            # tool rounds per job before we force an answer
MAX_PAGE_CHARS = 15000   # we only send the first part of long pages
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
}
# Sites we never fetch: LinkedIn (against their terms), Indeed (blocks scripts).
NEVER_FETCH = ("linkedin.com", "indeed.com")


# ---------------------------------------------------------------------------
# Tools (plain Python functions Claude can ask us to run)
# ---------------------------------------------------------------------------

def fetch_page(url):
    """Download a web page and return its visible text."""
    host = urlparse(url).netloc.lower()
    if any(site in host for site in NEVER_FETCH):
        return "Error: this site can't be fetched. Use web_search or fetch_ats_posting instead."

    response = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
    if response.status_code != 200:
        return f"Error: the page returned HTTP {response.status_code} (it may be expired or blocked)."
    return html_to_text(response.text)[:MAX_PAGE_CHARS]


def best_title_match(wanted_title, postings, get_title):
    """
    Find the posting whose title is closest to wanted_title.
    difflib gives a similarity score from 0 (nothing alike) to 1 (identical).
    Returns (posting, score), or (None, 0) if the list is empty.
    """
    best, best_score = None, 0.0
    for posting in postings:
        score = difflib.SequenceMatcher(
            None, wanted_title.lower(), get_title(posting).lower()
        ).ratio()
        if score > best_score:
            best, best_score = posting, score
    return best, best_score


def fetch_ats_posting(ats, company_slug, title):
    """
    Look up a company's openings on Greenhouse, Lever, or Ashby and return the
    posting whose title best matches. company_slug is the company's short name
    in that system's URLs, e.g. "airbnb" in boards.greenhouse.io/airbnb.
    """
    ats = ats.lower()
    if ats == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true"
        key_title = lambda p: p["title"]
    elif ats == "lever":
        url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"
        key_title = lambda p: p["text"]
    elif ats == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{company_slug}?includeCompensation=true"
        key_title = lambda p: p["title"]
    else:
        return "Error: ats must be greenhouse, lever, or ashby."

    response = requests.get(url, timeout=20)
    if response.status_code != 200:
        return f"Error: no {ats} job board found for '{company_slug}' (HTTP {response.status_code})."

    data = response.json()
    postings = data if ats == "lever" else data.get("jobs", [])
    posting, score = best_title_match(title, postings, key_title)
    if posting is None or score < 0.6:
        some_titles = ", ".join(key_title(p) for p in postings[:15])
        return f"No close title match on {ats}/{company_slug}. Some open titles: {some_titles}"

    # Pull out the useful fields; each system names them differently.
    if ats == "greenhouse":
        text = html_to_text(html.unescape(posting.get("content", "")))
        location = posting.get("location", {}).get("name", "")
        link = posting.get("absolute_url", "")
        extra = f"Updated: {posting.get('updated_at', '')}"
    elif ats == "lever":
        parts = [posting.get("descriptionPlain", "")]
        for section in posting.get("lists", []):
            parts.append(section.get("text", "") + "\n" + html_to_text(section.get("content", "")))
        parts.append(posting.get("additionalPlain", ""))
        text = "\n".join(parts)
        location = posting.get("categories", {}).get("location", "")
        link = posting.get("hostedUrl", "")
        extra = f"Workplace: {posting.get('workplaceType', '')}"
    else:  # ashby
        text = posting.get("descriptionPlain", "")
        location = posting.get("location", "")
        link = posting.get("jobUrl", "")
        comp = (posting.get("compensation") or {}).get("compensationTierSummary", "")
        extra = f"Remote: {posting.get('isRemote')}, Workplace: {posting.get('workplaceType', '')}, Pay: {comp}"

    return (f"Title: {key_title(posting)}\nLocation: {location}\n{extra}\nURL: {link}\n\n"
            f"{text}")[:MAX_PAGE_CHARS]


# Tool definitions sent to Claude: name, what it does, and its inputs.
TOOLS = [
    {
        "name": "fetch_page",
        "description": "Download a web page (job board posting or company careers page) and return its text. "
                       "Does not work for LinkedIn or Indeed.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full https:// URL"}},
            "required": ["url"],
        },
    },
    {
        "name": "fetch_ats_posting",
        "description": "Get a posting from a company's public Greenhouse, Lever, or Ashby job board. "
                       "Use when a search result or careers page links to boards.greenhouse.io/<slug>, "
                       "job-boards.greenhouse.io/<slug>, jobs.lever.co/<slug>, or jobs.ashbyhq.com/<slug>.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ats": {"type": "string", "enum": ["greenhouse", "lever", "ashby"]},
                "company_slug": {"type": "string", "description": "The <slug> part of the board URL"},
                "title": {"type": "string", "description": "Job title to look for"},
            },
            "required": ["ats", "company_slug", "title"],
        },
    },
    # Server tool: Anthropic runs the search; we don't write any code for it.
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 2,
     "blocked_domains": ["linkedin.com", "indeed.com"]},
]

PYTHON_TOOLS = {"fetch_page": fetch_page, "fetch_ats_posting": fetch_ats_posting}


# ---------------------------------------------------------------------------
# The answer we want back
# ---------------------------------------------------------------------------

text_list = {"type": "array", "items": {"type": "string"}}
CRITERIA_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "source": {"type": "string"},          # e.g. "Built In page", "Greenhouse", "careers page"
        "source_url": {"type": "string"},
        "remote": {"type": "string", "enum": ["yes", "no", "hybrid", "unknown"]},
        "remote_details": {"type": "string"},  # e.g. "remote, US only", "3 days in Boston office"
        "must_have": text_list,
        "nice_to_have": text_list,
        "industry": {"type": "string"},
        "python": {"type": "string"},          # how much Python, any coding test mentioned
        "management": {"type": "string"},      # direct reports, team lead, path to manage
        "culture_signals": {"type": "string"},
        "salary_min": {"type": ["integer", "null"]},
        "salary_max": {"type": ["integer", "null"]},
        "benefits": {"type": "string"},
        "company_size": {"type": "string"},
        "travel_and_locations": {"type": "string"},
        "unusual": text_list,                  # clearance, drug test, AI notetaker ban, odd travel...
        "posted_date": {"type": ["string", "null"]},
    },
    "required": [
        "found", "source", "source_url", "remote", "remote_details", "must_have",
        "nice_to_have", "industry", "python", "management", "culture_signals",
        "salary_min", "salary_max", "benefits", "company_size",
        "travel_and_locations", "unusual", "posted_date",
    ],
    "additionalProperties": False,
}

INSTRUCTIONS = """You research one job posting and extract the facts a job seeker
needs to evaluate it. You do NOT judge fit; you only report facts.

Find the real posting, trying tools in this order:
1. If the job's link is on Built In, Dice, or Wellfound, use fetch_page on it.
2. Otherwise (LinkedIn, Indeed, or the page failed), use web_search for
   "<company> <title>" to find the employer's own posting. If a result is on
   Greenhouse, Lever, or Ashby, prefer fetch_ats_posting; otherwise fetch_page.
3. Stop as soon as you have the posting. Be efficient: few tool calls.

Make sure the posting is the same job (same company, same or very similar title).
found=true if you read the posting itself OR a copy of the same posting on
another site (say which in "source"). found=false only if you read no posting
at all; then fill what you can from the email details.

Extraction rules:
- Write short phrases, not paragraphs. Use "" or [] when the posting says nothing.
- must_have / nice_to_have: concrete requirements (tools, years, domains, degrees).
- python: what the posting says about Python level, and any mention of coding
  tests, live coding, or technical assessments. "not mentioned" if none.
- management: direct reports, team size, "lead", or growth path to management.
- salary: yearly US dollars (hourly x 2080). null if not stated.
- unusual: anything notable, good or bad: frequent or interesting travel
  (e.g. Hawaii, Mexico, Miami), security clearance, drug test, US-only work
  authorization, bans on AI notetakers, very old posting, relocation, etc."""


# ---------------------------------------------------------------------------
# The agent loop
# ---------------------------------------------------------------------------

def run_agent(client, model, job):
    """
    Research one job. Returns (criteria_dict, usage_totals).
    usage_totals: {"input_tokens", "output_tokens", "searches"} for cost tracking.
    """
    job_text = (f"Company: {job['company']}\nTitle: {job['title']}\n"
                f"Location: {job['location']}\nSource: {job['source']}\nLink: {job['link']}")
    messages = [{"role": "user", "content": job_text}]
    totals = {"input_tokens": 0, "output_tokens": 0, "searches": 0}

    for step in range(MAX_STEPS + 1):
        last_step = step == MAX_STEPS
        response = client.messages.create(
            model=model,
            max_tokens=8000,
            system=INSTRUCTIONS,
            tools=TOOLS,
            # On the last step, tools are switched off so Claude must answer now.
            tool_choice={"type": "none"} if last_step else {"type": "auto"},
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": CRITERIA_SCHEMA}},
            messages=messages,
        )
        totals["input_tokens"] += response.usage.input_tokens
        totals["output_tokens"] += response.usage.output_tokens
        server_use = getattr(response.usage, "server_tool_use", None)
        totals["searches"] += getattr(server_use, "web_search_requests", 0) or 0

        # Keep the conversation going: Claude's turn goes into the history.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
            # A long web search paused; sending the history back resumes it.
            continue

        tool_calls = [block for block in response.content if block.type == "tool_use"]
        if not tool_calls:
            # No more tools: the last text block is the final JSON answer.
            text = [block.text for block in response.content if block.type == "text"][-1]
            return json.loads(text), totals

        # Run each tool Claude asked for and send all the results back together.
        results = []
        for call in tool_calls:
            try:
                output = PYTHON_TOOLS[call.name](**call.input)
                is_error = output.startswith("Error")
            except Exception as error:  # network problems, bad JSON, etc.
                output, is_error = f"Error: {error}", True
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": output, "is_error": is_error})
        messages.append({"role": "user", "content": results})

    raise RuntimeError("agent did not finish")  # not reachable: last step has tools off


def not_found(job, reason):
    """The answer we store when the agent fails completely."""
    return {
        "found": False, "source": "", "source_url": job["link"], "remote": "unknown",
        "remote_details": "", "must_have": [], "nice_to_have": [], "industry": "",
        "python": "", "management": "", "culture_signals": "", "salary_min": None,
        "salary_max": None, "benefits": "", "company_size": "", "travel_and_locations": "",
        "unusual": [f"Description not found ({reason})"], "posted_date": None,
    }


def describe_job(client, model, job):
    """Run the agent on one job; never raises, so one bad job can't stop a run."""
    try:
        return run_agent(client, model, job)
    except Exception as error:
        return not_found(job, str(error)[:200]), {"input_tokens": 0, "output_tokens": 0, "searches": 0}


# ---------------------------------------------------------------------------
# Run on many jobs
# ---------------------------------------------------------------------------

REMOTE_GROUPS = {"yes": "Remote", "no": "Onsite/Hybrid", "hybrid": "Onsite/Hybrid"}


def describe_jobs(jobs, client, config):
    """
    Research every job, 8 at a time, and copy the results into each job:
      description_found, description_source, description_url, criteria (JSON text).
    Also fills in what the alert email didn't have:
      - remote_group, when the email didn't say (was "Unknown")
      - salary, when the email had none
    Returns cost totals: {"input_tokens", "output_tokens", "searches"}.
    """
    from concurrent.futures import ThreadPoolExecutor

    model = config["models"]["describe"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda job: describe_job(client, model, job), jobs))

    totals = {"input_tokens": 0, "output_tokens": 0, "searches": 0}
    for job, (criteria, usage) in zip(jobs, results):
        for key in totals:
            totals[key] += usage[key]

        job["description_found"] = criteria["found"]
        job["description_source"] = criteria["source"]
        job["description_url"] = criteria["source_url"]
        job["criteria"] = json.dumps(criteria)

        if job.get("remote_group") == "Unknown" and criteria["remote"] in REMOTE_GROUPS:
            job["remote_group"] = REMOTE_GROUPS[criteria["remote"]]
        if not job.get("salary_max") and criteria["salary_max"]:
            job["salary_min"] = criteria["salary_min"]
            job["salary_max"] = criteria["salary_max"]

    return totals
