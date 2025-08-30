### --- standard + typing utilities --- ###
import json, re, time, uuid
from typing import Any, Dict, List, Optional, TypedDict
from pydantic import BaseModel

### --- third-party libraries --- ###
from fastapi import FastAPI, HTTPException
from dateutil import parser as dtparse
from dateutil import tz
from langgraph.graph import StateGraph, START, END

### --- project imports --- ###
from app.config import settings
from app.domain.schemas import EventIn, EmailIn
from app.services.google_auth import GoogleAuth, ALL_SCOPES
from app.services.calendar_service import CalendarClient
from app.services.gmail_service import GmailClient
from app.llm.client import make_llm

### -------- Global scipre for Core services (auth + API clients + LLM) ------------- ###

auth = GoogleAuth(ALL_SCOPES)
cal = CalendarClient(auth)
gm  = GmailClient(auth)
llm = make_llm()

# FastAPI app init
app = FastAPI(title="Calendar+Gmail — Agentic Orchestration")

### -------------------------- Direct tool endpoints --------------------------------- ###

@app.post("/events/create")
def create_event_api(e: EventIn):
    """Create a calendar event directly (no agent). Inputs: EventIn. Returns: ToolResult-like dict."""
    return cal.create_event(**e.model_dump())

@app.post("/email/send")
def send_email_api(m: EmailIn):
    """Send an email directly (no agent). Inputs: EmailIn. Returns: ToolResult-like dict."""
    return gm.send(**m.model_dump())

@app.get("/healthz")
def healthz():
    """Health probe."""
    return {"ok": True}

### -------------------------- Agent (LangGraph) — minimal --------------------------------- ###
class AgentState(TypedDict, total=False):
    """State passed through the graph: slots, history, artifacts, and UI flags."""
    slots: Dict[str, Any]            # title,start_iso,end_iso,timezone,attendees[],location,description,recurrence,link
    history: List[Dict[str, str]]    # [{role:"user"|"assistant", content:str}]
    busy: List[Dict[str, Any]]
    created_event: Dict[str, Any]
    email_result: Dict[str, Any]
    reply: str
    new_user_message: str
    awaiting_confirm: bool
    confirm_summary: str

### -------------------------- Config / Patterns --------------------------------- ###

DEFAULT_TZ = getattr(settings, "default_timezone", "America/Los_Angeles")

# Small regexes for yes/no and "no email"
YES_RE = re.compile(r"\b(yes|yep|yeah|sure|confirm|go ahead|proceed|looks good|create it|do it)\b", re.I)
NO_RE  = re.compile(r"\b(no|nah|stop|cancel|don'?t|hold|wait)\b", re.I)
NO_EMAIL_RE = re.compile(r"\b(don'?t|do not|no|skip)\s+email\b", re.I)

EMAIL_RE = re.compile(r"[\w\.-]+@[\w\.-]+\.\w+")
URL_RE   = re.compile(r"https?://\S+")
REQUIRED = ["title", "start_iso", "end_iso", "timezone"]

# Tiny map of TZ words/abbr → IANA
TZ_WORDS = {
    "pacific": "America/Los_Angeles",
    "mountain": "America/Denver",
    "central": "America/Chicago",
    "eastern": "America/New_York",
}
TZ_ABBR = {
    "PT": "America/Los_Angeles","PST":"America/Los_Angeles","PDT":"America/Los_Angeles",
    "MT": "America/Denver","MST":"America/Denver","MDT":"America/Denver",
    "CT": "America/Chicago","CST":"America/Chicago","CDT":"America/Chicago",
    "ET": "America/New_York","EST":"America/New_York","EDT":"America/New_York",
}

# Small text templates
CONFIRM_TEMPLATE = 'Create “{title}” from {start} to {end} ({tz}) with attendees [{attendees}]{loc}{link}? (yes/no)'
EMAIL_SUBJECT_TPL = "Invite: {title}"
EMAIL_BODY_TPL = (
    "You're invited to '{title}'.\n"
    "Start: {start}\nEnd: {end}\nTimezone: {tz}\n{linkline}"
)
EVENT_LINK_LABEL = "Link"

