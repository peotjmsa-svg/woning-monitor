# Huizenjacht-monitor

Twee bronnen die samen één lijst bijhouden, zodat je elke woning maar één keer krijgt:

**GitHub (altijd aan, elk kwartier)** — `check_alerts.py`
1. Leest de alert-mails van Pararius en Huurwoningen.nl (IMAP, afgelopen 7 dagen)
2. Haalt de woningen eruit: Huurwoningen-alerts met een vaste parser (gratis), andere
   mails via Claude
3. Gratis voorfilter op prijs, kamers en postcode binnen de ring (`config.py`); wat
   overblijft beoordeelt Claude op de gegevens uit de mail
4. Kandidaten gaan naar de wachtlijst (`queue.json`) voor controle door de pc thuis
5. Staat een kandidaat na `FALLBACK_MINUTES` (standaard 60) nog op de wachtlijst, dan
   mailt GitHub hem toch, gemarkeerd als **niet gecontroleerd**

**Pc thuis (Taakplanner, elk kwartier als hij aan staat)** — `home_run.py`
1. Haalt de wachtlijst van GitHub op en opent die advertenties (vanaf je thuisverbinding
   laat Cloudflare dat wel toe)
2. Zoekt zelf ook op de sites (de scraper uit `monitor.py`)
3. Leest de volledige advertentie: filtert op studenten, garantsteller, woningdelers,
   woningruil, max. 2 personen en inkomenseis, en laat Claude oordelen (incl. de
   "moet studeren in Amsterdam"-eis)
4. Mailt de woningen die passen, gemarkeerd als **gecontroleerd**
5. Pusht `home_seen.json` naar GitHub, zodat GitHub die woningen niet nog eens mailt

Dezelfde woning wordt overal herkend aan het listing-id of aan postcode + prijs.

## 1. Zoekalerts instellen

Maak op pararius.nl en huurwoningen.nl een account aan met het Gmail-adres dat de alerts
moet ontvangen, zoek op Amsterdam met je filters (bijv. max €3000, 4+ kamers) en klik
**Bewaar zoekopdracht / Alert instellen**. Kies de frequentie **direct** of **dagelijks**
(direct is sneller; de monitor checkt toch elke 2 uur).

## 2. Gmail app-wachtwoord maken

Voor het Gmail-account dat de alerts ontvangt:

1. Zet tweestapsverificatie aan: https://myaccount.google.com/signinoptions/twosv
2. Ga naar https://myaccount.google.com/apppasswords
3. Naam: `housing-monitor`, klik **Maken** en kopieer de 16 letters

IMAP staat in Gmail standaard aan. Werkt het inloggen niet, check dan Gmail →
Instellingen → Doorsturen en POP/IMAP → IMAP inschakelen.

## 3. Claude-abonnement koppelen (Pro/Max)

De monitor gebruikt je Claude-abonnement via Claude Code (`claude -p`), dus geen API-kosten.
Maak eenmalig een token aan op je eigen pc:

```
claude setup-token
```

Log in in de browser en kopieer het token dat verschijnt (begint met `sk-ant-oat`).

Elke beoordeling telt mee voor de gebruikslimiet van je abonnement, die je deelt met je
eigen gebruik van Claude. Per aanroep is dat klein (±1.500 tokens), maar bij heel veel
alert-mails per dag telt het op.

Liever per gebruik betalen via de API? Zet dan een `ANTHROPIC_API_KEY`-secret (van
https://console.anthropic.com/settings/keys). Als die er is, gebruikt de monitor die.

## 4. GitHub Secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
(direct: https://github.com/peotjmsa-svg/woning-monitor/settings/secrets/actions):

| Naam | Waarde |
|---|---|
| `GMAIL_ADDRESS` | het Gmail-adres dat de alerts ontvangt |
| `GMAIL_APP_PASSWORD` | het app-wachtwoord van stap 2 (voor datzelfde adres) |
| `CLAUDE_CODE_OAUTH_TOKEN` | het token van stap 3 |
| `DESTINATION_EMAIL` | waar de samenvatting heen moet; meerdere adressen komma-gescheiden |

## 5. De pc thuis

Vul `.env` in de hoofdmap in met dezelfde waarden als de secrets (`GMAIL_ADDRESS`,
`GMAIL_APP_PASSWORD`, `DESTINATION_EMAIL`). Claude gebruikt op de pc je eigen Claude
Code-login. Zolang `.env` leeg is slaat de taak elke run over. De taak in Taakplanner
heet `Woning-monitor` en draait `housing_monitor\home_run.py`; de log staat in
`monitor.log`. Proefrun zonder mailen of pushen:

```
.venv\Scripts\python housing_monitor\home_run.py --dry-run
```

Later op een Raspberry Pi: zelfde repo klonen, `.env` invullen, Claude Code installeren
en inloggen, en `home_run.py` elk kwartier via cron draaien.

## 6. Handmatig testen

Tab **Actions** → **Housing monitor (e-mailalerts)** → **Run workflow**. In de log van de
stap *Alert-mails verwerken* zie je hoeveel alert-mails er zijn gevonden, hoeveel woningen
er uit kwamen, het oordeel per woning en of er een mail is verstuurd. Daarna draait hij
vanzelf elk kwartier.

Mislukt een mail (bijv. API-fout), dan wordt die niet als verwerkt gemarkeerd, de run
krijgt een rood kruisje, en de volgende run probeert het opnieuw.

## Lokaal één mail testen

Sla een alert-mail op als `.eml` (Gmail: ⋮ → *Downloaden als bericht*) en draai:

```
pip install -r housing_monitor/requirements.txt
python housing_monitor/check_alerts.py --eml pad/naar/alert.eml
```

Dat gebruikt je eigen Claude Code-login op die pc en laat per woning het oordeel zien,
zonder IMAP, zonder mail en zonder iets op te slaan.

## Webapp: filters, sites en overzicht

https://peotjmsa-svg.github.io/woning-monitor/ (bron: `docs/index.html`)

- **Overzicht:** woningen per dag, afwijsredenen, gebieden, prijzen, per bron, en een
  doorzoekbare lijst. Komt uit `log_github.json` en `log_home.json`.
- **Filters:** budget, kamers, gebieden (postcodes, max €/kamer), afwijsregels, de criteria
  voor Claude en de timing.
- **Sites:** zoekpagina's voor de pc aan/uit zetten, aanpassen of een nieuwe site toevoegen
  (zoek-URL + een stukje tekst dat in elke advertentielink staat), en de afzenders van
  alert-mails.

Kijken kan zonder inloggen. Voor opslaan koppel je eenmalig een fine-grained GitHub-token
met alleen *Contents: Read and write* op deze repository (instructies staan in de app).
Opslaan schrijft `housing_monitor/settings.json`; beide monitors gebruiken het vanaf hun
volgende run. Is het bestand ongeldig, dan gelden de standaardwaarden uit `config.py` en
staat er een waarschuwing in de log.

## Aanpassen

- **Filters, sites, criteria:** via de webapp (of `housing_monitor/settings.json`)
- **Model:** standaard `claude-sonnet-4-5`; een ander model kan via een extra secret/variabele
  `CLAUDE_MODEL` (toevoegen aan `env:` in de workflow)
- **Frequentie:** de `cron` in `.github/workflows/housing-monitor.yml`
