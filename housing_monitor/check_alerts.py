#!/usr/bin/env python3
"""
Huizenjacht-monitor op basis van e-mailalerts.

Leest de alert-mails van Pararius en Huurwoningen.nl uit Gmail (IMAP), laat Claude
de woningen eruit halen en beoordelen, en mailt de geschikte woningen door.

Gebruik:
  python housing_monitor/check_alerts.py                  # normale run (IMAP + SMTP)
  python housing_monitor/check_alerts.py --eml alert.eml  # één opgeslagen mail testen,
                                                          # print het resultaat, mailt niets

Omgevingsvariabelen: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, DESTINATION_EMAIL (komma-gescheiden
mag), en CLAUDE_CODE_OAUTH_TOKEN (Claude-abonnement, via `claude -p`) of ANTHROPIC_API_KEY
(betalen per gebruik). Optioneel CLAUDE_MODEL.
"""
import email
import hashlib
import imaplib
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import unquote

import anthropic
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as CONFIG  # noqa: E402  zelfde prijs/kamers/gebieden als de scraper

STATE_FILE = Path(__file__).with_name("seen_listings.json")   # GitHub: afgehandelde woningen
QUEUE_FILE = Path(__file__).with_name("queue.json")           # GitHub: wachten op controle thuis
HOME_SEEN_FILE = Path(__file__).with_name("home_seen.json")   # pc thuis: gecontroleerde woningen
LOG_GITHUB_FILE = Path(__file__).with_name("log_github.json") # logboek voor het overzicht in de webapp
LOG_HOME_FILE = Path(__file__).with_name("log_home.json")     # idem, geschreven door de pc thuis
LOG_MAX_ENTRIES = 2000
MODEL = os.environ.get("CLAUDE_MODEL") or "claude-sonnet-4-5"
LOOKBACK_DAYS = 7                            # zo ver terug kijken naar alert-mails
STATE_TTL_DAYS = 90
MAX_EMAIL_CHARS = 60_000                     # langer dan dit: melden i.p.v. stil afkappen

LISTING_ID_RE = re.compile(
    r"(?:pararius\.nl|huurwoningen\.nl)/[a-z-]+/[a-z0-9-]+/([0-9a-f]{8})(?:/|$|\?)")


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- mail uitlezen

def header(msg, name):
    raw = msg.get(name, "")
    return str(make_header(decode_header(raw))) if raw else ""


def email_to_text(msg):
    """Zet een mail om naar platte tekst waarin elke link als [L1], [L2], ... staat.
    Zo hoeft Claude de (lange tracking-)URL's niet over te nemen. Geeft (tekst, {ref: url})."""
    html = plain = None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/html" and html is None:
            html = text
        elif part.get_content_type() == "text/plain" and plain is None:
            plain = text

    links = {}
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["style", "script", "head"]):
            tag.decompose()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith("http"):
                continue
            ref = next((r for r, u in links.items() if u == href), None)
            if ref is None:
                ref = f"L{len(links) + 1}"
                links[ref] = href
            label = a.get_text(" ", strip=True) or (a.img.get("alt", "") if a.img else "") or "link"
            a.replace_with(f"{label} [{ref}]")
        text = soup.get_text("\n", strip=True)
    else:
        text = plain or ""
        def repl(m):
            ref = f"L{len(links) + 1}"
            links[ref] = m.group(0)
            return f"[{ref}]"
        text = re.sub(r"https?://\S+", repl, text)
    return text, links


def listing_id_from_url(url):
    """Het 8-teken-id van Pararius/Huurwoningen, ook als het in een tracking-link verstopt zit."""
    for candidate in (url, unquote(url), unquote(unquote(url))):
        m = LISTING_ID_RE.search(candidate)
        if m:
            return m.group(1)
    return None


def fallback_key(listing):
    """Sleutel op adres + prijs: herkent ook dezelfde woning op de andere site."""
    addr = re.sub(r"[^a-z0-9]", "", (listing.get("address") or "").lower())
    return f"addr:{addr}:{listing.get('price_eur') or '?'}" if addr else None


