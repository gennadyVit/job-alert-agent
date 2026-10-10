"""
main.py
Runs the whole pipeline once. This is the file you run:
    python main.py

Current version: stages 1-3 (read alerts, extract jobs, dedupe, save, status).
Filters, descriptions, scoring, and the digest email get added in steps 4-7.
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import anthropic
from dotenv import load_dotenv

from dedupe import remove_duplicates
from extract import extract_jobs, get_email_text, source_from_sender
from gmail_client import (
    PROJECT_FOLDER, build_alert_query, get_gmail_service,
    get_message_bodies, list_message_ids, load_config,
)
from status import find_status_events
from store import Store

DAY = 24 * 3600  # seconds in a day


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(os.path.join(PROJECT_FOLDER, ".env"))

    config = load_config()
    gmail = get_gmail_service()
    claude = anthropic.Anthropic()
    store = Store(config)

    # Remember when this run started. The next run picks up from here, so an
    # email that arrives while this run is working is caught next time.
    run_start = time.time()

    # 1. Keep the BigQuery tables from expiring (sandbox rule).
    expires = store.keep_tables_alive()
    print(f"Tables kept alive until {expires:%Y-%m-%d}")

    # 2. Where did the last run leave off?
    last_run = store.get_last_run_time()
    if last_run:
        alert_since = status_since = last_run
        print(f"Last run: {time.strftime('%Y-%m-%d %H:%M', time.localtime(last_run))}")
    else:
        alert_since = run_start - config["first_run_lookback_days"] * DAY
        status_since = run_start - config["first_run_status_lookback_days"] * DAY
        print("First run.")

    # 3. Status emails first, so jobs you applied to are known before
    #    we look at new alerts.
    events, status_emails_read = find_status_events(
        gmail, claude, config, status_since, store.get_processed_email_ids()
    )
    store.append_rows("status_events", events)
    print(f"Status emails: {status_emails_read} read, {len(events)} were applications, rejections, or interviews")

    # 4. Read alert emails and extract jobs (Claude reads 8 emails at a time).
    query = build_alert_query(config["alert_senders"], alert_since)
    emails = [get_message_bodies(gmail, m) for m in list_message_ids(gmail, query)]

    def extract(email):
        jobs, _usage = extract_jobs(
            claude, config["models"]["extract"],
            get_email_text(email), source_from_sender(email["from"]),
        )
        for job in jobs:
            job["email_id"] = email["id"]
            job["date_found"] = run_start
        return jobs

    with ThreadPoolExecutor(max_workers=8) as pool:
        all_jobs = [job for jobs in pool.map(extract, emails) for job in jobs]
    print(f"Alert emails: {len(emails)} read, {len(all_jobs)} jobs found")

    # 5. Drop jobs we've seen before or already applied to, then save the rest.
    new_jobs, skipped = remove_duplicates(all_jobs, store.get_known_job_keys())
    store.append_rows("jobs", new_jobs)
    print(f"New jobs: {len(new_jobs)}  (skipped {skipped} already seen or applied)")

    # 6. Log this run.
    store.append_rows("runs", [{
        "run_time": run_start,
        "alert_emails_read": len(emails),
        "jobs_found": len(all_jobs),
        "jobs_new": len(new_jobs),
        "status_emails_read": status_emails_read,
        "status_events_found": len(events),
    }])

    print(f"Done in {time.time() - run_start:.0f} seconds.")


if __name__ == "__main__":
    main()