### ------------------------------ Basic Helpers --------------------------------------- ###

def _llm_complete(prompt: str) -> str:
    """Call LLM and return text. Supports .complete() or .predict()."""
    return llm.complete(prompt).text if hasattr(llm, "complete") else llm.predict(prompt)

def is_affirmative(text: str) -> bool:
    """Detect a 'yes' style confirmation."""
    return bool(YES_RE.search(text or ""))

def is_negative(text: str) -> bool:
    """Detect a 'no/cancel' response."""
    return bool(NO_RE.search(text or ""))

def wants_no_email(text: str) -> bool:
    """Detect 'no email' intent."""
    return bool(NO_EMAIL_RE.search((text or "").lower()))

def _parse_dt(s: str):
    """Parse to aware datetime; default to DEFAULT_TZ if naive. Returns datetime|None."""
    try:
        dt = dtparse.parse(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=tz.gettz(DEFAULT_TZ))
    except Exception:
        return None

def _normalize_times(slots: Dict[str, Any]) -> None:
    """Best-effort ISO normalization of start/end; ensure timezone present."""
    for k in ("start_iso", "end_iso"):
        v = slots.get(k)
        if v and isinstance(v, str):
            try:
                slots[k] = dtparse.parse(v).isoformat(timespec="seconds")
            except Exception:
                pass
    if not slots.get("timezone"):
        slots["timezone"] = DEFAULT_TZ

def _missing(slots: Dict[str, Any]) -> List[str]:
    """Return list of missing/invalid required fields."""
    miss = [k for k in REQUIRED if not slots.get(k)]
    si, ei = slots.get("start_iso"), slots.get("end_iso")
    if si and ei and isinstance(si, str) and isinstance(ei, str):
        sdt, edt = _parse_dt(si), _parse_dt(ei)
        if sdt and edt and sdt >= edt:
            miss.append("end_iso (must be after start_iso)")
    return miss

def _detect_tz_tokens(text: str) -> Optional[str]:
    """Extract TZ from words/abbr in text. Returns IANA or None."""
    t = (text or "").lower()
    for w, zone in TZ_WORDS.items():
        if w in t: return zone
    m = re.findall(r"\b(PDT|PST|PT|MDT|MST|MT|CDT|CST|CT|EDT|EST|ET)\b", text or "", flags=re.I)
    return TZ_ABBR.get((m[-1] if m else "").upper()) if m else None

def _cheap_title(text: str) -> str:
    """Simple title guess: 'Meeting with {Name}' or 'Meeting'."""
    m = re.search(r"\bwith\s+([A-Z][a-zA-Z]+)\b", text or "")
    return f"Meeting with {m.group(1)}" if m else "Meeting"

def _preextract_slots(state: AgentState) -> None:
    """Quick pass to add emails/link/tz hints from latest message."""
    text = state.get("new_user_message") or ""
    if not text: return
    s = state["slots"]
    # emails
    emails = EMAIL_RE.findall(text)
    if emails:
        seen = {e.lower() for e in (s.get("attendees") or [])}
        for e in emails:
            if e.lower() not in seen:
                s.setdefault("attendees", []).append(e)
    # first URL as link
    if not s.get("link"):
        m = URL_RE.search(text)
        if m: s["link"] = m.group(0)
    # tz hint
    tz_hint = _detect_tz_tokens(text)
    if tz_hint: s["timezone"] = tz_hint

def _build_confirm_summary(s: Dict[str, Any]) -> str:
    """Render a short confirmation line from current slots."""
    title = s.get("title") or "Untitled"
    start = s.get("start_iso") or "?"
    end   = s.get("end_iso") or "?"
    tzid  = s.get("timezone") or DEFAULT_TZ
    attendees = ", ".join(s.get("attendees") or []) or "none"
    loc  = f", location: {s['location']}" if s.get("location") else ""
    link = f", link: {s['link']}" if s.get("link") else ""
    return CONFIRM_TEMPLATE.format(title=title, start=start, end=end, tz=tzid,
                                   attendees=attendees, loc=loc, link=link)

