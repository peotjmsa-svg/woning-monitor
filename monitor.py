#!/usr/bin/env python3
"""
Woning-monitor: checkt Pararius en Huurwoningen.nl op nieuwe huurwoningen,
leest de beschrijving (studenten/garantsteller/inkomenseis) en mailt matches.

Gebruik:
  python monitor.py                 # normale run
  python monitor.py --test <url>    # één woning beoordelen en resultaat printen
Zonder GMAIL_* variabelen wordt de mail naar de terminal geprint i.p.v. verstuurd.
"""
import json
import os
import random
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

import config as C

STATE_FILE = Path(__file__).with_name("state.json")
SEEN_TTL_DAYS = 60

# Beide sites zitten achter Cloudflare, dat gewone Python-requests blokkeert.
# curl_cffi bootst de TLS-handdruk van Chrome na; User-Agent e.d. zet het zelf.
HEADERS = {"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"}


def new_session():
    return requests.Session(impersonate="chrome")

POSTCODE_RE = re.compile(r"\b(1\d{3})\s?([A-Z]{2})\b")
LISTING_PATH_RE = re.compile(r"/([0-9a-f]{8})/[a-z0-9-]+/?$")


class Blocked(Exception):
    pass


# ---------------------------------------------------------------- fetching

def fetch(session, url):
    r = session.get(url, headers=HEADERS, timeout=30)
    head = r.text[:3000].lower()
    if r.status_code in (403, 429) or "just a moment" in head or "cf-chl" in head:
        raise Blocked(f"{urlparse(url).netloc} gaf {r.status_code}")
    r.raise_for_status()
    return r.text


def polite_sleep():
    time.sleep(random.uniform(*C.REQUEST_DELAY))


