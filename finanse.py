"""Budżet (głównie studencki): dochody, cykliczne wydatki (dojazd, jedzenie, opłaty…), terminy płatności i symulacja miesięczna.

Kwoty nie są wymuszane — koszt może być szacunkiem („~”, np. paliwo), a faktyczne wydatki
dopisuje się na bieżąco (np. każde tankowanie) i porównuje z planem.
"""
import json
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import get_conn

router = APIRouter()

WEEKS_PER_MONTH = 52 / 12
DAYS_PER_MONTH = 365 / 12
CATEGORIES = {"dojazd", "jedzenie", "mieszkanie", "oplaty", "materialy", "abonamenty", "zdrowie", "rozrywka", "inne"}
PERIODS = {"daily", "day", "week", "month", "semester", "year", "once"}
MODES = {"samochod", "komunikacja", "pociag", "rower", "hulajnoga", "pieszo"}

def init():
    """Tabele modułu (idempotentne) — przy starcie i dla każdej nowej bazy (wersja webowa)."""
    with get_conn() as _c:
        _c.executescript("""
        CREATE TABLE IF NOT EXISTS costs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            category    TEXT DEFAULT 'inne',
            amount      REAL,                  -- kwota za okres (NULL = nie podano)
            period      TEXT DEFAULT 'month',  -- daily (codziennie) | day (dzień zajęć / X razy w tygodniu) | week | month | semester | year | once
            variable    INTEGER DEFAULT 0,     -- 1 = szacunek, kwota się waha
            mode        TEXT DEFAULT '',       -- dojazd: samochod | komunikacja | pociag | rower | hulajnoga | pieszo
            params      TEXT DEFAULT '{}',     -- dojazd samochodem: km, l100, price, days
            due_date    TEXT DEFAULT '',       -- termin płatności YYYY-MM-DD
            remind_days INTEGER DEFAULT 7,
            paid        INTEGER DEFAULT 0,
            note        TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS cost_entries (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            cost_id  INTEGER,
            amount   REAL NOT NULL,
            date     TEXT NOT NULL,
            note     TEXT DEFAULT '',
            FOREIGN KEY (cost_id) REFERENCES costs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS cost_settings (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS incomes (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            kind      TEXT DEFAULT 'inne',     -- praca | stypendium | rodzice | freelance | inne
            amount    REAL,
            period    TEXT DEFAULT 'month',    -- daily | week | month | semester | year | once
            variable  INTEGER DEFAULT 0,
            pay_day   INTEGER,                 -- dzień miesiąca wypłaty (1–31), opcjonalnie
            note      TEXT DEFAULT ''
        );
        """)


init()

INCOME_KINDS = {"praca", "stypendium", "rodzice", "freelance", "inne"}


def _settings(c) -> dict:
    s = {r["k"]: r["v"] for r in c.execute("SELECT k, v FROM cost_settings")}
    try:
        goal = max(0.0, float(s.get("savings_goal") or 0))
    except ValueError:
        goal = 0.0
    return {"study_days": int(s.get("study_days") or 5), "savings_goal": goal}


def _params(row) -> dict:
    try:
        p = json.loads(row["params"] or "{}")
        return p if isinstance(p, dict) else {}
    except Exception:
        return {}


def _days(row, st) -> float:
    p = _params(row)
    try:
        d = float(p.get("days") or 0)
    except (TypeError, ValueError):
        d = 0
    return d if 0 < d <= 7 else st["study_days"]


def _per_period(row, st) -> float | None:
    """Kwota za okres — dla samochodu liczona z km, spalania i ceny paliwa."""
    if row["category"] == "dojazd" and row["mode"] == "samochod":
        p = _params(row)
        try:
            km, l100, price = float(p.get("km") or 0), float(p.get("l100") or 0), float(p.get("price") or 0)
        except (TypeError, ValueError):
            return row["amount"]
        if km and l100 and price:
            return round(km * 2 * l100 / 100 * price, 2)   # tam i z powrotem, za jeden dzień zajęć
    return row["amount"]


