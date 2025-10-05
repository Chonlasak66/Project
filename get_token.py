# oauth_token_gen.py
from __future__ import annotations
import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CLIENT_SECRETS = os.environ.get("GDRIVE_OAUTH_CLIENT_SECRETS", "credentials.json")
TOKEN_PATH = os.environ.get("GDRIVE_TOKEN_PATH", "token.json")

creds = None
if os.path.exists(TOKEN_PATH):
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, SCOPES)
        creds = flow.run_local_server(port=0)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())

# quick sanity check
service = build("drive", "v3", credentials=creds)
about = service.about().get(fields="user(displayName,emailAddress)").execute()
print("OK:", about["user"])
