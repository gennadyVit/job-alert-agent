"""
store.py
Everything that talks to BigQuery lives here.

Three tables in the job_alerts dataset (think of them like SQL tables in Snowflake):
    jobs           one row per job we've seen in an alert email
    status_events  one row per application / rejection / interview email
    runs           one row per run of the script

Two BigQuery sandbox rules shape this file:
1. Tables are deleted 60 days after their "expires" date. Every run pushes that
   date out to 60 days from now (keep_tables_alive). Run at least every 60 days.
2. Sandbox doesn't allow inserting rows one at a time ("streaming") or UPDATE.
   So we only ever ADD rows, using a "load job" (a batch insert).
   A job's status is never stored; it's calculated by a query (get_job_statuses).
"""

import datetime as dt

from google.cloud import bigquery

from gmail_client import get_google_credentials

# Table definitions: (column name, BigQuery type). NULLABLE unless noted.
TABLES = {
    "jobs": [
        ("job_key", "STRING"),
        ("company_key", "STRING"),
        ("date_found", "TIMESTAMP"),
        ("source", "STRING"),
        ("company", "STRING"),
        ("title", "STRING"),
        ("location", "STRING"),
        ("remote", "STRING"),
        ("salary_min", "INT64"),
        ("salary_max", "INT64"),
        ("link", "STRING"),
        ("job_id", "STRING"),
        ("email_id", "STRING"),
        # Filled in by stage 4 (filters).
        ("filter_reason", "STRING"),   # "" = passed, "too senior", "unrelated"
        ("salary_flag", "STRING"),
        ("remote_group", "STRING"),    # Remote, Onsite/Hybrid, Unknown
        # Filled in by step 6 (scoring).
        ("experience_fit", "INT64"),
        ("preferences_fit", "INT64"),
        ("python_risk", "STRING"),
        ("interview_likelihood", "STRING"),
        ("top_gaps", "STRING"),
        ("take", "STRING"),
        ("flags", "STRING"),
    ],
    "status_events": [
        ("email_id", "STRING"),
        ("email_date", "TIMESTAMP"),
        ("event", "STRING"),          # Applied, Rejected, or Interviewing
        ("company", "STRING"),
        ("title", "STRING"),          # empty when the email doesn't name the job
        ("company_key", "STRING"),
        ("job_key", "STRING"),        # empty when there's no title
        ("sender", "STRING"),
        ("subject", "STRING"),
    ],
    "runs": [
        ("run_time", "TIMESTAMP"),
        ("alert_emails_read", "INT64"),
        ("jobs_found", "INT64"),
        ("jobs_new", "INT64"),
        ("status_emails_read", "INT64"),
        ("status_events_found", "INT64"),
    ],
}