def postcode_of(listing):
    m = re.search(r"\b(1\d{3})\s?([A-Za-z]{2})\b", listing.get("address") or "")
    return f"{m.group(1)}{m.group(2).upper()}" if m else None


def postcode_key(listing):
    """Postcode + prijs: robuuster dan het adres, want buurtnamen verschillen per site.
    Deze sleutel gebruikt de pc thuis ook, zo herkennen beide bronnen elkaars woningen."""
    pc = postcode_of(listing)
    return f"pc:{pc}:{listing['price_eur']}" if pc and listing.get("price_eur") else None


def listing_keys(listing):
    keys = [k for k in (listing_id_from_url(listing.get("url") or ""), postcode_key(listing),
                        fallback_key(listing)) if k]
    return keys or ["hash:" + hashlib.sha1(json.dumps(listing, sort_keys=True).encode()).hexdigest()[:12]]


# ---------------------------------------------------------------- gratis voorfilter

# Vast formaat van Huurwoningen-alerts:
#   Steve Bikoplein [L2]
#   1092GN Amsterdam (Oud-Oost)
#   € 2.900 per maand
#   80 m²  ·  3 kamers  ·  Gemeubileerd  ·  Appartement
HW_CARD_RE = re.compile(
    r"^(?P<street>[^\n\[]+?) \[(?P<ref>L\d+)\]\n"
    r"(?P<pc>1\d{3}\s?[A-Z]{2}) (?P<city>[^\n(]+?)(?: \((?P<area>[^)\n]+)\))?\n"
    r"€\s?(?P<price>[\d.]+) per maand\n"
    r"(?P<facts>[^\n]*)", re.M)


def parse_known_format(text, links):
    """Woningen uit een alert met bekend formaat, zonder Claude. None = formaat niet herkend."""
    out = []
    for m in HW_CARD_RE.finditer(text):
        facts = m.group("facts")
        rooms = re.search(r"(\d+)\s+kamers?", facts)
        size = re.search(r"(\d+)\s*m²", facts)
        area = f" ({m.group('area')})" if m.group("area") else ""
        out.append({
            "address": f"{m.group('street').strip()}, {m.group('pc')}{area}",
            "city": m.group("city").strip(),
            "price_eur": int(m.group("price").replace(".", "")),
            "rooms": int(rooms.group(1)) if rooms else None,
            "size_m2": int(size.group(1)) if size else None,
            "url": links.get(m.group("ref")),
            "other_details": re.sub(r"\s+", " ", facts).strip(),
        })
    return out or None


def area_of(postcode):
    if not postcode:
        return None
    n = int(postcode[:4])
    for area, ranges in CONFIG.AREAS.items():
        if any(lo <= n <= hi for lo, hi in ranges):
            return area
    return "buiten de ring"


def prefilter(listing):
    """Harde criteria zonder Claude. Geeft een afwijsreden, of None als Claude moet kijken."""
    price, rooms = listing.get("price_eur"), listing.get("rooms")
    if price and price > CONFIG.MAX_PRICE:
        return f"te duur (€{price})"
    if rooms and rooms < CONFIG.MIN_ROOMS:
        return f"te weinig kamers ({rooms})"
    area = area_of(postcode_of(listing))
    if area == "buiten de ring":
        return f"buiten de ring ({postcode_of(listing)})"
    limit = CONFIG.AREA_MAX_PRICE_PER_ROOM.get(area)
    if limit and price and rooms and price / rooms > limit:
        return f"{area} en geen topdeal (€{price / rooms:.0f}/kamer)"
    return None


# ---------------------------------------------------------------- Claude

