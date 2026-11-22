#!/usr/bin/env python3
"""
Urheiluoppaan ohjelmatietojen rakentaja.

Hakee otteluohjelmat (TheSportsDB) ja käsin ylläpidetyt tapahtumat (data/manual.yaml),
liittää niihin lähetyskanavat oikeustaulukosta (data/rights.yaml) ja kirjoittaa staattiset
JSON-tiedostot, joita sovellus lukee:

  v1/countries.json
  v1/<MAA>/channels.json
  v1/<MAA>/<YYYY-MM-DD>.json     (päivä maan omassa aikavyöhykkeessä)
  v1/meta.json

Käyttö:
  python scripts/build.py --out public
  python scripts/build.py --out public --days 10 --no-fetch     # vain manual.yaml
  python scripts/build.py --discover Soccer --date 2026-11-22    # listaa lähteen sarjanimet
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LANGS = ("fi", "sv", "en")
DEFAULT_TZ = "Europe/Helsinki"
TSDB_BASE = "https://www.thesportsdb.com/api/v1/json"


# ---------------------------------------------------------------------------
# Tietomalli
# ---------------------------------------------------------------------------

@dataclass
class Event:
    competition: str
    start: datetime                      # UTC
    home: str = ""
    away: str = ""
    title: dict[str, str] | None = None  # yksilölajit / sessiot
    duration: int | None = None
    source: str = "manual"
    extra_channels: dict[str, list[str]] = field(default_factory=dict)
    note: dict[str, str] | None = None

    def key(self) -> str:
        """Tunniste, jolla sama tapahtuma tunnistetaan eri lähteistä."""
        label = f"{self.home}|{self.away}" if self.home else (self.title or {}).get("en", "")
        return f"{self.competition}|{self.start.date().isoformat()}|{norm(label)}"

    def stable_id(self) -> str:
        import hashlib
        return "e" + hashlib.sha1(self.key().encode()).hexdigest()[:12]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == "|")


def load_yaml(name: str) -> dict:
    with open(DATA / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Lähteet
# ---------------------------------------------------------------------------

def tsdb_key() -> str:
    # Ilmainen testiavain on rajoitettu; oma avain GitHubin salaisuutena THESPORTSDB_KEY
    return os.environ.get("THESPORTSDB_KEY") or "123"


def tsdb_events_day(day: date, sport: str) -> list[dict]:
    q = urllib.parse.urlencode({"d": day.isoformat(), "s": sport})
    url = f"{TSDB_BASE}/{tsdb_key()}/eventsday.php?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "urheiluopas-data/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.load(r)
    return payload.get("events") or []


def parse_tsdb_start(e: dict) -> datetime | None:
    ts = e.get("strTimestamp")
    if ts:
        ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    d, t = e.get("dateEvent"), e.get("strTime")
    if d and t:
        return datetime.fromisoformat(f"{d}T{t[:8]}").replace(tzinfo=timezone.utc)
    return None


def fetch_provider_events(rights: dict, days: list[date]) -> list[Event]:
    """Hakee TheSportsDB:stä ne sarjat, joille rights.yaml antaa sarjanimen."""
    wanted: dict[tuple[str, str], str] = {}
    for cid, c in rights.items():
        t = c.get("thesportsdb") or {}
        if t.get("sport") and t.get("league"):
            wanted[(t["sport"], t["league"])] = cid
    sports = sorted({s for s, _ in wanted})
    out: list[Event] = []
    for day in days:
        for sport in sports:
            try:
                events = tsdb_events_day(day, sport)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
                print(f"  ! {sport} {day}: haku epäonnistui ({err}); käytetään vain manual.yaml", file=sys.stderr)
                continue
            for e in events:
                cid = wanted.get((sport, e.get("strLeague", "")))
                start = parse_tsdb_start(e)
                if not cid or not start or not e.get("strHomeTeam"):
                    continue
                out.append(Event(cid, start, e["strHomeTeam"], e.get("strAwayTeam") or "", source="thesportsdb"))
    return out


def manual_events(manual: dict) -> list[Event]:
    out = []
    for m in manual.get("events") or []:
        tz = ZoneInfo(m.get("tz", DEFAULT_TZ))
        start = datetime.fromisoformat(m["start"]).replace(tzinfo=tz).astimezone(timezone.utc)
        out.append(Event(
            competition=m["competition"], start=start,
            home=m.get("home", ""), away=m.get("away", ""),
            title=m.get("title"), duration=m.get("duration"),
        ))
    return out


# ---------------------------------------------------------------------------
# Poikkeukset
# ---------------------------------------------------------------------------

def apply_overrides(events: list[Event], manual: dict) -> None:
    for o in manual.get("overrides") or []:
        m = o["match"]
        for e in events:
            if e.competition != m["competition"]:
                continue
            local_day = e.start.astimezone(ZoneInfo(DEFAULT_TZ)).date().isoformat()
            if "date" in m and local_day != m["date"]:
                continue
            if "from" in m and local_day < m["from"]:
                continue
            if "to" in m and local_day > m["to"]:
                continue
            if "home" in m and norm(e.home) != norm(m["home"]):
                continue
            if "away" in m and norm(e.away) != norm(m["away"]):
                continue
            if "titleContains" in m and m["titleContains"].lower() not in json.dumps(e.title or {}, ensure_ascii=False).lower():
                continue
            for country, chans in (o.get("addChannels") or {}).items():
                e.extra_channels.setdefault(country, [])
                e.extra_channels[country] += [c for c in chans if c not in e.extra_channels[country]]
            if o.get("note"):
                e.note = o["note"]


# ---------------------------------------------------------------------------
# Tulosteet
# ---------------------------------------------------------------------------

def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def build(out: Path, days_ahead: int, fetch: bool) -> None:
    rights = load_yaml("rights.yaml")["competitions"]
    channels_cfg = load_yaml("channels.yaml")["countries"]
    manual = load_yaml("manual.yaml")

    today = datetime.now(ZoneInfo(DEFAULT_TZ)).date()
    # Yksi päivä taaksepäin ja eteenpäin varaa aikavyöhyke-eroille
    days = [today + timedelta(days=i) for i in range(-1, days_ahead + 1)]

    events: dict[str, Event] = {}
    if fetch:
        for e in fetch_provider_events(rights, days):
            events[e.key()] = e
    for e in manual_events(manual):          # käsin syötetty voittaa lähteen
        events[e.key()] = e
    unknown = {e.competition for e in events.values()} - set(rights)
    if unknown:
        sys.exit(f"Tuntematon kilpailu manual.yaml:ssa: {', '.join(sorted(unknown))}")
    all_events = sorted(events.values(), key=lambda e: e.start)
    apply_overrides(all_events, manual)

    names = [
        {"entityId": cid, "language": lang, "name": c["names"].get(lang, c["names"]["fi"])}
        for cid, c in rights.items() for lang in LANGS
    ]
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    countries = []
    for code, cfg in channels_cfg.items():
        countries.append({"code": code, "name": cfg["name"]["fi"], "defaultLanguage": cfg["defaultLanguage"],
                          "timeZone": cfg["timeZone"], "enabled": True})
        write_json(out / "v1" / code / "channels.json", [
            {"id": ch["id"], "countryCode": code, "name": ch["name"], "type": "STREAMING",
             "deepLink": ch.get("deepLink"), "free": bool(ch.get("free")), "sortOrder": i}
            for i, ch in enumerate(cfg["channels"])
        ])

        tz = ZoneInfo(cfg["timeZone"])
        valid_channels = {ch["id"] for ch in cfg["channels"]}
        per_day: dict[str, dict] = {}
        for e in all_events:
            comp = rights[e.competition]
            chans = list(comp["broadcast"].get(code) or []) + [
                c for c in e.extra_channels.get(code, []) if c not in (comp["broadcast"].get(code) or [])
            ]
            chans = [c for c in chans if c in valid_channels]
            if not chans:
                continue                      # ei lähetystä tässä maassa
            local_day = e.start.astimezone(tz).date().isoformat()
            day = per_day.setdefault(local_day, {"matches": [], "broadcasts": []})
            mid = e.stable_id()
            note = e.note or (comp.get("note") or {}).get(code)
            day["matches"].append({
                "id": mid,
                "sportId": comp["sport"],
                "leagueId": e.competition,
                "competitionType": comp.get("type", "club"),
                "homeTeam": e.home,
                "awayTeam": e.away,
                "titles": e.title,
                "startTimeUtc": e.start.isoformat().replace("+00:00", "Z"),
                "durationMin": e.duration or comp.get("duration", 120),
                "note": note,
                "source": e.source,
            })
            for c in chans:
                day["broadcasts"].append({"matchId": mid, "channelId": c, "countryCode": code,
                                          "startTimeUtc": e.start.isoformat().replace("+00:00", "Z"), "isLive": True})
        for d in days:
            iso = d.isoformat()
            content = per_day.get(iso, {"matches": [], "broadcasts": []})
            write_json(out / "v1" / code / f"{iso}.json", {
                "generatedAt": generated, "country": code, "date": iso, "names": names, **content,
            })
        print(f"{code}: {sum(len(v['matches']) for v in per_day.values())} tapahtumaa, {len(days)} päivää")

    write_json(out / "v1" / "countries.json", countries)
    write_json(out / "v1" / "meta.json", {"generatedAt": generated, "days": [d.isoformat() for d in days]})
    (out / ".nojekyll").write_text("")      # GitHub Pages: julkaise tiedostot sellaisenaan


def discover(sport: str, day: date) -> None:
    events = tsdb_events_day(day, sport)
    leagues: dict[str, int] = {}
    for e in events:
        leagues[e.get("strLeague", "?")] = leagues.get(e.get("strLeague", "?"), 0) + 1
    print(f"{sport} {day}: {len(events)} tapahtumaa")
    for name, n in sorted(leagues.items(), key=lambda x: -x[1]):
        print(f"  {n:3d}  {name}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=ROOT / "public")
    p.add_argument("--days", type=int, default=7, help="montako päivää eteenpäin")
    p.add_argument("--no-fetch", action="store_true", help="älä hae TheSportsDB:stä")
    p.add_argument("--discover", metavar="SPORT", help="listaa lähteen sarjanimet, esim. Soccer, Darts, Snooker")
    p.add_argument("--date", type=date.fromisoformat, default=date.today())
    a = p.parse_args()
    if a.discover:
        discover(a.discover, a.date)
    else:
        build(a.out, a.days, fetch=not a.no_fetch)


if __name__ == "__main__":
    main()
