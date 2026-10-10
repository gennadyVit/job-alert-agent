"""
dedupe.py
Stage 3: decide which jobs are new.

Every job gets a "job_key": company + title, cleaned up so small
differences don't matter. For example, these all become the same key:
    "Acme, Inc."  + "Sr. Data Engineer"
    "ACME"        + "Senior Data Engineer"
    -> "acme|senior data engineer"
"""

import re

# Words at the end of company names that don't change who the company is.
COMPANY_SUFFIXES = {
    "inc", "llc", "ltd", "corp", "corporation", "co", "company",
    "lp", "llp", "plc", "gmbh", "careers", "group",
}

# Short forms in job titles, turned into the full word.
TITLE_WORDS = {
    "sr": "senior",
    "jr": "junior",
    "mgr": "manager",
    "eng": "engineer",
    "dir": "director",
}


def clean_words(text):
    """Lowercase, turn punctuation into spaces, and split into words."""
    text = text.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).split()


def normalize_company(company):
    """'Acme, Inc.' -> 'acme'.  Drops suffix words from the end only."""
    words = clean_words(company)
    while len(words) > 1 and words[-1] in COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def normalize_title(title):
    """'Sr. Data Engineer (Remote)' -> 'senior data engineer remote'."""
    return " ".join(TITLE_WORDS.get(word, word) for word in clean_words(title))


def make_job_key(company, title):
    """The ID we use to recognize the same job across emails and runs."""
    return f"{normalize_company(company)}|{normalize_title(title)}"


def remove_duplicates(jobs, known_keys):
    """
    Keep only jobs we have never seen.
    - known_keys: a set of job_keys already in BigQuery (seen or applied to).
    - Also drops repeats inside this batch (the same job in two emails).
    Adds "job_key" and "company_key" to every job.
    Returns (new_jobs, number_skipped).
    """
    new_jobs = []
    seen_now = set()

    for job in jobs:
        job["company_key"] = normalize_company(job["company"])
        job["job_key"] = make_job_key(job["company"], job["title"])

        if job["job_key"] in known_keys or job["job_key"] in seen_now:
            continue
        seen_now.add(job["job_key"])
        new_jobs.append(job)

    return new_jobs, len(jobs) - len(new_jobs)