EXTRACT_TOOL = {
    "name": "report_listings",
    "description": "Geef alle losse woningaanbiedingen die in de alert-mail staan.",
    "input_schema": {
        "type": "object",
        "properties": {
            "listings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "Straat (+ huisnummer indien vermeld), en buurt/postcode als die er staat"},
                        "city": {"type": "string"},
                        "price_eur": {"type": ["integer", "null"], "description": "Huurprijs per maand in euro"},
                        "rooms": {"type": ["integer", "null"], "description": "Totaal aantal kamers"},
                        "bedrooms": {"type": ["integer", "null"]},
                        "size_m2": {"type": ["integer", "null"]},
                        "link_ref": {"type": ["string", "null"], "description": "De [Lx]-verwijzing van de link naar deze woning, bv. 'L3'"},
                        "income_requirement_text": {"type": ["string", "null"], "description": "Letterlijke tekst over inkomenseisen, indien aanwezig"},
                        "other_details": {"type": ["string", "null"], "description": "Overige relevante tekst uit de mail over deze woning (beschrijving, voorwaarden)"},
                    },
                    "required": ["address", "price_eur", "rooms", "link_ref"],
                },
            }
        },
        "required": ["listings"],
    },
}

JUDGE_TOOL = {
    "name": "judge_listing",
    "description": "Geef je oordeel over deze woning.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["fit", "geen fit", "twijfel"]},
            "reason": {"type": "string", "description": "1-2 zinnen onderbouwing, in het Nederlands"},
            "risks": {"type": "array", "items": {"type": "string"},
                      "description": "Expliciete risico's, bv. 'eis: moet studeren in Amsterdam'"},
        },
        "required": ["verdict", "reason", "risks"],
    },
}

def fallback_minutes():
    """Zo lang wacht een kandidaat op controle door de pc thuis; daarna mailt GitHub hem
    ongecontroleerd. Instelbaar in de webapp; FALLBACK_MINUTES in de omgeving wint."""
    return int(os.environ.get("FALLBACK_MINUTES") or CONFIG.FALLBACK_MINUTES)


def reject_rules():
    """De afwijsregels uit settings.json als tekst voor Claude."""
    rules = [text for on, text in (
        (CONFIG.REJECT_NO_STUDENTS, "de verhuurder geen studenten wil"),
        (CONFIG.REJECT_NO_GUARANTOR, "een garantsteller niet geaccepteerd wordt"),
        (CONFIG.REJECT_NO_SHARING, "woningdelers niet toegestaan zijn"),
        (CONFIG.REJECT_SWAP, "het een woningruil is"),
    ) if on]
    return ("Afwijzen (\"geen fit\") als de advertentie zegt dat " + "; of dat ".join(rules) + ".") if rules else ""


def judge_system():
    """Systeemprompt voor het oordeel, met de criteria en afwijsregels uit settings.json."""
    return f"""\
Je beoordeelt huurwoningen in Amsterdam voor een groep studenten.

Zoekcriteria:
{CONFIG.CRITERIA_TEXT}
{reject_rules()}

Hulp bij de locatie: binnen de A10-ring liggen o.a. Centrum, West (Oud-West, \
De Baarsjes, Westerpark, Bos en Lommer), Zuid (Oud-Zuid, De Pijp, Rivierenbuurt) en \
Oost binnen de ring (Oosterparkbuurt, Dapperbuurt, Indische Buurt, Watergraafsmeer, \
Oostelijk Havengebied). Buiten de ring liggen o.a. Noord, Nieuw-West (Slotervaart, Osdorp, \
Geuzenveld), Zuidoost, Buitenveldert, IJburg en Amstelveen. Postcodes binnen de ring \
beginnen grofweg met 1011-1019, 1051-1059, 1071-1079 en 1091-1098.

Oordeel:
- "geen fit": een criterium wordt duidelijk niet gehaald.
- "fit": alle bekende gegevens passen.
- "twijfel": iets is onduidelijk of grensgeval (bv. Oost zonder duidelijk goede deal, \
locatie niet te bepalen, inkomenseis die misschien te hoog is).
Een alert-mail bevat vaak weinig details. Ontbrekende informatie is geen reden voor \
"geen fit"; noem wat je niet kon controleren in de onderbouwing."""


