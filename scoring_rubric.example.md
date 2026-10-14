# Job Scoring Rubric (example)

Copy this file to `scoring_rubric.md` (git-ignored) and write your own.
The pipeline sends it to Claude together with every resume in `resumes/`.
The output fields are fixed in `score.py`; this file only says how to judge.

## Output

1. **Company:** one line.
2. **Experience fit %:** against any resume version, scored as interview
   probability, not skill overlap. State your strong and weak skills here.
   Gaps go in bullets.
3. **Interview likelihood:** very low to very high (see below).
4. **Preferences fit %:** your priorities, most important first
   (e.g. remote, management path, culture; then salary, benefits, company size).
5. **Target salary:** if none is posted, a conservative estimate plus a number to ask for.
6. **Take:** two sentences, including anything unusual.
7. **Resume version:** which resume to send.
8. **Improvements:** if experience fit is 70%+ and can be improved, list how.

## Interview likelihood

A judgment rating. List what moves it down for you, e.g. hard credential gaps
(years, certifications, clearance), likely live-coding rounds, stale or
heavily applied postings, seniority you haven't held.
