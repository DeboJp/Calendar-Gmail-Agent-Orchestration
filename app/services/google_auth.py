from __future__ import annotations
import os
from typing import List
from app.config import settings
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# All Google API scopes needed for this project (Calendar + Gmail send)
ALL_SCOPES: List[str] = (
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
)

class GoogleAuth:
    def __init__(self, scopes: List[str]):
        """
        Initialize the GoogleAuth helper with OAuth2 scopes.
        Inputs:
            scopes: list of Google API scope URLs.
        """
        self.scopes = scopes
        self.client_path = settings.google_client_secret_path
        self.token_path = settings.google_token_path

    def creds(self) -> Credentials:
        """
        Retrieve valid Google OAuth2 credentials.
        - Loads saved token from disk if available.
        - If expired, refreshes token (if refresh token exists).
        - If missing/invalid, runs local OAuth2 flow to get a new one.
        Returns:
            google.oauth2.credentials.Credentials object (valid access token).
        """
        c = None
        if os.path.exists(self.token_path):
            c = Credentials.from_authorized_user_file(self.token_path, self.scopes)
        if not c or not c.valid:
            if c and c.expired and c.refresh_token:
                from google.auth.transport.requests import Request
                c.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.client_path, self.scopes)
                # For headless: flow.run_console()
                c = flow.run_local_server(port=0)
            with open(self.token_path, "w") as f:
                f.write(c.to_json())
        return c
