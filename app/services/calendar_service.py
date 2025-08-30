from typing import Dict, Any, List, Optional
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from app.services.google_auth import GoogleAuth
from app.config import settings

class CalendarClient:
    def __init__(self, auth: GoogleAuth):
        """
        Google Calendar API client wrapper.
        Inputs:
            auth: GoogleAuth instance used to retrieve valid credentials.
        """
        self._auth = auth

    def _svc(self):
        """
        Build and return a Calendar API service object using authorized credentials.
        Returns:
            googleapiclient Calendar API service instance.
        """
        return build("calendar", "v3", credentials=self._auth.creds())

    def create_event(
        self, title: str,
        start_iso: str, end_iso: str,
        timezone: str = settings.default_timezone,
        attendees: Optional[List[str]] = None,
        location: Optional[str] = None,
        description: Optional[str] = None,
        recurrence: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a new event on the user's primary calendar.
        Inputs:
            title: event summary/title.
            start_iso: start time.
            end_iso: end time.
            timezone: timezone string.
            Optional:
            attendees: list of attendee email addresses.
            location: location string.
            description: description text.
            recurrence: recurrence rule string.
        Returns:
            dict with:
                - ok: True/False indicating success.
                - data: event id + link if successful.
                - error: error message string if failed.
        """
        body: Dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": start_iso, "timeZone": timezone},
            "end":   {"dateTime": end_iso,   "timeZone": timezone},
        }
        if attendees:   body["attendees"]   = [{"email": a} for a in attendees]
        if location:    body["location"]    = location
        if description: body["description"] = description
        if recurrence:  body["recurrence"]  = [recurrence]
        try:
            ev = self._svc().events().insert(calendarId="primary", body=body, sendUpdates="all").execute()
            return {"ok": True, "data": {"id": ev.get("id"), "link": ev.get("htmlLink")}}
        except HttpError as e:
            return {"ok": False, "error": str(e)}
        
    def get_busy(self, start_iso: str, end_iso: str, timezone: str):
        """
        Query the user's calendar for busy time ranges.
        Inputs:
            start_iso: start time.
            end_iso: end time.
            timezone: timezone string.
        Returns:
            list of dicts with {"start": < string>, "end": < string>} for each busy block.
        """

        body = {
            "timeMin": start_iso,
            "timeMax": end_iso,
            "timeZone": timezone,
            "items": [{"id": "primary"}],
        }
        resp = self.service.freebusy().query(body=body).execute()
        return [{"start": b.get("start"), "end": b.get("end")}
                for b in resp.get("calendars", {}).get("primary", {}).get("busy", [])]
