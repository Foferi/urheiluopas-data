#!/usr/bin/env python3
"""
Urheiluoppaan ohjelmatietojen rakentaja.

Hakee otteluohjelmat ja lähetykset (TheSportsDB, Katsomo, NHL, Viaplay ja HBO Max),
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
import re
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
KATSOMO_LIIGA_PANEL = "55SV6AGGC0BmnIEaqigd96"
KATSOMO_PANEL_QUERY = "9ff092585150e045a6b98a87ec217b3447b98818c7384081f916c029a6b1caf8"
KATSOMO_CLIENT_VERSION = "5.5.0"
NHL_SCHEDULE = "https://api-web.nhle.com/v1/schedule"
NHL_VIAPLAY = {
    "FI": ("viaplay", "https://content.viaplay.fi/pcdash-fi/urheilu/jaakiekko/nhl?language=fi",
           "Tämänhetkiset lähetykset"),
    "SE": ("viaplay_se", "https://content.viaplay.se/pcdash-se/sport/ishockey/nhl?language=sv",
           "Aktuella sändningar"),
}


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
    channel_starts: dict[str, datetime] = field(default_factory=dict)
    tournament: str = ""                # automaattisen snooker- tai dartssession nimi

    def key(self) -> str:
        """Tunniste, jolla sama tapahtuma tunnistetaan eri lähteistä."""
        if self.tournament:
            return f"{self.competition}|{self.start.isoformat()}|{norm(self.tournament)}"
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


def load_json(url: str, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "urheiluopas-data/1.0", **(headers or {})})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def katsomo_liiga_cards() -> list[dict]:
    cards = []
    offset = 0
    while True:
        query = urllib.parse.urlencode({
            "operationName": "Panel",
            "variables": json.dumps({
                "panelId": KATSOMO_LIIGA_PANEL, "limit": 20, "offset": offset, "includeProgress": False,
            }),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": KATSOMO_PANEL_QUERY}}),
        })
        response = load_json(f"https://nordic-gateway.mtv.a2d.tv/graphql?{query}", {
            "client-name": "mtv-web", "client-version": KATSOMO_CLIENT_VERSION,
            "Origin": "https://www.mtv.fi",
        })
        if response.get("errors"):
            raise ValueError(response["errors"][0]["message"])
        content = response["data"]["panel"]["content"]
        cards.extend(card["content"] for card in content["cards"])
        page = content["pageInfo"]
        if not page["hasNextPage"]:
            if len(cards) != page["totalCount"]:
                raise ValueError("Katsomon ottelulistasta puuttuu sivu")
            return cards
        if page["nextPageOffset"] <= offset:
            raise ValueError("Katsomon ottelulistan sivutus ei edennyt")
        offset = page["nextPageOffset"]


def reconcile_liiga(events: dict[str, Event], today: date) -> None:
    zone = ZoneInfo(DEFAULT_TZ)

    def key(event: Event) -> tuple[date, str, str]:
        return event.start.astimezone(zone).date(), norm(event.home), norm(event.away)

    confirmed: dict[tuple[date, str, str], Event] = {}
    cards = katsomo_liiga_cards()
    for card in cards:
        if not card or card.get("league") != "Liiga" or not card.get("isLiveContent"):
            continue
        home, separator, away = card["title"].partition(" - ")
        if not separator:
            continue
        home = "Kiekko-Espoo" if home == "K-Espoo" else home
        away = "Kiekko-Espoo" if away == "K-Espoo" else away
        live = next((label for label in card["labels"] if label["style"] == "LIVE"), None)
        starts = live["displayFrom"] if live else card["playableFrom"]
        start = datetime.fromisoformat(starts["isoString"])
        item = Event("liiga", start, home, away, source="katsomo", channel_starts={"mtv": start})
        match_key = key(item)
        if match_key not in confirmed or start < confirmed[match_key].start:
            confirmed[match_key] = item

    if not confirmed:
        if cards:
            raise ValueError("Katsomon Liiga-listasta ei löytynyt yhtään tunnistettua ottelua")
        print("Katsomon Liiga-lista on tyhjä.")
        return

    last_listed_day = max(match_key[0] for match_key in confirmed)
    covered_days = {match_key[0] for match_key in confirmed if today < match_key[0] < last_listed_day}
    for event_key, event in list(events.items()):
        if event.competition != "liiga":
            continue
        match_key = key(event)
        verified = confirmed.pop(match_key, None)
        if verified:
            event.channel_starts["mtv"] = verified.start
        elif match_key[0] in covered_days:
            print(f"  ! Liiga {match_key[0]}: ei Katsomon listalla: {event.home} - {event.away}")
            del events[event_key]

    for event in confirmed.values():
        event.note = {
            "fi": "Listattu aika on Katsomo-lähetyksen alkamisaika.",
            "sv": "Tiden avser sändningsstarten på Katsomo.",
            "en": "The listed time is the start of the Katsomo broadcast.",
        }
        events[event.key()] = event
    print(f"Katsomo: vahvistettu Liigan ohjelma, täydennetty {len(confirmed)} ottelua.")


def nhl_games(first: date, last: date) -> list[dict]:
    games = {}
    day = first
    while day <= last:
        week = load_json(f"{NHL_SCHEDULE}/{day.isoformat()}")
        if not week["gameWeek"]:
            raise ValueError(f"NHL:n ohjelmasta puuttuu viikko {day}")
        for slate in week["gameWeek"]:
            for game in slate["games"]:
                if game.get("gameScheduleState") == "OK" and game["gameType"] in (2, 3):
                    games[game["id"]] = game
        next_day = date.fromisoformat(week["nextStartDate"])
        if next_day <= day:
            raise ValueError("NHL:n ohjelman sivutus ei edennyt")
        day = next_day
    return list(games.values())


def viaplay_products(url: str, title: str) -> list[dict]:
    blocks = load_json(url)["_embedded"]["viaplay:blocks"]
    block = next((item for item in blocks if item.get("title") == title), None)
    if block is None:
        raise ValueError(f"Viaplayn ohjelmalista puuttuu ({url})")
    products = []
    visited = {url}
    while True:
        products.extend(block["_embedded"]["viaplay:products"])
        next_url = block.get("_links", {}).get("next", {}).get("href")
        if not next_url:
            return products
        if next_url in visited or urllib.parse.urlsplit(next_url).hostname != urllib.parse.urlsplit(url).hostname:
            raise ValueError(f"Viaplayn ohjelmalistan sivutus on virheellinen ({url})")
        visited.add(next_url)
        block = load_json(next_url)


def nhl_team(team: dict) -> tuple[str, set[str]]:
    place = team["placeName"]["default"]
    nickname = team["commonName"]["default"]
    name = f"{place} {nickname}"
    aliases = {place, nickname, name, team["abbrev"]}
    if place == "New York":
        aliases = {nickname, name, team["abbrev"], f"NY {nickname}"}
    return name, {norm(alias) for alias in aliases}


def fetch_nhl_events(days: list[date]) -> list[Event]:
    games = nhl_games(days[0] - timedelta(days=1), days[-1] + timedelta(days=1))
    candidates = []
    for game in games:
        home, home_aliases = nhl_team(game["homeTeam"])
        away, away_aliases = nhl_team(game["awayTeam"])
        start = datetime.fromisoformat(game["startTimeUTC"])
        event = Event("nhl", start, home, away, source="nhl_viaplay")
        candidates.append((event, home_aliases, away_aliases))

    for country, (channel, url, title) in NHL_VIAPLAY.items():
        unmatched = []
        for product in viaplay_products(url, title):
            if product.get("type") != "sport":
                continue
            label = product["content"]["title"]
            parts = re.split(r"\s+[-–—]\s+", re.sub(r"\s*\([^()]*\)\s*$", "", label), maxsplit=1)
            if len(parts) != 2:
                continue
            epg = product["epg"]
            broadcast_start = datetime.fromisoformat(epg.get("streamStart") or epg["start"])
            left, right = map(norm, parts)
            matches = [
                event for event, home, away in candidates
                if abs(event.start - broadcast_start) <= timedelta(hours=2)
                and ((left in home and right in away) or (left in away and right in home))
            ]
            if len(matches) != 1:
                if days[0] <= broadcast_start.date() <= days[-1]:
                    unmatched.append(label)
                continue
            event = matches[0]
            channels = event.extra_channels.setdefault(country, [])
            if channel not in channels:
                channels.append(channel)
            event.channel_starts[channel] = min(broadcast_start, event.channel_starts.get(channel, broadcast_start))
        if unmatched:
            print(f"  ! NHL {country}: lähetykselle ei löytynyt yksiselitteistä ottelua: {', '.join(unmatched)}",
                  file=sys.stderr)
    return [event for event, _, _ in candidates if event.extra_channels]


def viaplay_darts_sessions(url: str, block_title: str):
    for product in viaplay_products(url, block_title):
        if product.get("type") != "sport":
            continue
        content, epg = product["content"], product.get("epg") or {}
        yield content.get("title"), epg.get("streamStart") or epg.get("start"), (
            epg.get("streamEnd") or epg.get("end")
        ), (content.get("originalTitle") or "").split("|", 1)[0].strip()


def hbo_snooker_sessions(url: str, _block_title: str):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        html = response.read().decode("utf-8")
    script = re.search(r'<script\b[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not script:
        raise ValueError("HBO Maxin ohjelmatiedot puuttuvat sivulta")
    blocks = json.loads(script[1])["props"]["pageProps"]["mappedData"]
    for block in blocks.values():
        if not isinstance(block, dict):
            continue
        if (block.get("title") or {}).get("full") != "EMEA | Snooker | Live and Upcoming":
            continue
        for item in block["items"]:
            if item.get("sport") != "Snooker" or item.get("eventStatus") not in {"live", "upcoming", "scheduled"}:
                continue
            dates = item.get("scheduleDates") or {}
            short_title = (item.get("title") or {}).get("short") or ""
            yield item.get("league"), dates.get("startDate"), dates.get("endDate"), short_title.partition("|")[2].strip()


def fetch_broadcast_sessions(days: list[date]) -> list[Event]:
    feeds = [
        ("darts_pdc", "FI", "viaplay", viaplay_darts_sessions,
         "https://content.viaplay.fi/pcdash-fi/urheilu/darts/pdc-darts?language=fi", "Tämänhetkiset lähetykset"),
        ("darts_pdc", "SE", "viaplay_se", viaplay_darts_sessions,
         "https://content.viaplay.se/pcdash-se/sport/dart/pdc-dart?language=sv", "Aktuella sändningar"),
        ("snooker_wst", "FI", "hbo", hbo_snooker_sessions, "https://www.hbomax.com/fi/fi/sports/snooker", ""),
        ("snooker_wst", "SE", "hbo_se", hbo_snooker_sessions, "https://www.hbomax.com/se/sv/sports/snooker", ""),
    ]
    sessions: list[Event] = []
    for competition, country, channel, fetch, url, block in feeds:
        zone = ZoneInfo({"FI": DEFAULT_TZ, "SE": "Europe/Stockholm"}[country])
        try:
            listings = list(fetch(url, block))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
            print(f"  ! {country} {competition}: lähetyslistan haku epäonnistui: {err}", file=sys.stderr)
            continue
        for name, starts_at, ends_at, phase in listings:
            if not (name and starts_at and ends_at):
                continue
            try:
                start = datetime.fromisoformat(starts_at)
                end = datetime.fromisoformat(ends_at)
                if start.tzinfo is None or end.tzinfo is None or end <= start:
                    continue
            except (ValueError, TypeError):
                continue
            start = (start.astimezone(timezone.utc) + timedelta(seconds=30)).replace(second=0, microsecond=0)
            if start.astimezone(zone).date() not in days:
                continue
            duration = max(1, round((end - start).total_seconds() / 60))
            event = next((event for event in sessions if event.competition == competition
                          and norm(event.tournament) == norm(name)
                          and abs(event.start - start) <= timedelta(minutes=5)), None)
            if event is None:
                title = {lang: name for lang in LANGS}
                event = Event(competition, start, title=title, duration=duration,
                              source="viaplay" if competition == "darts_pdc" else "hbo_max", tournament=name)
                sessions.append(event)
            if phase:
                event.title[country.lower()] = f"{name} – {phase}"
                if phase.casefold() in {"karsinta", "kval"}:
                    event.title["en"] = f"{name} – qualifying"
            event.duration = max(event.duration, duration)
            event.extra_channels[country] = [channel]
            event.channel_starts[channel] = min(start, event.channel_starts.get(channel, start))
        print(f"{country} {competition}: {sum(country in e.extra_channels and e.competition == competition for e in sessions)} sessiota.")
    return sessions


def manual_events(manual: dict) -> list[Event]:
    out = []
    for m in manual.get("events") or []:
        tz = ZoneInfo(m.get("tz", DEFAULT_TZ))
        start = datetime.fromisoformat(m["start"]).replace(tzinfo=tz).astimezone(timezone.utc)
        out.append(Event(
            competition=m["competition"], start=start,
            home=m.get("home", ""), away=m.get("away", ""),
            title=m.get("title"), duration=m.get("duration"),
            channel_starts={
                channel: datetime.fromisoformat(value).replace(tzinfo=tz).astimezone(timezone.utc)
                for channel, value in (m.get("channelStarts") or {}).items()
            },
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
        if "nhl" in rights:
            try:
                for e in fetch_nhl_events(days):
                    events[e.key()] = e
            except (OSError, ValueError, KeyError, TypeError) as err:
                sys.exit(f"NHL:n ohjelmatietojen haku epäonnistui: {err}")
    if fetch:
        for e in fetch_broadcast_sessions(days):
            events[e.key()] = e
    for e in manual_events(manual):          # käsin syötetty voittaa lähteen
        title = (e.title or {}).get("en", "")
        tournament = re.split(r"[:|]|\s[–—-]\s", title, maxsplit=1)[0]
        matching_sessions = [
            key for key, auto in events.items()
            if auto.tournament and auto.competition == e.competition
            and norm(auto.tournament) == norm(tournament)
            and abs(auto.start - e.start) <= timedelta(minutes=10)
        ]
        for key in matching_sessions:
            session = events.pop(key)
            e.extra_channels.update(session.extra_channels)
            e.channel_starts = session.channel_starts | e.channel_starts
        previous = events.get(e.key())
        if previous and previous.competition == "nhl":
            e.extra_channels = previous.extra_channels
            e.channel_starts = previous.channel_starts | e.channel_starts
        events[e.key()] = e
    if fetch and "liiga" in rights:
        try:
            reconcile_liiga(events, today)
        except (OSError, ValueError, KeyError, TypeError) as err:
            sys.exit(f"Katsomon Liiga-ohjelman tarkistus epäonnistui: {err}")
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
            default_channels = [] if e.tournament else (comp["broadcast"].get(code) or [])
            chans = list(dict.fromkeys([*default_channels, *e.extra_channels.get(code, [])]))
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
                broadcast_start = e.channel_starts.get(c, e.start)
                day["broadcasts"].append({"matchId": mid, "channelId": c, "countryCode": code,
                                          "startTimeUtc": broadcast_start.isoformat().replace("+00:00", "Z"), "isLive": True})
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
    p.add_argument("--no-fetch", action="store_true", help="käytä vain käsin ylläpidettyjä ohjelmatietoja")
    p.add_argument("--discover", metavar="SPORT", help="listaa lähteen sarjanimet, esim. Soccer, Darts, Snooker")
    p.add_argument("--date", type=date.fromisoformat, default=date.today())
    a = p.parse_args()
    if a.discover:
        discover(a.discover, a.date)
    else:
        build(a.out, a.days, fetch=not a.no_fetch)


if __name__ == "__main__":
    main()
