"""
status.py
Stage 3 (backup check): read emails that change a job's status.

1. Search Gmail for the phrases in config.yaml (status_phrases).
2. Ask Claude (Haiku) what each email is: an application confirmation,
   a rejection, an interview invite, or something else.
3. Return one "event" per real status email, ready to save in BigQuery.
"""

import json
from concurrent.futures import ThreadPoolExecutor

from dedupe import make_job_key, normalize_company
from extract import get_email_text
from gmail_client import get_message_bodies, list_message_ids

# We only send the start of each email to Claude; the important part is
# always near the top, and it keeps the cost down.
MAX_EMAIL_CHARS = 4000

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "event": {"type": "string", "enum": ["Applied", "Rejected", "Interviewing", "Other"]},
        "company": {"type": "string"},
        "title": {"type": ["string", "null"]},
    },
    "required": ["event", "company", "title"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You classify one email from a job seeker's inbox.

event:
- "Applied": confirms the person submitted an application.
- "Rejected": says the person will not move forward for a role.
- "Interviewing": invites the person to an interview, phone screen, or
  assessment, or asks to schedule one.
- "Other": anything else (job alerts, newsletters, recruiter spam,
  marketing, generic "next steps" that are not about their application).

company: the hiring company (not the job board or the applicant tracking
system like Workday or Greenhouse). "" if unknown.

title: the job title the email is about, or null if the email doesn't name one.
Some confirmation emails also list "similar jobs"; ignore those, use only
the job the person applied to."""


def build_status_query(phrases, alert_senders, since_timestamp):
    """
    Gmail search for any of the phrases, excluding job alert emails
    and emails you sent yourself.
    """
    phrase_list = " OR ".join(f'"{p}"' for p in phrases)
    sender_list = " OR ".join(alert_senders)
    return f"({phrase_list}) -from:({sender_list}) -in:sent after:{int(since_timestamp)}"


def classify_email(client, model, email):
    """Ask Claude what one email is. Returns a dict: event, company, title."""
    text = get_email_text(email)[:MAX_EMAIL_CHARS]
    content = f"From: {email['from']}\nSubject: {email['subject']}\n\n{text}"

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": EVENT_SCHEMA},
        },
        system=INSTRUCTIONS,
        messages=[{"role": "user", "content": content}],
    )
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


def find_status_events(gmail, claude, config, since_timestamp, skip_email_ids):
    """
    Find and classify status emails since a point in time.
    skip_email_ids: emails already saved in BigQuery (not sent to Claude again).
    Returns (events, number_of_emails_read).
    """
    query = build_status_query(
        config["status_phrases"], config["alert_senders"], since_timestamp
    )
    message_ids = [m for m in list_message_ids(gmail, query) if m not in skip_email_ids]

    # Download the emails one by one (Gmail's connection can't be shared
    # between threads), then classify them 8 at a time.
    emails = [get_message_bodies(gmail, message_id) for message_id in message_ids]

    def classify(email):
        return classify_email(claude, config["models"]["status"], email)

    # A thread pool runs the function on several emails at once.
    # .map() returns the results in the same order as the emails.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(classify, emails))

    events = []
    for email, result in zip(emails, results):
        message_id = email["id"]
        if result["event"] == "Other" or not result["company"].strip():
            continue

        title = (result["title"] or "").strip()
        events.append({
            "email_id": message_id,
            "email_date": email["timestamp"],
            "event": result["event"],
            "company": result["company"],
            "title": title,
            "company_key": normalize_company(result["company"]),
            "job_key": make_job_key(result["company"], title) if title else "",
            "sender": email["from"],
            "subject": email["subject"],
        })

    return events, len(message_ids)