class Store:
    """
    A small wrapper around the BigQuery client.
    Create it once: store = Store(config). Then call store.<method>().
    """

    def __init__(self, config):
        self.project = config["bigquery"]["project"]
        self.dataset = config["bigquery"]["dataset"]
        self.client = bigquery.Client(project=self.project, credentials=get_google_credentials())

    def table_id(self, name):
        """'jobs' -> 'job-alert-agent-511001.job_alerts.jobs'"""
        return f"{self.project}.{self.dataset}.{name}"

    def query(self, sql):
        """Run a SQL query and return the rows as a list of dictionaries."""
        return [dict(row) for row in self.client.query(sql).result()]

    # -------------------------------------------------------------------
    # Setup
    # -------------------------------------------------------------------

    def keep_tables_alive(self):
        """
        Create the dataset and tables if they don't exist yet, then push each
        table's delete date to 60 days from now (the sandbox maximum).
        Returns the new delete date.
        """
        self.client.create_dataset(f"{self.project}.{self.dataset}", exists_ok=True)
        expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=60)

        for name, columns in TABLES.items():
            schema = [bigquery.SchemaField(col, typ) for col, typ in columns]
            table = self.client.create_table(
                bigquery.Table(self.table_id(name), schema=schema), exists_ok=True
            )

            # If we've added columns to TABLES since the table was created,
            # add them to the real table too (like ALTER TABLE ADD COLUMN).
            existing = {field.name for field in table.schema}
            missing = [field for field in schema if field.name not in existing]
            fields_to_update = ["expires"]
            if missing:
                table.schema = list(table.schema) + missing
                fields_to_update.append("schema")

            table.expires = expires
            self.client.update_table(table, fields_to_update)

        return expires

    # -------------------------------------------------------------------
    # Reading
    # -------------------------------------------------------------------

    def get_last_run_time(self):
        """Time of the last run (seconds since 1970), or None if never run."""
        rows = self.query(f"SELECT MAX(run_time) AS last FROM `{self.table_id('runs')}`")
        last = rows[0]["last"]
        return last.timestamp() if last else None

    def get_known_job_keys(self):
        """Every job_key we've already seen, plus every job you applied to."""
        rows = self.query(f"""
            SELECT job_key FROM `{self.table_id('jobs')}`
            UNION DISTINCT
            SELECT job_key FROM `{self.table_id('status_events')}`
            WHERE job_key IS NOT NULL AND job_key != ''
        """)
        return {row["job_key"] for row in rows}

    def get_processed_email_ids(self):
        """IDs of status emails already saved, so we never save one twice."""
        rows = self.query(f"SELECT DISTINCT email_id FROM `{self.table_id('status_events')}`")
        return {row["email_id"] for row in rows}

    def get_job_statuses(self):
        """
        Each job's current status, calculated from status_events.
        Returns a list of dicts: job_key, company, title, status, status_date.

        Matching rules:
        - An event that names the job (job_key) matches that exact job.
        - An event that only names the company matches every job at that
          company that we found BEFORE the email arrived.
        The latest matching event wins. No match means "New".
        """
        return self.query(f"""
            WITH matches AS (
                SELECT j.job_key, e.event, e.email_date
                FROM `{self.table_id('jobs')}` AS j
                JOIN `{self.table_id('status_events')}` AS e
                  ON e.job_key = j.job_key
                  OR ( (e.job_key IS NULL OR e.job_key = '')
                       AND e.company_key = j.company_key
                       AND e.email_date >= j.date_found )
            ),
            latest AS (
                SELECT job_key, event, email_date
                FROM matches
                QUALIFY ROW_NUMBER() OVER (PARTITION BY job_key ORDER BY email_date DESC) = 1
            )
            SELECT j.job_key, j.company, j.title,
                   COALESCE(l.event, 'New') AS status,
                   l.email_date AS status_date
            FROM `{self.table_id('jobs')}` AS j
            LEFT JOIN latest AS l USING (job_key)
            ORDER BY j.date_found DESC
        """)

    # -------------------------------------------------------------------
    # Writing
    # -------------------------------------------------------------------

    def append_rows(self, table_name, rows):
        """
        Add rows to a table with a load job (a batch insert, allowed in sandbox).
        Each row is a dictionary; keys that aren't table columns are ignored.
        """
        if not rows:
            return
        columns = TABLES[table_name]
        clean_rows = []
        for row in rows:
            clean = {}
            for col, typ in columns:
                value = row.get(col)
                # BigQuery wants timestamps as text like "2026-10-09T12:00:00+00:00".
                if typ == "TIMESTAMP" and isinstance(value, (int, float)):
                    value = dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
                clean[col] = value
            clean_rows.append(clean)

        job_config = bigquery.LoadJobConfig(
            schema=[bigquery.SchemaField(col, typ) for col, typ in columns],
            write_disposition="WRITE_APPEND",
        )
        self.client.load_table_from_json(
            clean_rows, self.table_id(table_name), job_config=job_config
        ).result()  # .result() waits until the load finishes