### ------------------------------- Tools ---------------------------------------- ###

def tool_check_availability(state: AgentState) -> AgentState:
    """Call Calendar free/busy. Inputs: slots[start/end/tz]. Returns: busy list + reply."""
    s = state["slots"]
    if not (s.get("start_iso") and s.get("end_iso") and s.get("timezone")):
        return {"reply": "I need start time, end time, and timezone to check availability."}
    try:
        busy = cal.get_busy(s["start_iso"], s["end_iso"], s["timezone"])
        if busy:
            return {"busy": busy, "reply": "That time conflicts with another event."}
        return {"busy": [], "reply": "You're free at that time."}
    except Exception as e:
        return {"reply": f"Availability check failed: {e}"}

def tool_create_event(state: AgentState) -> AgentState:
    """Create calendar event from slots; attach link if present. Returns: created_event + reply."""
    s = state["slots"]
    miss = _missing(s)
    if miss: return {"reply": f"Still missing: {', '.join(miss)}."}
    desc = s.get("description") or ""
    if s.get("link") and EVENT_LINK_LABEL.lower() not in desc.lower():
        desc = (desc + "\n" if desc else "") + f"{EVENT_LINK_LABEL}: {s['link']}"
    event_in = EventIn(
        title=s["title"], start_iso=s["start_iso"], end_iso=s["end_iso"],
        timezone=s["timezone"], attendees=s.get("attendees") or [],
        location=s.get("location"), description=(desc or None), recurrence=s.get("recurrence"),
    )
    created = cal.create_event(**event_in.model_dump())
    # prefer returned htmlLink if present
    link = (
        (created.get("data", {}) or {}).get("link")
        or created.get("htmlLink")
        or (created.get("data", {}) or {}).get("htmlLink")
    )
    if link: s["link"] = link
    return {"created_event": created, "reply": "Event created."}

def tool_send_email(state: AgentState, yes: bool) -> AgentState:
    """Send invite email to attendees if confirmed. Returns: email_result + reply."""
    if not yes: return {"reply": "Okay, no email sent."}
    s, ev = state["slots"], state.get("created_event")
    if not ev: return {"reply": "I couldn't find the event I just created."}
    atts = s.get("attendees") or []
    if not atts: return {"reply": "Event created. (No attendees to email.)"}
    link = ((ev.get("data", {}) or {}).get("link") or ev.get("htmlLink") or s.get("link") or "")
    subject = EMAIL_SUBJECT_TPL.format(title=s["title"])
    linkline = f"Link: {link}" if link else ""
    body = EMAIL_BODY_TPL.format(title=s["title"], start=s["start_iso"], end=s["end_iso"], tz=s["timezone"], linkline=linkline)
    result = gm.send(to=atts, subject=subject, body_text=body)
    return {"email_result": result, "reply": "Event created and email sent."}

### ----------------------------- Policy Prompt ---------------------------------- ###

AGENT_SYS = f"""
You are a calendar-scheduling agent. Default timezone: {DEFAULT_TZ}.
Choose ONE action per turn. Return STRICT JSON ONLY (no prose).

Schema:
{{
  "action": "<ask|set|confirm|check_availability|create_event|send_email|finish>",
  "args": {{}}
}}

Slots you can set: title, start_iso, end_iso, timezone, attendees, location, description, recurrence, link.

Behavior:
- Do NOT check availability unless the user explicitly asks (no proactive suggestions).
- Do NOT create the event until the user confirms.
- When required slots are known, ask for explicit confirmation with action "confirm" and include a concise summary in args.summary.
- Infer a simple neutral title if possible (e.g., "Meeting with Ada"); otherwise ask only if necessary.
- Interpret relative dates like "tomorrow" and time ranges like "10-10:30". Convert to ISO 8601 strings with seconds.
- Map TZ tokens (PT/MT/CT/ET, PDT/PST/etc.) or words ("Pacific time") to IANA timezones. If none present, keep current timezone.

Return ONLY the JSON object.
""".strip()

