"""
get_refresh_token.py
Run this on your own computer to log in to Google and get a refresh token.
Run it again whenever the list of permissions (SCOPES) changes.

What it does:
1. Opens your browser to Google's login page.
2. You approve the permissions (read Gmail, send Gmail, use BigQuery).
3. It saves the three GOOGLE_ values straight into your .env file,
   so the secrets never show up on screen.

Before running, the OAuth client file must be in this folder as
credentials.json (see README.md).

Run it with:
    python get_refresh_token.py
"""

import os

from google_auth_oauthlib.flow import InstalledAppFlow

from gmail_client import PROJECT_FOLDER, SCOPES


def save_to_env(values):
    """
    Write KEY=value lines into .env. Lines for these keys are replaced;
    every other line in the file (like ANTHROPIC_API_KEY) is kept as is.
    """
    env_path = os.path.join(PROJECT_FOLDER, ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.read().splitlines()

    # Keep every line that isn't one of the keys we're about to write.
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in values]
    kept += [f"{key}={value}" for key, value in values.items()]

    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")


def main():
    flow = InstalledAppFlow.from_client_secrets_file(
        os.path.join(PROJECT_FOLDER, "credentials.json"), SCOPES
    )

    # access_type="offline" asks for a refresh token (a long-lived key).
    # prompt="consent" forces Google to issue a new one even if you
    # approved this app before.
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    save_to_env({
        "GOOGLE_CLIENT_ID": credentials.client_id,
        "GOOGLE_CLIENT_SECRET": credentials.client_secret,
        "GOOGLE_REFRESH_TOKEN": credentials.refresh_token,
    })
    print("Success. Your Google login was saved to .env")


if __name__ == "__main__":
    main()
