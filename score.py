"""
score.py
Stage 6: score each job with Claude Sonnet, using my rubric and resumes.

What Claude gets for every job:
  - system prompt: scoring_rubric.md + every resume in resumes/ (the same for
    every job, so it is CACHED: after the first job, re-reading it costs ~90% less)
  - message: the job's email details + the criteria found in stage 5

What comes back: the scores and notes defined in SCORE_SCHEMA below.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor

from gmail_client import PROJECT_FOLDER
from resumes import load_resumes

LIKELIHOOD = ["very low", "low", "medium", "high", "very high"]


def build_system_prompt():
    """Rubric + all resumes, read fresh from disk on every run."""
    with open(os.path.join(PROJECT_FOLDER, "scoring_rubric.md"), encoding="utf-8") as f:
        rubric = f.read()
    resumes = load_resumes()
    resume_text = "\n\n".join(
        f"=== RESUME VERSION: {name} ===\n{text}" for name, text in resumes.items()
    )
    prompt = (
        "You evaluate job postings for one job seeker, following their rubric exactly.\n\n"
        f"{rubric}\n\n"
        "Notes for this automated version:\n"
        "- You get facts extracted from the posting, not the full text. If the posting "
        "wasn't found (found=false), score from what you have and say so in the take.\n"
        "- Lists (gaps, flags, improvements) are short phrases.\n"
        "- python_risk: Low, Medium, or High chance of a live Python coding interview.\n\n"
        f"The job seeker's resumes:\n\n{resume_text}"
    )
    return prompt, list(resumes)


def score_schema(resume_names):
    """The answer shape. best_resume can only be one of the resume files."""
    text_list = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "company_summary": {"type": "string"},
            "experience_fit": {"type": "integer"},
            "top_gaps": text_list,
            "interview_likelihood": {"type": "string", "enum": LIKELIHOOD},
            "python_risk": {"type": "string", "enum": ["Low", "Medium", "High"]},
            "preferences_fit": {"type": "integer"},
            "salary_is_estimate": {"type": "boolean"},
            "salary_range_low": {"type": ["integer", "null"]},
            "salary_range_high": {"type": ["integer", "null"]},
            "salary_ask": {"type": ["integer", "null"]},
            "take": {"type": "string"},
            "flags": text_list,
            "best_resume": {"type": "string", "enum": resume_names},
            "resume_improvements": text_list,
        },
        "required": [
            "company_summary", "experience_fit", "top_gaps", "interview_likelihood",
            "python_risk", "preferences_fit", "salary_is_estimate", "salary_range_low",
            "salary_range_high", "salary_ask", "take", "flags", "best_resume",
            "resume_improvements",
        ],
        "additionalProperties": False,
    }


def job_message(job):
    """What we tell Claude about one job."""
    salary = (f"${job['salary_min']:,} - ${job['salary_max']:,}"
              if job.get("salary_max") else "not in the alert email")
    criteria = job.get("criteria") or "{}"
    return (f"Company: {job['company']}\nTitle: {job['title']}\n"
            f"Location: {job['location']}\nSource: {job['source']}\n"
            f"Salary from alert: {salary}\n\n"
            f"Facts extracted from the posting (JSON):\n{criteria}")


def score_job(client, model, system_prompt, schema, job):
    """Score one job. Returns (scores_dict or None, usage)."""
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        # The system prompt is the same for every job; cache_control marks it
        # for caching so later jobs read it from the cache.
        system=[{"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": job_message(job)}],
    )
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text), response.usage


def score_jobs(jobs, client, config):
    """
    Score every job and copy the results into it. Lists are stored as JSON text.
    Returns cost totals for the run.
    """
    if not jobs:
        return {"cost": 0.0}

    model = config["models"]["score"]
    system_prompt, resume_names = build_system_prompt()
    schema = score_schema(resume_names)

    def safe_score(job):
        try:
            return score_job(client, model, system_prompt, schema, job)
        except Exception as error:  # one bad job shouldn't stop the run
            print(f"  scoring failed for {job['company']} / {job['title']}: {str(error)[:150]}")
            return None, None

    # Score the first job alone so it writes the cache, then the rest 8 at a time
    # (otherwise 8 parallel calls would all pay full price to write the same cache).
    results = [safe_score(jobs[0])]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results += list(pool.map(safe_score, jobs[1:]))

    # Sonnet 5.5 prices per million tokens: $2 input, $10 output,
    # $0.20 cache read, $2.50 cache write (1.25x input).
    cost = 0.0
    for job, (scores, usage) in zip(jobs, results):
        if usage:
            cost += (usage.input_tokens * 2 + usage.output_tokens * 10
                     + (usage.cache_read_input_tokens or 0) * 0.20
                     + (usage.cache_creation_input_tokens or 0) * 2.50) / 1e6
        if not scores:
            continue
        for key in ("company_summary", "experience_fit", "interview_likelihood",
                    "python_risk", "preferences_fit", "salary_is_estimate",
                    "salary_range_low", "salary_range_high", "salary_ask",
                    "take", "best_resume"):
            job[key] = scores[key]
        for key in ("top_gaps", "flags", "resume_improvements"):
            job[key] = json.dumps(scores[key])

    return {"cost": cost, "scored": sum(1 for s, _ in results if s)}