def call_tool(client, tool, system, content, max_tokens):
    """Laat Claude een JSON-object volgens tool['input_schema'] teruggeven.
    client=None: via de Claude Code CLI met je abonnement (CLAUDE_CODE_OAUTH_TOKEN)."""
    if client is None:
        return call_cli(tool, system, content)
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError(f"Claude-antwoord afgekapt (max_tokens) bij {tool['name']}")
    for block in response.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return block.input
    raise RuntimeError(f"Claude gaf geen {tool['name']}-resultaat (stop_reason={response.stop_reason})")


def call_cli(tool, system, content):
    """`claude -p` met een eigen korte systeemprompt, zonder tools, MCP of instellingen.
    Zonder die opties laadt Claude Code zijn volledige standaardcontext (~20-80k tokens per
    aanroep), wat de limiet van een abonnement snel opmaakt. In een lege map, zodat
    tekst uit een mail nergens bij kan."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("Claude Code CLI niet gevonden (npm install -g @anthropic-ai/claude-code)")
    # Systeemprompt via een bestand: regeleinden in een argument breken de aanroep
    # op Windows (claude.cmd), waarna de overige opties stil wegvallen.
    cmd = [exe, "-p", tool["description"],
           "--system-prompt-file", "system.txt",
           "--output-format", "json",
           "--json-schema", json.dumps(tool["input_schema"]),
           "--model", MODEL,
           "--tools", "",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "",
           "--max-turns", "3",
           "--no-session-persistence"]
    with tempfile.TemporaryDirectory() as empty_dir:
        Path(empty_dir, "system.txt").write_text(system, encoding="utf-8")
        proc = subprocess.run(cmd, input=content, capture_output=True, text=True,
                              encoding="utf-8", cwd=empty_dir, timeout=300,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(f"claude -p gaf geen JSON (exit {proc.returncode}): "
                           f"{(proc.stderr or proc.stdout)[:300]}")
    if out.get("is_error") or out.get("structured_output") is None:
        raise RuntimeError(f"claude -p mislukt ({out.get('subtype')}): {str(out.get('result'))[:300]}")
    return out["structured_output"]


def extract_listings(client, subject, sender, text, links):
    known = parse_known_format(text, links)
    if known:
        return known
    if not re.search(r"€\s?\d", text):       # bv. welkomstmail: geen prijzen, dus geen woningen
        return []
    if len(text) > MAX_EMAIL_CHARS:
        raise RuntimeError(f"mail te lang ({len(text)} tekens) om in één keer te verwerken")
    data = call_tool(
        client, EXTRACT_TOOL,
        "Je haalt woningaanbiedingen uit alert-mails van Pararius en Huurwoningen.nl. "
        "Neem alleen echte woningen op, geen advertenties, knoppen of links naar zoekpagina's. "
        "Verzin geen gegevens: onbekend is null.",
        f"Afzender: {sender}\nOnderwerp: {subject}\n\n{text}",
        max_tokens=8000,
    )
    listings = []
    for l in data.get("listings", []):
        l["url"] = links.get((l.get("link_ref") or "").strip("[] "))
        listings.append(l)
    return listings


def judge_listing(client, listing):
    facts = {k: listing.get(k) for k in
             ("address", "city", "price_eur", "rooms", "bedrooms", "size_m2",
              "income_requirement_text", "other_details")}
    return call_tool(
        client, JUDGE_TOOL, judge_system(),
        "Beoordeel deze woning:\n" + json.dumps(facts, ensure_ascii=False, indent=1),
        max_tokens=1000,
    )


# ---------------------------------------------------------------- samenvattingsmail

def fmt_price(p):
    return f"€{p:,}".replace(",", ".") if p else "prijs onbekend"


def build_summary(results, checked=False):
    """results: [{"listing", "verdict", optioneel "warnings"}]. checked=False: gegevens komen
    alleen uit de alert-mail, de advertentie zelf is niet gelezen."""
    order = {"fit": 0, "twijfel": 1}
    results = sorted(results, key=lambda r: order[r["verdict"]["verdict"]])
    fits = sum(r["verdict"]["verdict"] == "fit" for r in results)
    twijfel = len(results) - fits
    rows = []
    for r in results:
        l, v = r["listing"], r["verdict"]
        badge = ("<span style='color:#15803d'>✅ fit</span>" if v["verdict"] == "fit"
                 else "<span style='color:#b45309'>🤔 twijfel</span>")
        facts = " · ".join(x for x in [
            fmt_price(l.get("price_eur")) + " p/m",
            f"{l['rooms']} kamers" if l.get("rooms") else None,
            f"{l['size_m2']} m²" if l.get("size_m2") else None,
        ] if x)
        title = escape(l.get("address") or "Onbekend adres")
        link = (f"<a href='{escape(l['url'])}' style='font-size:16px;font-weight:600'>{title}</a>"
                if l.get("url") else f"<b>{title}</b>")
        risks = "".join(f"<div style='color:#b91c1c'>⚠️ {escape(x)}</div>"
                        for x in list(r.get("warnings", [])) + list(v.get("risks", [])))
        rows.append(f"<div style='margin:0 0 18px'>{badge} {link}<div>{escape(facts)}</div>"
                    f"<div style='color:#444'>{escape(v['reason'])}</div>{risks}</div>")
    if fits:
        subject = f"🏠 {fits} passende woning{'en' if fits != 1 else ''}" + (
            f" (+{twijfel} twijfel)" if twijfel else "")
    else:
        subject = f"🤔 {twijfel} twijfelgeval{'len' if twijfel != 1 else ''}"
    if checked:
        note = "<p style='color:#15803d'>✔ Advertentie gelezen en gecontroleerd.</p>"
    else:
        subject += " – niet gecontroleerd"
        note = ("<p style='color:#b45309'>Advertentie niet gecontroleerd (pc thuis stond uit): "
                "dit is alleen beoordeeld op de gegevens uit de alert-mail. Check zelf op "
                "studenten, garantsteller en inkomenseis.</p>")
    return subject, (f"<html><body style='font-family:sans-serif'>{note}{''.join(rows)}"
                     f"</body></html>")


def send_email(subject, html):
    user, pw = os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"]
    to = [x.strip() for x in os.environ["DESTINATION_EMAIL"].split(",") if x.strip()]
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, to, msg.as_string())
    log(f"Samenvatting verstuurd naar {', '.join(to)}: {subject}")


# ---------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        state = {}
    state.setdefault("listings", {})
    state.setdefault("mails", {})
    return state


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def log_event(source, site, listing, result, stage, reason):
    """Eén regel voor het overzicht in de webapp. listing: dict met address/price_eur/rooms/..."""
    pc = postcode_of(listing)
    url = listing.get("url") or ""
    if "track." not in url:          # tracking-links hebben hun query nodig; andere niet
        url = url.split("?")[0]
    return {
        "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source, "site": site,
        "name": listing.get("address"), "postcode": pc, "area": area_of(pc),
        "price": listing.get("price_eur"), "rooms": listing.get("rooms"), "size": listing.get("size_m2"),
        "result": result, "stage": stage, "reason": (reason or "")[:300],
        "url": url,
    }


def save_log(path, events, sites=None):
    """Voegt regels toe aan een logboek (max LOG_MAX_ENTRIES, max STATE_TTL_DAYS oud)."""
    data = load_json(path, {"entries": [], "sites": {}})
    data["entries"].extend(events)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STATE_TTL_DAYS)).isoformat()
    data["entries"] = [e for e in data["entries"] if e["t"] >= cutoff][-LOG_MAX_ENTRIES:]
    if sites:
        data["sites"].update(sites)
    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json(path, data)


def sender_site(sender):
    s = sender.lower()
    for name in ("pararius", "huurwoningen", "funda", "kamernet"):
        if name in s:
            return name.capitalize()
    return sender.split("<")[0].strip().strip('"') or "Mail"


def home_keys():
    """Sleutels die de pc thuis al heeft afgehandeld (alleen lezen; de pc schrijft dit bestand)."""
    return set(load_json(HOME_SEEN_FILE, {}).get("listings", {}))


def save_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STATE_TTL_DAYS)).isoformat()
    for key in ("listings", "mails"):
        state[key] = {k: v for k, v in state[key].items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False),
                          encoding="utf-8")


# ---------------------------------------------------------------- verwerking

def process_message(client, msg, state, known, events=None):
    """Verwerkt één alert-mail. `known` = sleutels die al ergens zijn afgehandeld of in de
    wachtlijst staan. Geeft (kandidaten voor de wachtlijst, sleutels om als afgehandeld op
    te slaan, aantal nieuwe woningen). Gooit een exceptie als iets misgaat; de mail wordt
    dan de volgende run opnieuw geprobeerd."""
    subject, sender = header(msg, "Subject"), header(msg, "From")
    text, links = email_to_text(msg)
    listings = extract_listings(client, subject, sender, text, links)
    log(f"  '{subject[:70]}': {len(listings)} woning(en) gevonden")
    site = sender_site(sender)
    events = events if events is not None else []

    candidates, done_keys, new = [], [], 0
    for l in listings:
        keys = listing_keys(l)
        if any(k in known for k in keys):
            log(f"    = {l.get('address')} (al eerder gezien)")
            continue
        new += 1
        known.update(keys)                      # dubbelingen binnen dezelfde run overslaan
        reason = prefilter(l)
        if reason:
            log(f"    voorfilter {l.get('address')}: {reason}")
            events.append(log_event("mail", site, l, "afgewezen", "voorfilter", reason))
            done_keys.extend(keys)
            continue
        verdict = judge_listing(client, l)
        log(f"    {verdict['verdict']:9} {l.get('address')} {fmt_price(l.get('price_eur'))}"
            f" {l.get('rooms')}k: {verdict['reason']}")
        if verdict["verdict"] in ("fit", "twijfel"):
            candidates.append({"keys": keys, "listing": l, "verdict": verdict, "site": site})
            events.append(log_event("mail", site, l, "wachtlijst", "Claude (mail)", verdict["reason"]))
        else:
            events.append(log_event("mail", site, l, "afgewezen", "Claude (mail)", verdict["reason"]))
            done_keys.extend(keys)
    return candidates, done_keys, new


def fetch_alert_messages(imap, state):
    """Alert-mails van de afgelopen dagen die nog niet verwerkt zijn: [(imap_id, message_id, msg)].
    Ook al-gelezen mails tellen mee (je kan de alert zelf al op je telefoon geopend hebben)."""
    imap.select("INBOX")
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    ids = set()
    for sender in CONFIG.ALERT_SENDERS:
        typ, data = imap.search(None, "SINCE", since, "FROM", f'"{sender}"')
        if typ == "OK" and data and data[0]:
            ids.update(data[0].split())
    found, todo = 0, []
    for imap_id in sorted(ids, key=int):
        typ, data = imap.fetch(imap_id, "(BODY.PEEK[])")   # PEEK: nog niet als gelezen markeren
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            continue
        msg = email.message_from_bytes(data[0][1])
        found += 1
        mid = msg.get("Message-ID") or f"imap:{imap_id.decode()}"
        if mid not in state["mails"]:
            todo.append((imap_id, mid, msg))
    log(f"{found} alert-mail(s) van de afgelopen {LOOKBACK_DAYS} dagen, {len(todo)} nieuw")
    return todo


def make_client():
    """Met een API-key: de Anthropic SDK (betalen per gebruik).
    Anders: None = via `claude -p` op je Claude-abonnement."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        log(f"Model: {MODEL} via de API (ANTHROPIC_API_KEY)")
        return anthropic.Anthropic()
    log(f"Model: {MODEL} via je Claude-abonnement (claude -p)")
    return None


