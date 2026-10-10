"""
extract.py
Stage 2: turn one alert email into a list of jobs.

How it works, in 4 steps:
1. Get the email as plain text (convert HTML to text if that's all there is).
2. Replace every link with a short label like [L7]. Job board links are
   hundreds of characters of tracking junk. Short labels are cheaper to send
   and Claude can't mistype them.
3. Ask Claude (Haiku) to list the jobs as JSON, pointing to links by label.
4. Swap each label back to a real link, cleaned of tracking where possible,
   and pull out the job ID.

Test it on the saved sample emails with:
    python extract.py
"""

import json
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import anthropic

from gmail_client import PROJECT_FOLDER, load_config

# ---------------------------------------------------------------------------
# Step 1: HTML to text
# ---------------------------------------------------------------------------

class _TextAndLinks(HTMLParser):
    """
    Walks through HTML tag by tag and keeps the visible text, adding line
    breaks where the layout had them. Each link's address is written right
    AFTER the link's text, e.g. "Senior Engineer ... (https://...)".
    Python's built-in HTMLParser calls these methods as it reads the HTML.
    """

    def __init__(self):
        super().__init__()
        self.pieces = []
        self.open_links = []  # addresses of <a> tags we are currently inside

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            # attrs is a list of (name, value) pairs; dict() makes it easy to look up.
            self.open_links.append(dict(attrs).get("href", ""))
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "table"):
            self.pieces.append("\n")

    def handle_endtag(self, tag):
        if tag == "a" and self.open_links:
            href = self.open_links.pop()
            if href.startswith("http"):
                self.pieces.append(f" ({href}) ")
        if tag in ("p", "div", "tr", "li", "td"):
            self.pieces.append("\n")

    def handle_data(self, data):
        self.pieces.append(data)


def html_to_text(html):
    """Turn HTML into readable text, keeping each link address after its text."""
    # Drop <style> and <script> blocks entirely; they are not visible text.
    html = re.sub(r"<(style|script)\b.*?</\1>", "", html, flags=re.IGNORECASE | re.DOTALL)

    parser = _TextAndLinks()
    parser.feed(html)
    text = "".join(parser.pieces)

    # Collapse runs of spaces and blank lines.
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def get_email_text(bodies):
    """Prefer the plain text version; fall back to converting the HTML one."""
    if bodies.get("plain", "").strip():
        return bodies["plain"]
    return html_to_text(bodies.get("html", ""))


# ---------------------------------------------------------------------------
# Step 2: replace links with short labels
# ---------------------------------------------------------------------------

# A URL starts with http(s):// and continues until a space, quote, or bracket.
URL_PATTERN = re.compile(r'https?://[^\s<>"\')\]|]+')


def label_links(text):
    """
    Replace every URL in the text with [L1], [L2], ...
    Returns the new text and a dictionary {"L1": "https://...", ...}.
    """
    links = {}

    def replace(match):
        label = f"L{len(links) + 1}"
        links[label] = html_unescape(match.group(0))
        return f"[{label}]"

    return URL_PATTERN.sub(replace, text), links


def html_unescape(url):
    """Plain text emails sometimes write & as &amp; inside links. Turn it back."""
    return url.replace("&amp;", "&")


# ---------------------------------------------------------------------------
# Step 4 helper: clean tracking out of links
# ---------------------------------------------------------------------------

