import base64
from typing import Dict, Any, List
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from app.services.google_auth import GoogleAuth
from app.config import settings

class GmailClient:
    def __init__(self, auth: GoogleAuth):
        """
        Gmail API client wrapper.
        Inputs:
            auth: GoogleAuth instance used to retrieve valid credentials.
        """
        self._auth = auth

    def _svc(self):
        """
        Build and return a Gmail API service object using authorized credentials.
        Returns:
            googleapiclient Gmail API service instance.
        """
        return build("gmail", "v1", credentials=self._auth.creds())

    def send(self, to: List[str], subject: str, body_text: str) -> Dict[str, Any]:
        """
        Send an email using the Gmail API.
        Inputs:
            to: list of recipient email addresses.
            subject: subject line for the email.
            body_text: plain-text body content of the email.
        Returns:
            dict with:
                - ok: True/False indicating success.
                - data: message ID if successful.
                - error: error message string if failed.
        """
        if not settings.gmail_from:
            return {"ok": False, "error": "GMAIL_FROM not set"}
        
        # Construct formatted message
        msg = f"From: {settings.gmail_from}\nTo: {', '.join(to)}\nSubject: {subject}\n\n{body_text}"
        raw = base64.urlsafe_b64encode(msg.encode()).decode()
        try:
            sent = self._svc().users().messages().send(userId="me", body={"raw": raw}).execute()
            return {"ok": True, "data": {"id": sent.get("id")}}
        except HttpError as e:
            return {"ok": False, "error": str(e)}