def _monthly(row, st) -> float:
    a = _per_period(row, st)
    if not a:
        return 0.0
    per = row["period"]
    if per == "day":
        return a * _days(row, st) * WEEKS_PER_MONTH
    return a * {"daily": DAYS_PER_MONTH, "week": WEEKS_PER_MONTH, "month": 1, "semester": 1 / 6, "year": 1 / 12, "once": 0}.get(per, 1)


def _shape(row, st, spent: dict) -> dict:
    d = dict(row)
    d["params"] = _params(row)
    d["variable"] = bool(d["variable"])
    d["paid"] = bool(d["paid"])
    d["per_period"] = _per_period(row, st)
    d["monthly"] = round(_monthly(row, st), 2)
    d["days"] = _days(row, st)
    d["spent_month"] = round(spent.get(row["id"], 0), 2)
    d["days_left"] = None
    if d["due_date"] and not d["paid"]:
        try:
            d["days_left"] = (date.fromisoformat(d["due_date"]) - date.today()).days
        except ValueError:
            pass
    return d


@router.get("/api/costs")
def list_costs():
    month = date.today().isoformat()[:7]
    with get_conn() as c:
        st = _settings(c)
        spent = {r["cost_id"]: r["s"] for r in c.execute(
            "SELECT cost_id, SUM(amount) s FROM cost_entries WHERE date LIKE ? GROUP BY cost_id", (month + "%",))}
        costs = [_shape(r, st, spent) for r in c.execute("SELECT * FROM costs ORDER BY category, name")]
        entries = [dict(r) for r in c.execute(
            "SELECT e.*, c.name cost_name, c.category, c.mode FROM cost_entries e LEFT JOIN costs c ON c.id = e.cost_id "
            "WHERE e.date LIKE ? ORDER BY e.date DESC, e.id DESC", (month + "%",))]
    by_cat: dict = {}
    for x in costs:
        by_cat[x["category"]] = round(by_cat.get(x["category"], 0) + x["monthly"], 2)
    monthly = round(sum(x["monthly"] for x in costs), 2)
    upcoming = sorted([x for x in costs if x["days_left"] is not None], key=lambda x: x["days_left"])
    with get_conn() as c:
        incomes = [_income(r) for r in c.execute("SELECT * FROM incomes ORDER BY amount IS NULL, amount DESC, name")]
    income = round(sum(x["monthly"] for x in incomes), 2)
    return {
        "costs": costs, "incomes": incomes, "settings": st, "entries": entries, "upcoming": upcoming,
        "summary": {
            "monthly": monthly,
            "income": income,
            "left": round(income - monthly, 2),
            "variable": round(sum(x["monthly"] for x in costs if x["variable"]), 2),
            "by_cat": by_cat,
            "spent_month": round(sum(e["amount"] for e in entries), 2),
            "once": round(sum((x["per_period"] or 0) for x in costs if x["period"] == "once" and not x["paid"]), 2),
        },
    }


def _income(row) -> dict:
    d = dict(row)
    d["variable"] = bool(d["variable"])
    a = d["amount"] or 0
    d["monthly"] = round(a * {"daily": DAYS_PER_MONTH, "week": WEEKS_PER_MONTH, "month": 1, "semester": 1 / 6, "year": 1 / 12, "once": 0}.get(d["period"], 1), 2)
    d["days_to_pay"] = None
    if d["pay_day"]:
        t = date.today()
        def on(y, m):
            last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
            return date(y, m, min(d["pay_day"], last))
        nxt = on(t.year, t.month)
        if nxt < t:
            nxt = on(t.year + (t.month == 12), t.month % 12 + 1)
        d["days_to_pay"] = (nxt - t).days
    return d


class Income(BaseModel):
    name: str | None = None
    kind: str | None = None
    amount: float | None = None
    period: str | None = None
    variable: bool | None = None
    pay_day: int | None = None
    note: str | None = None


