"""
resumes.py
Reads every resume in the resumes/ folder as plain text, for scoring.
The folder is git-ignored, so resumes never go to the public repo.

A .docx file is really a zip file. Inside it, word/document.xml holds the
text: each paragraph is a <w:p> tag and each piece of text is a <w:t> tag.
So we can read it with Python's built-in zipfile and XML tools; no extra
package needed.
"""

import os
import zipfile
import xml.etree.ElementTree as ET

from gmail_client import PROJECT_FOLDER

RESUME_FOLDER = os.path.join(PROJECT_FOLDER, "resumes")

# Word's XML uses this "namespace" prefix on every tag name.
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_to_text(path):
    """Return the text of a .docx file, one line per paragraph."""
    with zipfile.ZipFile(path) as docx:
        root = ET.fromstring(docx.read("word/document.xml"))

    lines = []
    for paragraph in root.iter(f"{W}p"):
        text = "".join(piece.text or "" for piece in paragraph.iter(f"{W}t"))
        if text.strip():
            lines.append(text.strip())
    return "\n".join(lines)


def load_resumes():
    """
    Returns {"DE": "...resume text...", "TPM": "...", ...}.
    The short name is whatever comes after "Resume_" in the file name,
    e.g. Gennady_Yezavit_Resume_DE.docx -> "DE".
    """
    resumes = {}
    for filename in sorted(os.listdir(RESUME_FOLDER)):
        if not filename.lower().endswith(".docx"):
            continue
        name = os.path.splitext(filename)[0].split("Resume_")[-1]
        resumes[name] = docx_to_text(os.path.join(RESUME_FOLDER, filename))
    return resumes


if __name__ == "__main__":
    for name, text in load_resumes().items():
        first_line = text.splitlines()[0] if text else ""
        print(f"{name:16} {len(text):>6,} chars   {first_line[:70]}")