def decide_and_act(state: AgentState) -> AgentState:
    """Single policy step: prep slots → prompt LLM → apply chosen action."""
    _preextract_slots(state)
    _normalize_times(state["slots"])

    transcript = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in state["history"][-8:])
    today_info = time.strftime("%Y-%m-%d (%A)")

    prompt = f"""{AGENT_SYS}

TODAY: {today_info}

Current slots (null means unknown):
{json.dumps(state["slots"], ensure_ascii=False)}

Latest user message:
{state.get("new_user_message","")}

Conversation (recent turns):
{transcript}

ASSISTANT:"""

    raw = _llm_complete(prompt).strip()
    try:
        decision = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        # Fallback: title -> confirm if ready
        if not state["slots"].get("title"):
            state["slots"]["title"] = _cheap_title(state.get("new_user_message","") or "Meeting")
        miss = _missing(state["slots"])
        if miss:
            return {"reply": f"Missing: {', '.join(miss)}."}
        summary = _build_confirm_summary(state["slots"])
        return {"awaiting_confirm": True, "confirm_summary": summary, "reply": summary}

    action = (decision.get("action") or "").lower()
    args   = decision.get("args") or {}

    if action == "ask":
        return {"reply": args.get("question") or "What should I provide next?"}

    if action == "set":
        for k, v in args.items():
            if k in state["slots"] and v not in (None, "", []):
                state["slots"][k] = v
        _normalize_times(state["slots"])
        miss = _missing(state["slots"])
        if miss: return {"reply": f"Missing: {', '.join(miss)}."}
        summary = _build_confirm_summary(state["slots"])
        return {"awaiting_confirm": True, "confirm_summary": summary, "reply": summary}

    if action == "confirm":
        summary = args.get("summary") or _build_confirm_summary(state["slots"])
        return {"awaiting_confirm": True, "confirm_summary": summary, "reply": summary}

    if action == "check_availability":
        return tool_check_availability(state)

    if action == "create_event":
        if not state.get("awaiting_confirm"):
            summary = _build_confirm_summary(state["slots"])
            return {"awaiting_confirm": True, "confirm_summary": summary, "reply": summary}
        return tool_create_event(state)

    if action == "send_email":
        return tool_send_email(state, yes=bool(args.get("yes", True)))

    if action == "finish":
        return {"reply": args.get("message", "All set.")}

    return {"reply": "Please provide the next detail."}

### ------------------------------- Graph ---------------------------------------- ###

# One node (policy) then END
graph = StateGraph(AgentState)
graph.add_node("policy", decide_and_act)
graph.add_edge(START, "policy")
graph.add_edge("policy", END)
APP_GRAPH = graph.compile()

### ------------------------ Minimal in-memory sessions --------------------------- ###

SESSIONS: Dict[str, AgentState] = {}
TOUCHED: Dict[str, float] = {}
TTL_SECS = getattr(settings, "session_ttl_secs", 15 * 60)

def _new_session() -> str:
    """Create a fresh session with empty slots/history. Returns session_id."""
    sid = str(uuid.uuid4())
    SESSIONS[sid] = {
        "slots": {"title": None, "start_iso": None, "end_iso": None, "timezone": DEFAULT_TZ,
                  "attendees": [], "location": None, "description": None, "recurrence": None, "link": None},
        "history": [],
        "reply": "Hi! What should I schedule?",
        "awaiting_confirm": False,
        "confirm_summary": "",
    }
    TOUCHED[sid] = time.time()
    return sid

def _get_session(sid: str) -> AgentState:
    """Fetch existing session or 404."""
    st = SESSIONS.get(sid)
    if not st: raise HTTPException(404, "invalid session")
    TOUCHED[sid] = time.time()
    return st