def _clean_income(p: Income, partial: bool) -> dict:
    f = p.model_dump(exclude_unset=True)
    if "name" in f or not partial:
        f["name"] = (f.get("name") or "").strip()[:80]
        if not f["name"]:
            raise HTTPException(400, "Podaj nazwę dochodu.")
    if f.get("kind") is not None and f["kind"] not in INCOME_KINDS:
        f["kind"] = "inne"
    if f.get("period") is not None and f["period"] not in PERIODS - {"day"}:
        f["period"] = "month"
    if f.get("amount") is not None:
        f["amount"] = max(0.0, min(1e7, float(f["amount"])))
    if "pay_day" in f:
        f["pay_day"] = f["pay_day"] if f["pay_day"] and 1 <= f["pay_day"] <= 31 else None
    if f.get("variable") is not None:
        f["variable"] = int(bool(f["variable"]))
    if "note" in f:
        f["note"] = (f["note"] or "")[:300]
    return {k: v for k, v in f.items() if v is not None or k in ("amount", "pay_day")}


@router.post("/api/incomes")
def add_income(p: Income):
    f = _clean_income(p, False)
    with get_conn() as c:
        cur = c.execute(f"INSERT INTO incomes({','.join(f)}) VALUES({','.join('?' * len(f))})", tuple(f.values()))
        return {"id": cur.lastrowid}