def clean_link(url):
    """
    Turn a tracking link into the plain job page link, when we know how.
    Returns (clean_url, job_id). job_id is "" if we can't find one.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    # LinkedIn: .../comm/jobs/view/4475886456/?tracking... -> .../jobs/view/4475886456/
    match = re.search(r"/jobs/view/(\d+)", parsed.path)
    if "linkedin.com" in host and match:
        job_id = match.group(1)
        return f"https://www.linkedin.com/jobs/view/{job_id}/", job_id

    # Indeed: the job key is the "jk" value in the link.
    if "indeed.com" in host:
        jk = parse_qs(parsed.query).get("jk", [""])[0]
        if jk:
            return f"https://www.indeed.com/viewjob?jk={jk}", jk
        # Sponsored ads and "match" emails hide the job key; keep the link as is.
        return url, ""

    # Built In: the real link is hidden, URL-encoded, inside an Amazon tracking link:
    #   https://....awstrack.me/L0/https:%2F%2Fbuiltin.com%2Fjob%2F<slug>%2F<id>%3Fi=.../1/...
    if "awstrack.me" in host and "/L0/" in url:
        inner = unquote(url.split("/L0/", 1)[1].split("/", 1)[0])
        url = inner.split("?", 1)[0]
        parsed = urlparse(url)
        host = parsed.netloc.lower()

    match = re.search(r"/job/[^/]+/(\d+)", parsed.path)
    if "builtin.com" in host and match:
        return url.split("?", 1)[0], match.group(1)

    # Dice: already clean, the ID is the last part of the path.
    match = re.search(r"/job-detail/([0-9a-f-]+)", parsed.path)
    if "dice.com" in host and match:
        return url, match.group(1)

    return url, ""


# ---------------------------------------------------------------------------
# Step 3: ask Claude
# ---------------------------------------------------------------------------

# The exact JSON shape Claude must return. Think of it as a table definition:
# one row per job, with these columns and types. "null" means "not in the email".
JOB_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "title": {"type": "string"},
                    "location": {"type": "string"},
                    "remote": {"type": "string", "enum": ["yes", "no", "hybrid", "unknown"]},
                    "salary_min": {"type": ["integer", "null"]},
                    "salary_max": {"type": ["integer", "null"]},
                    "link_label": {"type": ["string", "null"]},
                },
                "required": [
                    "company", "title", "location", "remote",
                    "salary_min", "salary_max", "link_label",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["jobs"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You extract job listings from a job alert email.

Return every job posting listed in the email, one object per job. Skip ads for
the job board itself, surveys, "see all jobs" links, footer links, and the
summary of the saved search or job preferences at the top of the email.

Rules:
- company, title, location: copy as written in the email. Use "" if missing.
  If the only location given is "Remote", use "Remote".
- remote: "yes" if the job says remote, "hybrid" if it says hybrid,
  "no" if it says on-site or in-person, otherwise "unknown".
  A location of just a country (like "United States") alone is "unknown".
- salary_min / salary_max: yearly US dollars as whole numbers.
  Convert hourly pay to yearly by multiplying by 2080.
  If only one number is given, use it for both. null if no salary is shown.
- link_label: the label (like "L7") of the link that opens that job's page.
  null if the job has no link of its own."""


def extract_jobs(client, model, email_text, source):
    """
    Send one email to Claude and get back a list of job dictionaries.
    "source" is the job board name (e.g. "LinkedIn"), added to every job.
    """
    labeled_text, links = label_links(email_text)

    response = client.messages.create(
        model=model,
        max_tokens=16000,
        output_config={
            "effort": "low",  # simple copying task, no deep thinking needed
            "format": {"type": "json_schema", "schema": JOB_SCHEMA},
        },
        system=INSTRUCTIONS,
        messages=[{"role": "user", "content": labeled_text}],
    )

    # The JSON answer is in the first text block of the response.
    text = next(block.text for block in response.content if block.type == "text")
    jobs = json.loads(text)["jobs"]

    # Safety net: a real job posting always names a company.
    jobs = [job for job in jobs if job["company"].strip()]

    # Swap labels back to real, cleaned links.
    for job in jobs:
        raw_link = links.get(job.pop("link_label") or "", "")
        job["link"], job["job_id"] = clean_link(raw_link) if raw_link else ("", "")
        job["source"] = source

    return jobs, response.usage


def source_from_sender(sender):
    """Turn a sender address into a short job board name."""
    sender = sender.lower()
    for domain, name in (
        ("linkedin.com", "LinkedIn"),
        ("indeed.com", "Indeed"),
        ("builtin.com", "Built In"),
        ("dice.com", "Dice"),
        ("wellfound.com", "Wellfound"),
    ):
        if domain in sender:
            return name
    return "Other"


# ---------------------------------------------------------------------------
# Test on saved sample emails
# ---------------------------------------------------------------------------

def main():
    """Run extraction on every sample alert email and print what came out."""
    sys.stdout.reconfigure(encoding="utf-8")
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_FOLDER, ".env"))

    config = load_config()
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    sample_folder = os.path.join(PROJECT_FOLDER, "tests", "sample_emails")

    samples = {
        "linkedin_alert": "LinkedIn",
        "indeed_alert": "Indeed",
        "indeed_match": "Indeed",
        "builtin_alert": "Built In",
        "dice_alert": "Dice",
    }

    total_in = total_out = 0
    for name, source in samples.items():
        bodies = {}
        for kind, extension in (("plain", "txt"), ("html", "html")):
            path = os.path.join(sample_folder, f"{name}.{extension}")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    bodies[kind] = f.read()

        jobs, usage = extract_jobs(client, config["models"]["extract"], get_email_text(bodies), source)
        total_in += usage.input_tokens
        total_out += usage.output_tokens

        print(f"\n=== {name}: {len(jobs)} jobs ===")
        for job in jobs:
            pay = f"${job['salary_min']:,}-${job['salary_max']:,}" if job["salary_min"] else "no salary"
            print(f"- {job['title']} | {job['company']} | {job['location']} | remote={job['remote']} | {pay}")
            print(f"    {job['link'] or '(no link)'}   id={job['job_id'] or '-'}")

    # Haiku 5.5 price: $0.10 per million input tokens, $0.50 per million output tokens.
    cost = total_in / 1e6 * 0.10 + total_out / 1e6 * 0.50
    print(f"\nTokens: {total_in:,} in, {total_out:,} out. Cost: ${cost:.4f}")


if __name__ == "__main__":
    main()