def _reap():
    """Drop idle sessions past TTL."""
    now = time.time()
    for sid, ts in list(TOUCHED.items()):
        if now - ts > TTL_SECS:
            SESSIONS.pop(sid, None); TOUCHED.pop(sid, None)

### --------------------------------- API ---------------------------------------- ###

class StartOut(BaseModel):
    """Response for /agent/start."""
    session_id: str
    reply: str

@app.post("/agent/start", response_model=StartOut)
def agent_start():
    """Open a new agent session."""
    _reap()
    sid = _new_session()
    return {"session_id": sid, "reply": SESSIONS[sid]["reply"]}

class ChatIn(BaseModel):
    """Input to /agent/chat."""
    session_id: str
    message: str

class ChatOut(BaseModel):
    """Response from /agent/chat."""
    session_id: str
    reply: str
    slots: Dict[str, Any]
    done: bool = False
    data: Optional[Dict[str, Any]] = None

def wants_check_availability(text: str) -> bool:
    """Heuristic: detect 'check availability' intent."""
    t = (text or "").lower()
    return bool(re.search(r"\b(check|verify|see)\b.*\b(avail|availability|free|busy)\b", t)) \
        or bool(re.search(r"\bam i (free|busy)\b", t))

@app.post("/agent/chat", response_model=ChatOut)
def agent_chat(inp: ChatIn):
    """Main chat loop: handle explicit availability, confirm yes/no locally, else run one policy step."""
    _reap()
    state = _get_session(inp.session_id)

    text = (inp.message or "").strip()
    state.setdefault("history", []).append({"role": "user", "content": text})
    state["new_user_message"] = text

    # Explicit availability path (no LLM)
    if wants_check_availability(text):
        _preextract_slots(state); _normalize_times(state["slots"])
        miss = _missing(state["slots"])
        if miss:
            reply = f"Missing for availability check: {', '.join(miss)}."
            state["new_user_message"] = ""
            state["history"].append({"role": "assistant", "content": reply})
            return ChatOut(session_id=inp.session_id, reply=reply, slots=state["slots"], done=False)
        updates = tool_check_availability(state)
        state.update(updates); state["new_user_message"] = ""
        reply = state.get("reply", "")
        if reply: state["history"].append({"role": "assistant", "content": reply})
        return ChatOut(session_id=inp.session_id, reply=reply, slots=state["slots"], done=False)

    # Confirmation loop: interpret yes/no locally (avoiding multiple agent loops)
    if state.get("awaiting_confirm"):
        if is_affirmative(text):
            created_updates = tool_create_event(state); state.update(created_updates)
            data: Dict[str, Any] = {}
            if state.get("created_event"):
                data["event"] = state["created_event"]
                if not wants_no_email(text):
                    email_updates = tool_send_email(state, yes=True); state.update(email_updates)
                    if state.get("email_result"): data["email"] = state["email_result"]
                    reply = state.get("reply", "Event created and email sent.")
                else:
                    reply = "Event created. (Skipped email.)"
                state["awaiting_confirm"] = False; state["confirm_summary"] = ""; state["new_user_message"] = ""
                state["history"].append({"role": "assistant", "content": reply})
                return ChatOut(session_id=inp.session_id, reply=reply, slots=state["slots"], done=True, data=data)
            # create failed
            reply = state.get("reply", "Failed to create event.")
            state["awaiting_confirm"] = False; state["confirm_summary"] = ""; state["new_user_message"] = ""
            state["history"].append({"role": "assistant", "content": reply})
            return ChatOut(session_id=inp.session_id, reply=reply, slots=state["slots"], done=False)
        if is_negative(text):
            state["awaiting_confirm"] = False; state["confirm_summary"] = ""; state["new_user_message"] = ""
            reply = "Okay, what would you like to change?"
            state["history"].append({"role": "assistant", "content": reply})
            return ChatOut(session_id=inp.session_id, reply=reply, slots=state["slots"], done=False)

    # If slots complete and user says continue without pending confirm -> treat as confirm
    _preextract_slots(state); _normalize_times(state["slots"])
    if not state.get("awaiting_confirm") and not _missing(state["slots"]) and is_affirmative(text):
        state["awaiting_confirm"] = True; state["confirm_summary"] = _build_confirm_summary(state["slots"])
        created_updates = tool_create_event(state); state.update(created_updates)
        data: Dict[str, Any] = {}
        if state.get("created_event"):
            data["event"] = state["created_event"]
            if not wants_no_email(text):
                email_updates = tool_send_email(state, yes=True); state.update(email_updates)
                if state.get("email_result"): data["email"] = state["email_result"]
                reply = state.get("reply", "Event created and email sent.")
            else:
                reply = "Event created. (Skipped email.)"
            state["awaiting_confirm"] = False; state["confirm_summary"] = ""; state["new_user_message"] = ""
            state["history"].append({"role": "assistant", "content": reply})
            return ChatOut(session_id=inp.session_id, reply=reply, slots=state["slots"], done=True, data=data)
        reply = state.get("reply", "Failed to create event.")
        state["awaiting_confirm"] = False; state["confirm_summary"] = ""; state["new_user_message"] = ""
        state["history"].append({"role": "assistant", "content": reply})
        return ChatOut(session_id=inp.session_id, reply=reply, slots=state["slots"], done=False)

    # ---- Run 1 policy/action step ----
    updates = APP_GRAPH.invoke(state)
    state.update(updates)
    state["new_user_message"] = ""
    reply = state.get("reply", "")

    # If a create happened in this step, auto-email (once) unless user said not to
    data: Dict[str, Any] = {}
    created_now = "created_event" in updates and updates["created_event"]
    if created_now:
        data["event"] = state["created_event"]
        if not wants_no_email(text):
            email_updates = tool_send_email(state, yes=True); state.update(email_updates)
            if state.get("email_result"): data["email"] = state["email_result"]
            reply = state.get("reply", "Event created and email sent.")
        else:
            reply = "Event created. (Skipped email.)"
        state["awaiting_confirm"] = False; state["confirm_summary"] = ""

    done = bool(state.get("created_event"))
    if reply: state["history"].append({"role": "assistant", "content": reply})
    return ChatOut(session_id=inp.session_id, reply=reply, slots=state["slots"], done=done, data=data)

