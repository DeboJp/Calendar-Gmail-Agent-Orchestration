from typing import List, Optional, Dict, Any
from pydantic import BaseModel, EmailStr, Field

class EventIn(BaseModel):
    """Schema for creating a calendar event."""
    title: str = Field(..., min_length=1)
    start_iso: str #ISO FORMAT
    end_iso: str
    timezone: str #IANA String
    attendees: List[EmailStr] = []
    location: Optional[str] = None
    description: Optional[str] = None
    recurrence: Optional[str] = None

class EmailIn(BaseModel):
    """Schema for sending an email."""
    to: List[EmailStr] # recipient list
    subject: str
    body_text: str

class ToolResult(BaseModel):
    """Generic tool call result wrapper."""
    ok: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
