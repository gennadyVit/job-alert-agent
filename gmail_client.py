"""
gmail_client.py
Everything that talks to Gmail lives in this file.

Step 1 goal: find the job alert emails from the last 24 hours
and print their subjects.

Run it with:
    python gmail_client.py
"""

import base64
import os
import sys
import time

import yaml
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# "Scopes" are the permissions we ask Google for.
# Gmail: read and send only. BigQuery: where we store every job we've seen.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/bigquery",
]

# Gmail limits how many requests you can make per minute. When we hit the
# limit, Google's library waits (1s, 2s, 4s, ... up to about 2 minutes total)
# and tries again, up to this many times.
GMAIL_RETRIES = 7

# The folder this file lives in, so config.yaml is found
# no matter which folder you run the script from.
PROJECT_FOLDER = os.path.dirname(os.path.abspath(__file__))


def load_config():
    """Read config.yaml and return it as a Python dictionary."""
    config_path = os.path.join(PROJECT_FOLDER, "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_google_credentials():
    """
    Build a Google login object from three secrets.
    Locally they come from the .env file. On GitHub they come from
    GitHub Secrets. Either way, Python reads them as environment variables.
    """
    load_dotenv(os.path.join(PROJECT_FOLDER, ".env"))

    # token=None means "no access token yet". Google's library uses the
    # refresh token to get a fresh access token automatically on the first call.
    return Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )


def get_gmail_service():
    """Return a Gmail API connection we can call methods on."""
    credentials = get_google_credentials()
    return build("gmail", "v1", credentials=credentials)


def build_alert_query(senders, since_timestamp):
    """
    Build a Gmail search string, the same kind you type in the Gmail search box.
    Example result:
        from:(a@x.com OR b@y.com) after:1759800000
    "after:" accepts a Unix timestamp (seconds since 1970), so we can say
    "everything since the exact moment of the last run".
    """
    sender_list = " OR ".join(senders)
    return f"from:({sender_list}) after:{int(since_timestamp)}"


def list_message_ids(service, query):
    """
    Return the IDs of every email matching the search query.
    Gmail returns results in pages (up to 100 at a time), so we keep
    asking for the next page until there isn't one. Think of it like
    paging through a large SQL result set.
    """
    message_ids = []
    page_token = None

    while True:
        response = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=100
        ).execute(num_retries=GMAIL_RETRIES)

        # "messages" is missing from the response when there are no matches,
        # so .get(..., []) gives us an empty list instead of an error.
        for message in response.get("messages", []):
            message_ids.append(message["id"])

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return message_ids


def get_message_headers(service, message_id):
    """
    Fetch one email's Subject, From, and Date (headers only, not the body).
    Returns a dictionary like {"id": ..., "subject": ..., "from": ..., "date": ...}.
    """
    message = service.users().messages().get(
        userId="me",
        id=message_id,
        format="metadata",
        metadataHeaders=["Subject", "From", "Date"],
    ).execute(num_retries=GMAIL_RETRIES)

    # Headers come back as a list of {"name": ..., "value": ...} pairs.
    # Turn that into a simple dictionary keyed by lowercase header name.
    headers = {}
    for header in message["payload"]["headers"]:
        headers[header["name"].lower()] = header["value"]

    return {
        "id": message_id,
        "subject": headers.get("subject", "(no subject)"),
        "from": headers.get("from", ""),
        "date": headers.get("date", ""),
    }


def decode_body_data(data):
    """
    Gmail sends email text encoded as "base64url" (letters and digits only).
    This turns it back into normal readable text.
    The "=" padding line fixes a quirk: Python's decoder wants the length
    to be a multiple of 4, and Gmail sometimes leaves the padding off.
    """
    data = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def collect_text_parts(part, found):
    """
    An email is built like folders inside folders: a part can contain more parts.
    This function looks at one part, keeps it if it is plain text or HTML,
    then calls itself on each sub-part to look deeper (this is called recursion).
    "found" is a dictionary we fill in as we go, e.g. {"text/plain": "...", "text/html": "..."}.
    """
    mime_type = part.get("mimeType", "")
    data = part.get("body", {}).get("data")

    # Keep only the first plain text part and the first HTML part we see.
    if data and mime_type in ("text/plain", "text/html") and mime_type not in found:
        found[mime_type] = decode_body_data(data)

    for sub_part in part.get("parts", []):
        collect_text_parts(sub_part, found)


def get_message_bodies(service, message_id):
    """
    Fetch one email's full content.
    Returns a dictionary:
        id, subject, from, timestamp (seconds since 1970),
        plain, html  (either can be "" if the sender didn't include that
                      version; Built In, for example, sends HTML only)
    """
    message = service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute(num_retries=GMAIL_RETRIES)

    found = {}
    collect_text_parts(message["payload"], found)

    headers = {h["name"].lower(): h["value"] for h in message["payload"]["headers"]}

    return {
        "id": message_id,
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        # Gmail gives the received time in milliseconds; we use seconds.
        "timestamp": int(message["internalDate"]) / 1000,
        "plain": found.get("text/plain", ""),
        "html": found.get("text/html", ""),
    }


def main():
    """Step 1 test: print the subject of every alert email since the first-run cutoff."""
    # The Windows terminal can't print emojis by default (job emails use them).
    # This tells Python to print using UTF-8, which handles any character.
    sys.stdout.reconfigure(encoding="utf-8")

    config = load_config()
    service = get_gmail_service()

    # For now, always look back the first-run window. Step 3 replaces this
    # with "the time of the last run", read from the Google Sheet.
    since = time.time() - config["first_run_lookback_days"] * 24 * 3600
    query = build_alert_query(config["alert_senders"], since)
    print(f"Gmail search: {query}\n")

    message_ids = list_message_ids(service, query)
    print(f"Found {len(message_ids)} alert emails.\n")

    for message_id in message_ids:
        info = get_message_headers(service, message_id)
        print(f"{info['date']}  |  {info['from']}")
        print(f"    {info['subject']}\n")


# This line means: only run main() when you run this file directly,
# not when another file (like main.py later) imports it.
if __name__ == "__main__":
    main()
