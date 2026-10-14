"""
digest.py
Stage 7: build the digest email and send it to myself.

Sections (thresholds in config.yaml under "digest"):
  1. Apply today      remote/unknown jobs, strong experience AND preferences fit
  2. Maybe            remote/unknown jobs, decent experience fit
  3. Onsite or Hybrid same rules, kept in their own table (never mixed with remote)
  4. Below $100K      only if experience fit is high (config: below_floor_min_fit)
  5. Filtered out     counts only

Plain black-and-white HTML: no colors.
"""

import base64
import datetime as dt
import json
from email.mime.text import MIMEText
from html import escape

from gmail_client import GMAIL_RETRIES

STYLE = """
<style>
  body  { font-family: Arial, sans-serif; color: #000; background: #fff; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 24px; }
  th, td { border: 1px solid #000; padding: 6px; text-align: left; vertical-align: top; font-size: 13px; }
  th    { font-weight: bold; }
  .take { font-style: italic; }
</style>
"""


def money(value):
    """130000 -> '$130K'"""
    return f"${round(value / 1000)}K" if value else ""


def pay_text(job):
    """Posted range, or Claude's estimate marked '(est.)', plus the number to ask for."""
    low, high = job.get("salary_range_low"), job.get("salary_range_high")
    if not high:
        low, high = job.get("salary_min"), job.get("salary_max")
    text = f"{money(low)}-{money(high)}" if high else "unknown"
    if job.get("salary_is_estimate"):
        text += " (est.)"
    if job.get("salary_ask"):
        text += f"<br>ask {money(job['salary_ask'])}"
    return text


def job_table(jobs, show_gaps=False):
    """One table: a row of facts per job, then a row with the take (and gaps)."""
    if not jobs:
        return "<p>None this time.</p>"

    rows = []
    for job in jobs:
        location = escape(job["location"] or "")
        if job.get("remote_group") == "Unknown":
            location += "<br>(remote not confirmed)"
        link = job.get("description_url") or job.get("link") or ""
        rows.append(
            "<tr>"
            f"<td>{escape(job['company'])}</td>"
            f"<td>{escape(job['title'])}</td>"
            f"<td>{location}</td>"
            f"<td>{pay_text(job)}</td>"
            f"<td>{job['experience_fit']}%</td>"
            f"<td>{job['preferences_fit']}%</td>"
            f"<td>{escape(job['interview_likelihood'])}</td>"
            f"<td>{escape(job['python_risk'])}</td>"
            f"<td>{escape(job['best_resume'])}</td>"
            f"<td><a href=\"{escape(link)}\">open</a></td>"
            "</tr>"
        )
        notes = f"<span class='take'>{escape(job['take'])}</span>"
        flags = json.loads(job.get("flags") or "[]")
        if flags:
            notes += "<br>Notes: " + escape("; ".join(flags))
        if show_gaps:
            gaps = json.loads(job.get("top_gaps") or "[]")
            if gaps:
                notes += "<br>Gaps: " + escape("; ".join(gaps[:3]))
        rows.append(f"<tr><td colspan='10'>{notes}</td></tr>")

    header = ("<tr><th>Company</th><th>Role</th><th>Location</th><th>Pay</th>"
              "<th>Exp. fit</th><th>Pref. fit</th><th>Interview</th><th>Python risk</th>"
              "<th>Resume</th><th>Link</th></tr>")
    return f"<table>{header}{''.join(rows)}</table>"


def sort_best_first(jobs):
    return sorted(jobs, key=lambda j: (j["experience_fit"], j["preferences_fit"]), reverse=True)


def build_digest(jobs, filtered_counts, config, run_date=None):
    """
    jobs: this run's kept, scored jobs. filtered_counts: {"reason": count}.
    Returns (subject, html).
    """
    rules = config["digest"]
    floor_min_fit = config["filters"]["below_floor_min_fit"]
    scored = [j for j in jobs if j.get("experience_fit") is not None]

    below_floor = [j for j in scored if j.get("salary_flag", "").startswith("Below $100K")]
    normal = [j for j in scored if j not in below_floor]

    def is_apply(j):
        return (j["experience_fit"] >= rules["apply_experience_min"]
                and j["preferences_fit"] >= rules["apply_preferences_min"])

    def is_maybe(j):
        return not is_apply(j) and j["experience_fit"] >= rules["maybe_experience_min"]

    remote = [j for j in normal if j.get("remote_group") != "Onsite/Hybrid"]
    onsite = [j for j in normal if j.get("remote_group") == "Onsite/Hybrid"]

    apply_today = sort_best_first([j for j in remote if is_apply(j)])
    maybe = sort_best_first([j for j in remote if is_maybe(j)])
    onsite_list = sort_best_first([j for j in onsite if is_apply(j) or is_maybe(j)])
    floor_list = sort_best_first([j for j in below_floor if j["experience_fit"] >= floor_min_fit])

    shown = len(apply_today) + len(maybe) + len(onsite_list) + len(floor_list)
    counts = dict(filtered_counts)
    counts["scored too low"] = len(scored) - shown
    total_filtered = sum(counts.values())

    run_date = run_date or dt.date.today()
    subject = (f"Job Digest, {run_date:%b %d, %Y}: {len(apply_today)} strong, "
               f"{len(maybe)} maybe, {total_filtered} filtered out")

    filtered_rows = "".join(
        f"<tr><td>{escape(reason)}</td><td>{count}</td></tr>"
        for reason, count in sorted(counts.items(), key=lambda kv: -kv[1]) if count
    )

    html = (
        f"<html><head>{STYLE}</head><body>"
        f"<h2>{escape(subject)}</h2>"
        f"<h3>1. Apply today ({len(apply_today)})</h3>"
        f"<p>Remote or not confirmed; experience fit {rules['apply_experience_min']}%+ "
        f"and preferences fit {rules['apply_preferences_min']}%+.</p>"
        f"{job_table(apply_today)}"
        f"<h3>2. Maybe ({len(maybe)})</h3>"
        f"<p>Remote or not confirmed; experience fit {rules['maybe_experience_min']}%+.</p>"
        f"{job_table(maybe, show_gaps=True)}"
        f"<h3>3. Onsite or Hybrid ({len(onsite_list)})</h3>"
        f"{job_table(onsite_list, show_gaps=True)}"
        f"<h3>4. Below $100K, strong fit ({len(floor_list)})</h3>"
        f"<p>Experience fit {floor_min_fit}%+ only. Worth discussing.</p>"
        f"{job_table(floor_list, show_gaps=True)}"
        f"<h3>5. Filtered out ({total_filtered})</h3>"
        f"<table><tr><th>Reason</th><th>Jobs</th></tr>{filtered_rows}</table>"
        "</body></html>"
    )
    return subject, html


def send_digest(gmail, subject, html):
    """Send the email from my Gmail to myself."""
    me = gmail.users().getProfile(userId="me").execute(num_retries=GMAIL_RETRIES)["emailAddress"]
    message = MIMEText(html, "html", "utf-8")
    message["To"] = me
    message["From"] = me
    message["Subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute(num_retries=GMAIL_RETRIES)
