"""One-shot script to obtain a refresh token covering Gmail + YouTube + Drive scopes.

Run with:  .venv/bin/python get_token.py
A browser window will open for Google consent. After approval, GOOGLE_REFRESH_TOKEN
is written directly to .env.
"""
import os
import re
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/drive",
]

client_config = {
    "installed": {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
creds = flow.run_local_server(port=0)

env_path = Path(__file__).parent / ".env"
text = env_path.read_text()
new_line = f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}"
if re.search(r"^GOOGLE_REFRESH_TOKEN=", text, re.MULTILINE):
    text = re.sub(r"^GOOGLE_REFRESH_TOKEN=.*", new_line, text, flags=re.MULTILINE)
else:
    text += f"\n{new_line}\n"
env_path.write_text(text)
print("✓ GOOGLE_REFRESH_TOKEN written to .env")
