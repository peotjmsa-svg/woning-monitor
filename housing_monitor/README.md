# Huizenjacht-monitor via e-mailalerts

Draait elke 2 uur op GitHub Actions. In plaats van Pararius en Huurwoningen.nl zelf te
scrapen (Cloudflare blokkeert dat vanaf GitHub) leest hij de gratis alert-mails die die
sites sturen:

1. Leest via IMAP de alert-mails van Pararius en Huurwoningen.nl van de afgelopen 7 dagen
2. Claude haalt de losse woningen uit elke mail (adres, prijs, kamers, m², link, inkomenseis)
3. Nieuwe woningen worden door Claude beoordeeld op de criteria in `check_alerts.py`
   (`CRITERIA`): **fit**, **twijfel** of **geen fit**, met een korte onderbouwing
4. Alle fit- en twijfel-woningen gaan in één mail naar `DESTINATION_EMAIL`
5. Verwerkte mails worden als gelezen gemarkeerd; `seen_listings.json` onthoudt welke
   woningen en mails al gedaan zijn en wordt door de workflow terug gecommit

Een mail telt als verwerkt op basis van zijn Message-ID, dus ook een alert die je zelf al
op je telefoon hebt geopend wordt meegenomen. Dezelfde woning op beide sites wordt
herkend aan het listing-id of aan adres + prijs.

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

## 3. Anthropic API-key

Maak een key aan op https://console.anthropic.com/settings/keys en zet er wat tegoed op
(Billing). Kosten: een paar cent per alert-mail; met een handvol alerts per dag ruim
onder de €5 per maand.

## 4. GitHub Secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
(direct: https://github.com/peotjmsa-svg/woning-monitor/settings/secrets/actions):

| Naam | Waarde |
|---|---|
| `GMAIL_ADDRESS` | het Gmail-adres dat de alerts ontvangt |
| `GMAIL_APP_PASSWORD` | het app-wachtwoord van stap 2 (voor datzelfde adres) |
| `ANTHROPIC_API_KEY` | de key van stap 3 |
| `DESTINATION_EMAIL` | waar de samenvatting heen moet; meerdere adressen komma-gescheiden |

## 5. Handmatig testen

Tab **Actions** → **Housing monitor (e-mailalerts)** → **Run workflow**. In de log van de
stap *Alert-mails verwerken* zie je hoeveel alert-mails er zijn gevonden, hoeveel woningen
er uit kwamen, het oordeel per woning en of er een mail is verstuurd. Daarna draait hij
vanzelf elke 2 uur.

Mislukt een mail (bijv. API-fout), dan wordt die niet als verwerkt gemarkeerd, de run
krijgt een rood kruisje, en de volgende run probeert het opnieuw.

## Lokaal één mail testen

Sla een alert-mail op als `.eml` (Gmail: ⋮ → *Downloaden als bericht*) en draai:

```
pip install -r housing_monitor/requirements.txt
set ANTHROPIC_API_KEY=...        (PowerShell: $env:ANTHROPIC_API_KEY="...")
python housing_monitor/check_alerts.py --eml pad/naar/alert.eml
```

Dat laat per woning het oordeel zien, zonder IMAP, zonder mail en zonder iets op te slaan.

## Aanpassen

- **Criteria:** `CRITERIA` en `JUDGE_SYSTEM` in `check_alerts.py`
- **Model:** standaard `claude-sonnet-4-5`; een ander model kan via een extra secret/variabele
  `CLAUDE_MODEL` (toevoegen aan `env:` in de workflow)
- **Frequentie:** de `cron` in `.github/workflows/housing-monitor.yml`