def run():
    missing = [k for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "DESTINATION_EMAIL")
               if not os.environ.get(k)]
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        missing.append("CLAUDE_CODE_OAUTH_TOKEN (abonnement) of ANTHROPIC_API_KEY")
    if missing:
        sys.exit(f"Ontbrekende omgevingsvariabelen: {', '.join(missing)}")

    state = load_state()
    queue = load_json(QUEUE_FILE, {"items": []})
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    client = make_client()
    home = home_keys()
    known = set(state["listings"]) | home | {k for it in queue["items"] for k in it["keys"]}

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
    try:
        if CONFIG.SETTINGS_ERROR:
            log(f"LET OP: {CONFIG.SETTINGS_ERROR}")
        todo = fetch_alert_messages(imap, state)
        done, n_new, n_queued, failed, events = [], 0, 0, 0, []
        for imap_id, mid, msg in todo:
            try:
                candidates, done_keys, new = process_message(client, msg, state, known, events)
            except (anthropic.APIError, RuntimeError) as e:
                failed += 1
                log(f"  FOUT bij '{header(msg, 'Subject')[:60]}': {e} (volgende run opnieuw)")
                continue
            n_new += new
            n_queued += len(candidates)
            for c in candidates:
                c["queued_at"] = now
                queue["items"].append(c)
            for k in done_keys:
                state["listings"][k] = now
            done.append((imap_id, mid))
        log(f"Resultaat: {n_new} nieuwe woning(en), {n_queued} naar de wachtlijst, "
            f"{failed} mislukte mail(s)")

        # Wachtlijst: wat de pc thuis al gecontroleerd heeft valt eraf; wat te lang wacht
        # wordt ongecontroleerd gemaild.
        keep, overdue = [], []
        deadline = (now_dt - timedelta(minutes=fallback_minutes())).isoformat()
        for it in queue["items"]:
            if any(k in home for k in it["keys"]):
                log(f"  ✔ {it['listing'].get('address')}: al gecontroleerd door de pc thuis")
                for k in it["keys"]:
                    state["listings"][k] = now
            elif it["queued_at"] <= deadline:
                overdue.append(it)
            else:
                keep.append(it)
        log(f"Wachtlijst: {len(keep)} wacht op de pc thuis, {len(overdue)} te lang gewacht")

        # Eerst mailen, dan pas opslaan: als het mailen faalt, komt alles de volgende run opnieuw.
        if overdue:
            send_email(*build_summary(overdue, checked=False))
            for it in overdue:
                for k in it["keys"]:
                    state["listings"][k] = now
                events.append(log_event("mail", it.get("site", "Mail"), it["listing"],
                                        "gemaild (niet gecontroleerd)", "vangnet", it["verdict"]["reason"]))
        queue["items"] = keep

        for imap_id, mid in done:
            state["mails"][mid] = now
        save_state(state)
        save_json(QUEUE_FILE, queue)
        save_log(LOG_GITHUB_FILE, events)
        for imap_id, _ in done:
            imap.store(imap_id, "+FLAGS", "\\Seen")
        log(f"{len(done)} mail(s) als verwerkt en gelezen gemarkeerd")
        if failed:
            sys.exit(1)                          # rood kruisje in Actions, zodat je het ziet
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def test_eml(path):
    """Verwerkt één opgeslagen .eml-bestand zonder IMAP, SMTP of state."""
    msg = email.message_from_bytes(Path(path).read_bytes())
    client = make_client()
    results, _, _ = process_message(client, msg, {"listings": {}, "mails": {}}, set())
    subject, html = build_summary(results) if results else ("(geen matches)", "")
    log(f"\nZou mailen: {subject}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--eml":
        test_eml(sys.argv[2])
    else:
        run()
