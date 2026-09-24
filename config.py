"""Instellingen voor de woning-monitor. Pas hier je criteria aan."""

# Zoekpagina's die gecheckt worden. Alleen pagina 1 wordt gelezen, dus ze
# moeten op "Nieuwste eerst" staan (Pararius doet dat standaard, Huurwoningen
# alleen met &sort=published_at&direction=desc). De filters hieronder worden
# in de code nog een keer toegepast, dus de URL mag breder zijn dan je criteria.
SEARCH_URLS = [
    "https://www.pararius.nl/huurwoningen/amsterdam/0-3000/4-aantalkamers",
    "https://www.huurwoningen.nl/in/amsterdam/?price=0-3000&rooms=4&sort=published_at&direction=desc",
]

MAX_PRICE = 3000   # euro per maand
MIN_ROOMS = 4      # totaal aantal kamers, incl. woonkamer

# Gebieden binnen de ring (A10), op basis van de eerste 4 cijfers van de postcode.
# Dit is een benadering: aan de randen kan een postcode net buiten de ring vallen.
# Noord, Nieuw-West, Zuidoost en Buitenveldert staan er bewust niet in.
AREAS = {
    "Centrum": [(1011, 1018)],
    "West":    [(1051, 1059)],
    "Zuid":    [(1071, 1079)],
    "Oost":    [(1019, 1019), (1091, 1098)],
}

# Oost alleen als het een goede deal is: max prijs per kamer.
# 625 betekent: 4 kamers tot 2500, 5+ kamers tot het normale budget.
AREA_MAX_PRICE_PER_ROOM = {"Oost": 625}

# Stuur ook woningen door waarvan de postcode niet gevonden kon worden?
INCLUDE_UNKNOWN_POSTCODE = True

# Afwijzen op basis van de beschrijving
REJECT_NO_STUDENTS = True       # "niet verhuurd aan studenten", "no students"
REJECT_NO_GUARANTOR = True      # "garantstelling niet geaccepteerd"
REJECT_NO_SHARING = True        # "geen woningdelers", "not suitable for sharing"
REJECT_SWAP = True              # woningruil: alleen als je zelf een huurwoning hebt om te ruilen

# Afwijzen als de gevonden inkomenseis (bruto per jaar) hoger is dan dit.
# None = nooit afwijzen op inkomen, alleen de eis tonen in de mail
# (met een garantsteller kan een hoge eis vaak toch).
INCOME_REJECT_ABOVE = None

# Hoeveel detailpagina's per run maximaal ophalen (rest volgt de volgende run)
MAX_DETAIL_FETCHES_PER_RUN = 25

# Pauze tussen requests in seconden (min, max), om de sites niet te belasten
REQUEST_DELAY = (2.0, 4.0)
