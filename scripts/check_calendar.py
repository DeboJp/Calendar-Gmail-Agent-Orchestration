from app.services.google_auth import GoogleAuth, ALL_SCOPES
from googleapiclient.discovery import build

#Run to check if calendar connected/working.
if __name__ == "__main__":
    svc = build("calendar","v3", credentials=GoogleAuth(ALL_SCOPES).creds())
    me = svc.calendarList().get(calendarId="primary").execute()
    print("Primary:", me.get("summary"), "| tz:", me.get("timeZone"))


# From root directory:
# python3 -m scripts.check_calendar 