@router.patch("/api/incomes/{iid}")
def edit_income(iid: int, p: Income):
    f = _clean_income(p, True)
    if f:
        with get_conn() as c:
            c.execute(f"UPDATE incomes SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), iid))
    return {"ok": True}


@router.delete("/api/incomes/{iid}")
def del_income(iid: int):
    with get_conn() as c:
        c.execute("DELETE FROM incomes WHERE id=?", (iid,))
    return {"ok": True}


@router.get("/api/costs/reminders")
def reminders():
    """Płatności w oknie przypomnienia (albo po terminie) — dla dzwonka i powiadomień."""
    out = [x for x in list_costs()["upcoming"] if x["days_left"] <= (x["remind_days"] or 0)]
    return [{"id": x["id"], "name": x["name"], "category": x["category"], "due_date": x["due_date"],
             "days_left": x["days_left"], "amount": x["per_period"]} for x in out]


class Cost(BaseModel):
    name: str | None = None
    category: str | None = None
    amount: float | None = None
    period: str | None = None
    variable: bool | None = None
    mode: str | None = None
    params: dict | None = None
    due_date: str | None = None
    remind_days: int | None = None
    paid: bool | None = None
    note: str | None = None


def _clean(p: Cost, partial: bool) -> dict:
    f = p.model_dump(exclude_unset=True)
    if "name" in f:
        f["name"] = (f["name"] or "").strip()[:80]
        if not f["name"]:
            raise HTTPException(400, "Podaj nazwę kosztu.")
    elif not partial:
        raise HTTPException(400, "Podaj nazwę kosztu.")
    if f.get("category") is not None and f["category"] not in CATEGORIES:
        f["category"] = "inne"
    if f.get("period") is not None and f["period"] not in PERIODS:
        f["period"] = "month"
    if f.get("mode") is not None and f["mode"] not in MODES:
        f["mode"] = ""
    if "amount" in f and f["amount"] is not None:
        f["amount"] = max(0.0, min(1e7, float(f["amount"])))
    if "params" in f:
        f["params"] = json.dumps({k: v for k, v in (f["params"] or {}).items()
                                  if k in ("km", "l100", "price", "days", "ticket") and isinstance(v, (int, float, str))})
    if "due_date" in f:
        try:
            f["due_date"] = date.fromisoformat(f["due_date"]).isoformat() if f["due_date"] else ""
        except ValueError:
            raise HTTPException(400, "Zły format terminu płatności.")
    if "remind_days" in f and f["remind_days"] is not None:
        f["remind_days"] = max(0, min(60, f["remind_days"]))
    for k in ("variable", "paid"):
        if k in f and f[k] is not None:
            f[k] = int(bool(f[k]))
    if "note" in f:
        f["note"] = (f["note"] or "")[:300]
    return {k: v for k, v in f.items() if v is not None or k == "amount"}


@router.post("/api/costs")
def add_cost(p: Cost):
    f = _clean(p, False)
    with get_conn() as c:
        cur = c.execute(f"INSERT INTO costs({','.join(f)}) VALUES({','.join('?' * len(f))})", tuple(f.values()))
        return {"id": cur.lastrowid}


@router.patch("/api/costs/{cid}")
def edit_cost(cid: int, p: Cost):
    f = _clean(p, True)
    if not f:
        return {"ok": True}
    with get_conn() as c:
        if not c.execute("SELECT 1 FROM costs WHERE id=?", (cid,)).fetchone():
            raise HTTPException(404, "Nie ma takiego kosztu.")
        c.execute(f"UPDATE costs SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), cid))
    return {"ok": True}


@router.delete("/api/costs/{cid}")
def del_cost(cid: int):
    with get_conn() as c:
        c.execute("DELETE FROM cost_entries WHERE cost_id=?", (cid,))
        c.execute("DELETE FROM costs WHERE id=?", (cid,))
    return {"ok": True}


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


@router.post("/api/costs/{cid}/pay")
def pay_cost(cid: int):
    """Opłacone: płatność cykliczna przesuwa termin o okres, jednorazowa zostaje oznaczona jako zapłacona.
    Kwota trafia do wydatków bieżącego miesiąca."""
    with get_conn() as c:
        r = c.execute("SELECT * FROM costs WHERE id=?", (cid,)).fetchone()
        if not r:
            raise HTTPException(404, "Nie ma takiego kosztu.")
        st = _settings(c)
        amt = _per_period(r, st)
        if amt:
            c.execute("INSERT INTO cost_entries(cost_id, amount, date, note) VALUES(?,?,?,?)",
                      (cid, amt, date.today().isoformat(), "opłacono"))
        step = {"month": 1, "semester": 6, "year": 12}.get(r["period"])
        if r["due_date"] and step:
            nd = _add_months(date.fromisoformat(r["due_date"]), step)
            c.execute("UPDATE costs SET due_date=?, paid=0 WHERE id=?", (nd.isoformat(), cid))
        else:
            c.execute("UPDATE costs SET paid=1 WHERE id=?", (cid,))
    return {"ok": True}


class Entry(BaseModel):
    cost_id: int | None = None
    amount: float
    date: str | None = None
    note: str = ""


@router.post("/api/cost-entries")
def add_entry(e: Entry):
    if not (0 < e.amount < 1e7):
        raise HTTPException(400, "Podaj kwotę większą od zera.")
    try:
        d = date.fromisoformat(e.date).isoformat() if e.date else date.today().isoformat()
    except ValueError:
        raise HTTPException(400, "Zła data.")
    with get_conn() as c:
        cur = c.execute("INSERT INTO cost_entries(cost_id, amount, date, note) VALUES(?,?,?,?)",
                        (e.cost_id, round(e.amount, 2), d, e.note[:200]))
        return {"id": cur.lastrowid}


@router.delete("/api/cost-entries/{eid}")
def del_entry(eid: int):
    with get_conn() as c:
        c.execute("DELETE FROM cost_entries WHERE id=?", (eid,))
    return {"ok": True}


class CostSettings(BaseModel):
    study_days: int | None = None
    savings_goal: float | None = None


@router.post("/api/costs/settings")
def set_cost_settings(s: CostSettings):
    with get_conn() as c:
        if s.study_days is not None:
            c.execute("INSERT OR REPLACE INTO cost_settings(k, v) VALUES('study_days', ?)", (str(max(1, min(7, s.study_days))),))
        if s.savings_goal is not None:
            c.execute("INSERT OR REPLACE INTO cost_settings(k, v) VALUES('savings_goal', ?)", (str(max(0.0, min(1e7, s.savings_goal))),))
    return {"ok": True}
