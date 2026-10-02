"""Import planu zajęć z USOS (plik .ics: USOSweb → Mój plan → eksport do iCalendar).

Z każdego zdarzenia bierzemy to, co USOS faktycznie podaje:
SUMMARY „W - Przedmiot” → typ + tytuł, DTSTART/DTEND → termin,
DESCRIPTION „Sala: 233\\nA30\\n\\n<link>” → sala, budynek, link, LOCATION → adres.
UID zajęć jest stały, więc ponowny import aktualizuje wpisy zamiast je dublować.
"""
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import get_conn

router = APIRouter()

# skróty form zajęć USOS → typy wpisów w kalendarzu
KINDS = {"W": "Wykład", "CW": "Ćwiczenia", "ĆW": "Ćwiczenia", "C": "Ćwiczenia", "LAB": "Laboratorium", "L": "Laboratorium",
         "P": "Projekt", "PROJ": "Projekt", "S": "Seminarium", "SEM": "Seminarium", "K": "Konsultacje", "LEK": "Lektorat"}


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\;", ";").replace("\\\\", "\\")


def _dt(v: str) -> datetime:
    # „20261003T113000” = czas lokalny planu (Europe/Warsaw); z „Z” = UTC → czas systemu
    if v.endswith("Z"):
        return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return datetime.strptime(v[:15], "%Y%m%dT%H%M%S")


def parse_ics(text: str) -> list[dict]:
    text = re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))   # rozwinięcie zawiniętych linii (RFC 5545)
    out = []
    for block in re.findall(r"BEGIN:VEVENT\n(.*?)END:VEVENT", text, re.S):
        f = {}
        for line in block.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                f[k.split(";")[0].upper()] = _unescape(v)
        if "DTSTART" not in f or f.get("STATUS", "").upper() == "CANCELLED":
            continue
        start = _dt(f["DTSTART"])
        end = _dt(f["DTEND"]) if "DTEND" in f else None
        summary = f.get("SUMMARY", "").strip()
        m = re.match(r"^([A-Za-zĆć]{1,5})\s*[-–]\s*(.+)$", summary)
        kind, title = ("", summary)
        if m and m.group(1).upper() in KINDS:
            kind, title = KINDS[m.group(1).upper()], m.group(2).strip()
        desc = f.get("DESCRIPTION", "")
        url = (re.search(r"https?://\S+", desc) or [""])[0]
        lines = [l.strip() for l in re.sub(r"https?://\S+", "", desc).split("\n") if l.strip()]
        room = building = ""
        if lines and lines[0].lower().startswith("sala:"):
            room = lines[0].split(":", 1)[1].strip()
            building = lines[1] if len(lines) > 1 else ""
        out.append({
            "uid": f.get("UID") or f"{summary}|{f['DTSTART']}",
            "title": title or "Zajęcia", "kind": kind,
            "date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"),
            "end_time": end.strftime("%H:%M") if end else None,
            "room": room, "building": building, "address": f.get("LOCATION", "").strip(), "url": url,
        })
    return out


class IcsImport(BaseModel):
    data: str   # treść pliku .ics


@router.post("/api/import/ics")
def import_ics(p: IcsImport):
    if "BEGIN:VCALENDAR" not in p.data:
        raise HTTPException(400, "To nie jest plik kalendarza (.ics).")
    events = parse_ics(p.data)
    if not events:
        raise HTTPException(400, "W pliku nie ma żadnych zajęć.")
    added = updated = 0
    with get_conn() as c:
        for e in events:
            row = c.execute("SELECT id FROM tasks WHERE ext_uid=?", (e["uid"],)).fetchone()
            vals = (e["title"], e["kind"], e["date"], e["start_time"], e["end_time"], e["room"], e["building"], e["address"], e["url"])
            if row:   # aktualizacja z USOS; zostają Twoje: zrobione, notatka, powiązanie, priorytet, przypomnienie
                c.execute("UPDATE tasks SET title=?,kind=?,date=?,start_time=?,end_time=?,room=?,building=?,address=?,url=? WHERE id=?",
                          (*vals, row["id"]))
                updated += 1
            else:
                c.execute("INSERT INTO tasks(title,kind,date,start_time,end_time,room,building,address,url,ext_uid) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (*vals, e["uid"]))
                added += 1
    dates = sorted(e["date"] for e in events)
    return {"added": added, "updated": updated, "first": dates[0], "last": dates[-1]}


if __name__ == "__main__":   # szybki test parsera: python usos.py plan.ics
    import sys
    evs = parse_ics(open(sys.argv[1], encoding="utf-8").read())
    assert evs and all(e["date"] and e["start_time"] for e in evs)
    for e in evs[:3]:
        print(e)
    print(len(evs), "zajęć")