class EndIn(BaseModel):
    """Input to /agent/end."""
    session_id: str

@app.post("/agent/end")
def agent_end(inp: EndIn):
    """Close a session and drop stored state."""
    SESSIONS.pop(inp.session_id, None); TOUCHED.pop(inp.session_id, None)
    return {"ended": True}

### ---------------------- curl examples ---------------------------- ###
"""
# Health
curl -s localhost:8000/healthz

# Direct tools (smoke tests)
curl -sX POST localhost:8000/events/create -H "content-type: application/json" \
  -d '{"title":"Sync","start_iso":"2025-08-18T10:00:00","end_iso":"2025-08-18T10:30:00","timezone":"America/Chicago","attendees":["someone@example.com"]}'

curl -sX POST localhost:8000/email/send -H "content-type: application/json" \
  -d '{"to":["someone@example.com"],"subject":"Test","body_text":"Hello."}'

# Agent flow
SID=$(curl -sX POST localhost:8000/agent/start -H "content-type: application/json" | jq -r .session_id)
curl -sX POST localhost:8000/agent/chat -H "content-type: application/json" \
  -d "{\"session_id\":\"$SID\",\"message\":\"schedule meeting with Ada tomorrow 10-10:30 PT, add bob@example.com, send confirmation\"}"
curl -sX POST localhost:8000/agent/chat -H "content-type: application/json" \
  -d "{\"session_id\":\"$SID\",\"message\":\"yes\"}"
"""
