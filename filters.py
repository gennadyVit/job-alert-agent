"""
filters.py
Stage 4: decide which new jobs are worth looking at.

Every job gets three new fields:
    filter_reason  ""  (passes), "too senior", or "unrelated"
    salary_flag    "", "Below $120K target", or "Below $100K floor"
                   (never dropped; the digest lists "Below $100K floor" jobs
                   at the end, and only if they score well)
    remote_group   "Remote", "Onsite/Hybrid", or "Unknown"
                   (Unknown is checked again in stage 5 using the full description)

All rules and word lists live in config.yaml under "filters".
"""

import json
import re
from collections import Counter


def has_any_word(text, words):
    """
    True if text contains any of the words as whole words, ignoring case.
    "Staff" matches "Staff Data Engineer" but not "Staffing Coordinator".
    \\b in a regular expression means "word boundary".
    """
    pattern = r"\b(" + "|".join(re.escape(w) for w in words) + r")\b"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def remote_group(job):
    """Turn the remote field from stage 2 into one of three groups."""
    if job["remote"] == "yes":
        return "Remote"
    if job["remote"] in ("no", "hybrid"):
        return "Onsite/Hybrid"
    return "Unknown"


def top_salary(job):
    """Best possible pay for the job, or None if no salary was posted."""
    return job.get("salary_max") or job.get("salary_min")


def salary_flag(job, floor, target):
    """Flag jobs whose best possible pay is under the floor or target. No salary = no flag."""
    top = top_salary(job)
    if top and top < floor:
        return f"Below ${floor // 1000}K floor"
    if top and top < target:
        return f"Below ${target // 1000}K target"
    return ""


# ---------------------------------------------------------------------------
# Claude check for unrelated titles (one call for all jobs)
# ---------------------------------------------------------------------------

RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "unrelated": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "title": {"type": "string"},
                },
                "required": ["number", "title"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["unrelated"],
    "additionalProperties": False,
}


def find_unrelated(client, model, jobs, target_roles):
    """
    Send all job titles to Claude in one numbered list.
    Returns the set of list positions (0-based) that are clearly unrelated.

    Claude returns each unrelated job's number AND its title. We only accept
    an answer when the title matches the job at that number, so a wrong
    number can never filter out the wrong job.
    """
    if not jobs:
        return set()

    roles = "\n".join(f"- {role}" for role in target_roles)
    titles = "\n".join(f"{n}. {job['title']} at {job['company']}" for n, job in enumerate(jobs, 1))

    response = client.messages.create(
        model=model,
        max_tokens=4000,
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": RELEVANCE_SCHEMA},
        },
        system=(
            "A job seeker targets these kinds of roles:\n" + roles + "\n\n"
            "You get a numbered list of job titles. Return the numbers of titles "
            "that are CLEARLY unrelated to all of the target roles (a different field "
            "or profession), with each title copied exactly.\n"
            "Anything involving data, data warehouses, ETL, databases, analytics, "
            "reporting, BI, AI, or technical program/product management is related. "
            "If a title could reasonably fit, it is related. When in doubt, it is related."
        ),
        messages=[{"role": "user", "content": titles}],
    )
    text = next(block.text for block in response.content if block.type == "text")

    positions = set()
    for item in json.loads(text)["unrelated"]:
        position = item["number"] - 1
        # Accept only if the number points at a job with the same title.
        # (Claude sometimes copies the whole line, "Title at Company", so we
        # check that the returned text STARTS with the job's title.)
        if 0 <= position < len(jobs) and item["title"].strip().startswith(jobs[position]["title"].strip()):
            positions.add(position)
    return positions


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def apply_filters(jobs, client, config):
    """
    Label every job (see top of file). Returns (kept_jobs, reason_counts).
    reason_counts is a Counter like {"too senior": 4, "unrelated": 11}.
    """
    rules = config["filters"]

    # Free checks first.
    for job in jobs:
        job["remote_group"] = remote_group(job)
        job["salary_flag"] = salary_flag(job, rules["salary_floor"], rules["salary_target"])
        if has_any_word(job["title"], rules["too_senior_words"]):
            job["filter_reason"] = "too senior"
        elif has_any_word(job["title"], rules["unrelated_keywords"]):
            job["filter_reason"] = "unrelated"
        else:
            job["filter_reason"] = ""

    # One Claude call for whatever is still in.
    still_in = [job for job in jobs if not job["filter_reason"]]
    for position in find_unrelated(client, config["models"]["extract"], still_in, rules["target_roles"]):
        still_in[position]["filter_reason"] = "unrelated"

    kept = [job for job in jobs if not job["filter_reason"]]
    reasons = Counter(job["filter_reason"] for job in jobs if job["filter_reason"])
    return kept, reasons