def extract_listing_links(html, base_url):
    """Alle woning-links van een zoekpagina: [(id, url)]. Id = 8 hex tekens.
    Dezelfde woning heeft op Pararius en Huurwoningen een ander id; dubbelingen
    worden na het ophalen herkend via listing_key()."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base_url).netloc
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        p = urlparse(urljoin(base_url, a["href"]))
        if p.netloc != host:
            continue
        m = LISTING_PATH_RE.search(p.path)
        if m and p.path.count("/") >= 4 and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((m.group(1), f"{p.scheme}://{p.netloc}{p.path}"))
    return out


# ---------------------------------------------------------------- parsing

def parse_euro(s):
    m = re.search(r"€\s*([\d.]+)", s or "")
    return int(m.group(1).replace(".", "")) if m else None


def jsonld_description(soup):
    best = ""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            d = obj.get("description") if isinstance(obj, dict) else None
            if isinstance(d, str) and len(d) > len(best):
                best = d
    return best


def listing_key(listing):
    """Sleutel om dezelfde woning op beide sites te herkennen (de ids verschillen)."""
    if listing["postcode"] and listing["price"]:
        return f"pc:{listing['postcode'].replace(' ', '')}:{listing['price']}"
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", attrs={"property": "og:title"})
    title = og["content"] if og and og.get("content") else (soup.title.get_text() if soup.title else url)
    h1 = soup.find("h1")
    name = h1.get_text(" ", strip=True) if h1 else title
    name = re.sub(r"^Te huur:\s*", "", name)

    text = soup.get_text("\n", strip=True)
    start = max(text.find(name), 0)

    price = parse_euro(title)
    if price is None:
        m = re.search(r"€\s*[\d.]+\s*per maand", text[start:])
        price = parse_euro(m.group(0)) if m else None

    pc = POSTCODE_RE.search(text, start)
    postcode = f"{pc.group(1)} {pc.group(2)}" if pc else None

    rooms = None
    # Fallback alleen in de kop, anders pakt hij een getal van "vergelijkbare woningen"
    m = (re.search(r"Aantal kamers\s*\n\s*(\d+)", text)
         or re.search(r"\b(\d+)\s+kamers?\b", text[start:start + 500]))
    if m:
        rooms = int(m.group(1))
    m = re.search(r"Aantal slaapkamers\s*\n\s*(\d+)", text)
    bedrooms = int(m.group(1)) if m else None
    m = re.search(r"Woonoppervlakte\s*\n\s*(\d+)\s*m", text)
    size = int(m.group(1)) if m else None

    desc_el = (soup.select_one(".listing-detail-description__additional")
               or soup.select_one(".listing-detail-description__content")
               or soup.select_one("[class*='listing-detail-description']"))
    if desc_el:
        description = desc_el.get_text("\n", strip=True)
    else:
        i = text.find("\nBeschrijving\n")
        j = text.find("\nOverdracht\n", i + 1) if i >= 0 else -1
        description = text[i:j] if i >= 0 and j > i else text
    # Huurwoningen kapt de zichtbare tekst af ("De ..."); de JSON-LD heeft 'm volledig
    ld = jsonld_description(soup)
    if len(ld) > len(description):
        description = ld

    return {"url": url, "name": name, "price": price, "postcode": postcode,
            "rooms": rooms, "bedrooms": bedrooms, "size": size,
            "description": description}


# ---------------------------------------------------------------- filters

def any_match(patterns, text):
    return any(re.search(p, text) for p in patterns)


NO_STUDENTS = [
    r"\b(?:geen|niet\s+(?:verhuurd\s+)?(?:aan|voor))\s+studenten",
    r"niet\s+geschikt\s+voor\s+studenten",
    r"studenten\s+(?:zijn\s+|worden\s+)?(?:niet\s+toegestaan|uitgesloten|niet\s+welkom)",
    r"\bgeen\s+(?:\w+\s*/\s*){1,3}studenten",  # "geen delers/ studenten/ garantstelling"
    r"\bno\s+students\b",
    r"not\s+(?:be\s+)?(?:rented|let|leased|available|suitable)\s+(?:out\s+)?(?:to|for)\s+students",
    r"students\s+(?:are\s+)?not\s+(?:allowed|accepted|eligible|permitted)",
    r"(?:working\s+)?professionals\s+only",
    r"alleen\s+(?:voor\s+|aan\s+)?werkenden",
]
NO_GUARANTOR = [
    # Alleen garantst*/guarantor: "geen garanties" staat in elke makelaarsdisclaimer
    r"garantst\w*\s+(?:\([^)]*\)\s+)?(?:wordt|worden|is|zijn)?\s*niet\s+(?:geaccepteerd|toegestaan|mogelijk)",
    r"\bgeen\s+garantst\w*\b(?!\s+(?:nodig|vereist))",
    r"niet\s+(?:geschikt|toegestaan)\s+voor\s+(?:[\w/]+\s+){0,4}garantst",  # "studenten en/of garantstelling"
    r"\bgeen\s+(?:[\w/]+\s+){1,3}(?:en|of|en/of)\s+garantst",               # "geen studenten of garantstellers"
    r"\bgeen\s+(?:\w+\s*/\s*){1,3}garantst",
    r"guarantors?\s+(?:are\s+|is\s+)?not\s+(?:accepted|allowed|possible)",
    r"\bno\s+guarantors?\b(?!\s+(?:needed|required))",
]
NO_SHARING = [
    r"\bgeen\s+(?:woning)?del(?:ers|ing)\b",
    r"niet\s+geschikt\s+(?:om\s+te\s+delen|voor\s+(?:woning)?del(?:en|ers))",
    r"(?:woning)?del(?:en|ers)\s+(?:is\s+|zijn\s+)?niet\s+(?:toegestaan|mogelijk)",
    r"\bno\s+(?:house\s*|flat\s*)?shar(?:ing|ers)\b",
    r"not\s+suitable\s+for\s+(?:house\s*)?shar(?:ing|ers)",
]
SWAP = [
    r"\bwoningruil",
    r"\bruil(?:woning|object|partner)",
    r"\b(?:house|home)\s*swap",
]
NO_FRIENDS_CONTRACT = [
    r"friends?[\s-]*contract\w*\s+(?:wordt|worden|is|zijn)?\s*niet",
    r"\bgeen\s+friends?[\s-]*contract",
    r"\bno\s+friends?[\s-]*contracts?",
]
MAX_TWO = [
    r"maximaal\s+(?:twee|2)\s+(?:volwassen\s+)?(?:personen|huurders|bewoners)",
    r"max(?:imum|\.)?\s*(?:of\s+)?(?:two|2)\s+(?:adult\s+)?(?:persons|people|tenants|occupants)",
    r"\b(?:alleen|only)\s+(?:voor\s+|for\s+)?(?:een\s+)?(?:stel|koppel|couple)",
    r"\b(?:2|twee)\s+(?:woning)?delers\b",                                    # "2 woningdelers toegestaan"
    r"(?:woning)?del(?:en|ers)\s*\(?\s*(?:maximaal|max\.?)\s*(?:2|twee)\b",   # "woningdelers (maximaal 2)"
    r"max(?:imum|\.)?\s*(?:of\s+)?(?:two|2)\s+(?:house\s*)?sharers",
]
INCOME_FROM_WORK = [
    r"inkomen\s+uit\s+(?:werk|arbeid|loondienst)",
    r"(?:vast|permanent)\w*\s+(?:arbeids|employment\s+)?contract",
]
INCOME_WORDS = re.compile(r"inkomen|salaris|income|salary|jaarsalaris")
AMOUNT_RE = re.compile(r"€\s*(\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?![\d])")
K_RE = re.compile(r"\b(\d{2,3})\s?k\b")
MULTIPLE_RE = re.compile(
    r"(\d{1,2}(?:[.,]\d)?)\s*(?:x|×|keer|times)\s*(?:de\s+|the\s+)?"
    r"(?:bruto\s+|netto\s+|kale\s+|gross\s+|net\s+|monthly\s+)?(?:maand)?(?:huur(?:prijs)?|rent)"
)


def income_requirement(text, rent):
    """Geschatte inkomenseis per jaar als (bedrag, "bruto"/"netto"), of None."""
    found = []
    for kw in INCOME_WORDS.finditer(text):
        window = text[max(0, kw.start() - 150): kw.end() + 150]
        for m in AMOUNT_RE.finditer(window):
            amount = int(re.sub(r"[.,]", "", m.group(1)))
            if amount >= 20000:
                found.append(amount)
            elif 2500 <= amount < 20000 and re.search(r"maand|month", window) and amount != rent:
                found.append(amount * 12)
        for m in K_RE.finditer(window):
            found.append(int(m.group(1)) * 1000)
    if rent:
        for m in MULTIPLE_RE.finditer(text):
            k = float(m.group(1).replace(",", "."))
            netto = re.search(r"netto|\bnet\b", text[m.start():m.end() + 30])
            if 2 <= k <= 6:
                amount = int(k * rent * 12)        # x maandhuur als maandinkomen
            elif 24 <= k <= 60:
                amount = int(k * rent)             # x maandhuur als jaarinkomen
            else:
                continue
            found.append((amount, "netto" if netto else "bruto"))
    found = [x if isinstance(x, tuple) else (x, "bruto") for x in found]
    return max(found) if found else None


def classify_area(postcode):
    if not postcode:
        return None
    n = int(postcode[:4])
    for area, ranges in C.AREAS.items():
        if any(lo <= n <= hi for lo, hi in ranges):
            return area
    return "buiten de ring"


def evaluate(listing):
    """Geeft (ok, redenen_afwijzing, waarschuwingen)."""
    reasons, warnings = [], []
    price, rooms = listing["price"], listing["rooms"]
    area = classify_area(listing["postcode"])
    listing["area"] = area

    if price is None:
        warnings.append("prijs niet gevonden")
    elif price > C.MAX_PRICE:
        reasons.append(f"te duur (€{price})")

    if rooms is None:
        warnings.append("aantal kamers niet gevonden")
    elif rooms < C.MIN_ROOMS:
        reasons.append(f"te weinig kamers ({rooms})")

    if area is None:
        if not C.INCLUDE_UNKNOWN_POSTCODE:
            reasons.append("postcode onbekend")
        else:
            warnings.append("postcode niet gevonden, check de locatie")
    elif area == "buiten de ring":
        reasons.append(f"buiten de ring ({listing['postcode']})")
    elif area in C.AREA_MAX_PRICE_PER_ROOM and price and rooms:
        per_room = price / rooms
        limit = C.AREA_MAX_PRICE_PER_ROOM[area]
        if per_room > limit:
            reasons.append(f"{area} en geen topdeal (€{per_room:.0f}/kamer)")

    d = re.sub(r"\s+", " ", listing["description"].lower())
    if any_match(NO_STUDENTS, d):
        (reasons if C.REJECT_NO_STUDENTS else warnings).append("niet voor studenten")
    if any_match(NO_GUARANTOR, d):
        (reasons if C.REJECT_NO_GUARANTOR else warnings).append("garantsteller niet toegestaan")
    if any_match(NO_SHARING, d):
        (reasons if C.REJECT_NO_SHARING else warnings).append("niet voor woningdelers")
    if any_match(SWAP, d):
        (reasons if getattr(C, "REJECT_SWAP", True) else warnings).append("woningruil")
    if any_match(NO_FRIENDS_CONTRACT, d):
        warnings.append("geen friends-contract")
    if any_match(MAX_TWO, d):
        warnings.append("max. 2 personen (niet met z'n drieën)")
    if any_match(INCOME_FROM_WORK, d):
        warnings.append("vraagt inkomen uit werk / vast contract")

    inc = income_requirement(d, price)
    listing["income_req"] = inc
    if inc:
        amount, kind = inc
        if C.INCOME_REJECT_ABOVE and amount > C.INCOME_REJECT_ABOVE:
            reasons.append(f"inkomenseis ~€{amount:,} {kind}".replace(",", "."))
        else:
            warnings.append(f"inkomenseis ~€{amount:,}/jaar {kind}".replace(",", "."))
    elif re.search(r"inkomenseis|inkomensvereiste|income requirement", d):
        warnings.append("inkomenseis (bedrag niet genoemd)")

    return (not reasons), reasons, warnings


# ---------------------------------------------------------------- e-mail

def fmt(n):
    return f"€{n:,}".replace(",", ".") if n else "?"


def build_email(matches, rejected):
    rows = []
    for l, warns in matches:
        facts = " · ".join(x for x in [
            fmt(l["price"]) + " p/m",
            f"{l['rooms']} kamers" if l["rooms"] else None,
            f"{l['bedrooms']} slpk" if l["bedrooms"] else None,
            f"{l['size']} m²" if l["size"] else None,
            f"{l['postcode']} ({l['area']})" if l["postcode"] else None,
        ] if x)
        warn_html = "".join(f"<div style='color:#b45309'>⚠️ {escape(w)}</div>" for w in warns)
        rows.append(
            f"<div style='margin:0 0 18px'><a href='{escape(l['url'])}' style='font-size:16px;font-weight:600'>"
            f"{escape(l['name'])}</a><div>{escape(facts)}</div>{warn_html}</div>")
    body = "".join(rows)
    if rejected:
        items = "".join(
            f"<li><a href='{escape(l['url'])}'>{escape(l['name'])}</a>: {escape(', '.join(r))}</li>"
            for l, r in rejected)
        body += f"<hr><p style='color:#666'>Weggefilterd ({len(rejected)}):</p><ul style='color:#666'>{items}</ul>"
    n = len(matches)
    subject = f"🏠 {n} nieuwe woning{'en' if n != 1 else ''} in Amsterdam"
    return subject, f"<html><body style='font-family:sans-serif'>{body}</body></html>"


def send_email(subject, html):
    user, pw, to = (os.environ.get(k) for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "EMAIL_TO"))
    if not (user and pw):
        print(f"[DRY RUN] {subject}\n{html}\n")
        return
    recipients = [x.strip() for x in (to or user).split(",") if x.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, recipients, msg.as_string())
    print(f"Mail verstuurd: {subject}")


# ---------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": {}}


def save_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_TTL_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


# ---------------------------------------------------------------- main

def run():
    state = load_state()
    seen = state.setdefault("seen", {})
    now = datetime.now(timezone.utc).isoformat()
    session = new_session()
    candidates, blocked = {}, []
    streaks = state.setdefault("fail_streak", {})

    for url in C.SEARCH_URLS:
        problem = None
        try:
            links = extract_listing_links(fetch(session, url), url)
            print(f"{url}: {len(links)} woningen op de pagina")
            if not links:
                problem = "0 woningen gevonden (URL of site veranderd?)"
            for lid, lurl in links:
                if lid not in seen and lid not in candidates:
                    candidates[lid] = lurl
        except Blocked as e:
            problem = str(e)
        except RequestException as e:
            problem = f"fout: {e}"
        if problem:
            print(f"Probleem bij {url}: {problem}")
        # Pas melden na 3 mislukte runs op rij, één time-out is geen probleem
        streaks[url] = streaks.get(url, 0) + 1 if problem else 0
        if problem and streaks[url] >= 3:
            blocked.append(f"{url}: {problem}")
        polite_sleep()
    for url in list(streaks):
        if url not in C.SEARCH_URLS:
            del streaks[url]

    print(f"{len(candidates)} nieuwe woningen om te checken")
    matches, rejected = [], []
    for lid, lurl in list(candidates.items())[:C.MAX_DETAIL_FETCHES_PER_RUN]:
        try:
            listing = parse_detail(fetch(session, lurl), lurl)
        except Blocked as e:
            blocked.append(str(e))
            break
        except RequestException as e:
            print(f"Fout bij {lurl}: {e}")  # niet als gezien markeren, volgende run opnieuw
            continue
        seen[lid] = now
        key = listing_key(listing)
        if key in seen:
            print(f"  = {listing['name']} (al gezien op de andere site)")
            polite_sleep()
            continue
        if key:
            seen[key] = now
        ok, reasons, warnings = evaluate(listing)
        if ok:
            matches.append((listing, warnings))
        else:
            rejected.append((listing, reasons))
        print(f"  {'✓' if ok else '✗'} {listing['name']} {reasons or ''}")
        polite_sleep()

    if matches:
        send_email(*build_email(matches, rejected))

    if blocked:
        print("Geblokkeerd:", blocked)
        last = state.get("last_block_alert", "")
        if last < (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat():
            send_email("⚠️ Woning-monitor heeft een probleem",
                       "<p>" + escape("; ".join(blocked)) + "</p><p>De monitor probeert het "
                       "gewoon verder; deze melding krijg je max. 1x per dag.</p>")
            state["last_block_alert"] = now

    save_state(state)


def test_one(url):
    listing = parse_detail(fetch(new_session(), url), url)
    ok, reasons, warnings = evaluate(listing)
    info = {k: v for k, v in listing.items() if k != "description"}
    print(json.dumps(info, indent=1, ensure_ascii=False))
    print("MATCH" if ok else "AFGEWEZEN", reasons, warnings)


def load_env_file():
    """Leest GMAIL_USER e.d. uit .env naast dit script (voor draaien op je eigen pc)."""
    f = Path(__file__).with_name(".env")
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def log_to_file_if_windowless():
    """Via Taakplanner (pythonw) is er geen console: schrijf de output naar monitor.log.
    Geeft True als er geen console is."""
    if sys.stdout is not None:
        return False
    log = Path(__file__).with_name("monitor.log")
    if log.exists() and log.stat().st_size > 1_000_000:
        log.replace(log.with_suffix(".log.old"))
    sys.stdout = sys.stderr = open(log, "a", encoding="utf-8", buffering=1)
    print(f"\n=== {datetime.now():%Y-%m-%d %H:%M}")
    return True


if __name__ == "__main__":
    windowless = log_to_file_if_windowless()
    load_env_file()
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        test_one(sys.argv[2])
    elif windowless and not (os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD")):
        # Anders worden woningen als gezien gemarkeerd zonder dat je ze gemaild krijgt
        print("Overgeslagen: vul eerst GMAIL_USER en GMAIL_APP_PASSWORD in .env in")
    else:
        run()
