#!/usr/bin/env python3
"""
Huizenjacht-monitor op basis van e-mailalerts.

Leest de alert-mails van Pararius en Huurwoningen.nl uit Gmail (IMAP), laat Claude
de woningen eruit halen en beoordelen, en mailt de geschikte woningen door.

Gebruik:
  python housing_monitor/check_alerts.py                  # normale run (IMAP + SMTP)
  python housing_monitor/check_alerts.py --eml alert.eml  # één opgeslagen mail testen,
                                                          # print het resultaat, mailt niets

Omgevingsvariabelen: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ANTHROPIC_API_KEY,
DESTINATION_EMAIL (komma-gescheiden mag), optioneel CLAUDE_MODEL.
"""
import email
import hashlib
import imaplib
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import unquote

import anthropic
from bs4 import BeautifulSoup

STATE_FILE = Path(__file__).with_name("seen_listings.json")
MODEL = os.environ.get("CLAUDE_MODEL") or "claude-sonnet-4-5"
SENDERS = ["pararius", "huurwoningen"]      # matcht op het afzenderadres
LOOKBACK_DAYS = 7                            # zo ver terug kijken naar alert-mails
STATE_TTL_DAYS = 90
MAX_EMAIL_CHARS = 60_000                     # langer dan dit: melden i.p.v. stil afkappen

# Precies de criteria zoals opgegeven; dit gaat letterlijk naar Claude.
CRITERIA = """\
- Locatie: binnen de Amsterdamse A10-ring. Oost mag, maar alleen bij een duidelijk goede deal.
- Budget: max €3000/maand
- Aantal kamers: 4 of meer
- Oppervlakte: maakt niet uit
- We zoeken met 3 personen (2 kan ook, dan wel een extra kamer naast de woonkamer)
- We zijn studenten: listings met een hoge inkomenseis vallen af
- Let op: listings die een "moet studeren in Amsterdam"-eis stellen zijn problematisch — \
een van ons studeert een niet-Amsterdamse master, dus flag dat expliciet als risico i.p.v. \
het gewoon af te keuren"""

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

JUDGE_SYSTEM = f"""\
Je beoordeelt huurwoningen in Amsterdam voor een groep studenten.

Zoekcriteria:
{CRITERIA}

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


def extract_listings(client, subject, sender, text, links):
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
        client, JUDGE_TOOL, JUDGE_SYSTEM,
        "Beoordeel deze woning:\n" + json.dumps(facts, ensure_ascii=False, indent=1),
        max_tokens=1000,
    )


# ---------------------------------------------------------------- samenvattingsmail

def fmt_price(p):
    return f"€{p:,}".replace(",", ".") if p else "prijs onbekend"


def build_summary(results):
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
        risks = "".join(f"<div style='color:#b91c1c'>⚠️ {escape(x)}</div>" for x in v.get("risks", []))
        rows.append(f"<div style='margin:0 0 18px'>{badge} {link}<div>{escape(facts)}</div>"
                    f"<div style='color:#444'>{escape(v['reason'])}</div>{risks}</div>")
    subject = f"🏠 {fits} passende woning{'en' if fits != 1 else ''}" + (
        f" (+{twijfel} twijfel)" if twijfel else "")
    return subject, f"<html><body style='font-family:sans-serif'>{''.join(rows)}</body></html>"


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


def save_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STATE_TTL_DAYS)).isoformat()
    for key in ("listings", "mails"):
        state[key] = {k: v for k, v in state[key].items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False),
                          encoding="utf-8")


# ---------------------------------------------------------------- verwerking

def process_message(client, msg, state):
    """Verwerkt één alert-mail. Geeft (fit/twijfel-resultaten, sleutels van nieuwe woningen, aantal nieuw).
    Gooit een exceptie als iets misgaat; de mail wordt dan de volgende run opnieuw geprobeerd."""
    subject, sender = header(msg, "Subject"), header(msg, "From")
    text, links = email_to_text(msg)
    listings = extract_listings(client, subject, sender, text, links)
    log(f"  '{subject[:70]}': {len(listings)} woning(en) gevonden")

    results, new_keys, skipped = [], [], 0
    for l in listings:
        keys = [k for k in (listing_id_from_url(l.get("url") or ""), fallback_key(l)) if k]
        if not keys:
            keys = ["hash:" + hashlib.sha1(json.dumps(l, sort_keys=True).encode()).hexdigest()[:12]]
        if any(k in state["listings"] for k in keys):
            log(f"    = {l.get('address')} (al eerder verwerkt)")
            skipped += 1
            continue
        verdict = judge_listing(client, l)
        new_keys.extend(keys)
        log(f"    {verdict['verdict']:9} {l.get('address')} {fmt_price(l.get('price_eur'))}"
            f" {l.get('rooms')}k: {verdict['reason']}")
        if verdict["verdict"] in ("fit", "twijfel"):
            results.append({"listing": l, "verdict": verdict})
    return results, new_keys, len(listings) - skipped


def fetch_alert_messages(imap, state):
    """Alert-mails van de afgelopen dagen die nog niet verwerkt zijn: [(imap_id, message_id, msg)].
    Ook al-gelezen mails tellen mee (je kan de alert zelf al op je telefoon geopend hebben)."""
    imap.select("INBOX")
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    ids = set()
    for sender in SENDERS:
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


def run():
    missing = [k for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "ANTHROPIC_API_KEY", "DESTINATION_EMAIL")
               if not os.environ.get(k)]
    if missing:
        sys.exit(f"Ontbrekende omgevingsvariabelen: {', '.join(missing)}")

    state = load_state()
    now = datetime.now(timezone.utc).isoformat()
    client = anthropic.Anthropic()
    log(f"Model: {MODEL}")

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
    try:
        todo = fetch_alert_messages(imap, state)
        all_results, done, n_new, failed = [], [], 0, 0
        for imap_id, mid, msg in todo:
            try:
                results, keys, new = process_message(client, msg, state)
            except (anthropic.APIError, RuntimeError) as e:
                failed += 1
                log(f"  FOUT bij '{header(msg, 'Subject')[:60]}': {e} (volgende run opnieuw)")
                continue
            all_results.extend(results)
            n_new += new
            done.append((imap_id, mid))
            for k in keys:                      # dubbelingen binnen dezelfde run overslaan
                state["listings"][k] = now

        fits = sum(r["verdict"]["verdict"] == "fit" for r in all_results)
        log(f"Resultaat: {n_new} nieuwe woning(en) beoordeeld, {fits} fit, "
            f"{len(all_results) - fits} twijfel, {failed} mislukte mail(s)")

        # Eerst mailen, dan pas de state opslaan: als het mailen faalt, komt alles
        # de volgende run opnieuw.
        if all_results:
            send_email(*build_summary(all_results))
        else:
            log("Geen passende woningen, geen mail verstuurd")

        for imap_id, mid in done:
            state["mails"][mid] = now
        save_state(state)
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
    client = anthropic.Anthropic()
    results, _, _ = process_message(client, msg, {"listings": {}, "mails": {}})
    subject, html = build_summary(results) if results else ("(geen matches)", "")
    log(f"\nZou mailen: {subject}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--eml":
        test_eml(sys.argv[2])
    else:
        run()
