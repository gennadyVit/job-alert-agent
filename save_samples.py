"""
save_samples.py
Run this ONCE (after gmail_client.py works) to save real alert emails
into tests/sample_emails/. Step 2 uses these files to test job extraction
without calling Gmail every time.

The message IDs below are real emails from your inbox (Oct 6-8, 2026),
one of each type. If you delete those emails, swap in new IDs.

Run it with:
    python save_samples.py
"""

import os

from gmail_client import PROJECT_FOLDER, get_gmail_service, get_message_bodies

# name to use for the saved file -> Gmail message ID
SAMPLES = {
    "linkedin_alert": "1a118dfed763b2de",
    "linkedin_application_confirmation": "1a1121e1287cbe67",
    "indeed_alert": "1a1149ef9f06f65d",
    "indeed_match": "1a1156eee5fd0726",
    "builtin_alert": "1a1175bd962fa80c",
    "dice_alert": "1a110f6f3653d440",
}


def main():
    service = get_gmail_service()
    output_folder = os.path.join(PROJECT_FOLDER, "tests", "sample_emails")
    os.makedirs(output_folder, exist_ok=True)

    for name, message_id in SAMPLES.items():
        bodies = get_message_bodies(service, message_id)

        # Save each version that exists: name.txt for plain text, name.html for HTML.
        for kind, extension in (("plain", "txt"), ("html", "html")):
            if bodies[kind]:
                path = os.path.join(output_folder, f"{name}.{extension}")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(bodies[kind])
                print(f"Saved {path}  ({len(bodies[kind]):,} characters)")


if __name__ == "__main__":
    main()
