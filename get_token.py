"""One-shot script to obtain a refresh token covering Gmail + YouTube scopes.

Run with:  .venv/bin/python get_token.py
A browser window will open for Google consent. After approval, the new
GOOGLE_REFRESH_TOKEN value is printed — paste it into your .env file.
"""
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/youtube.readonly",
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

print("\n✓ New refresh token obtained. Add this to your .env:\n")
print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}\n")
