import base64
from app.services.google_auth import GoogleAuth, ALL_SCOPES
from app.config import settings
from googleapiclient.discovery import build

#Run to check if gmail connected/working.
if __name__ == "__main__":
    assert settings.gmail_from, "Set GMAIL_FROM in .env"
    svc = build("gmail","v1", credentials=GoogleAuth(ALL_SCOPES).creds())
    msg = f"From: {settings.gmail_from}\nTo: {settings.gmail_from}\nSubject: Gmail API test\n\nHello."
    raw = base64.urlsafe_b64encode(msg.encode()).decode()
    sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    print({"ok": True, "data": {"id": sent.get("id")}})

# From root directory:
# python3 -m scripts.check_gmail 