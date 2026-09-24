"""Controleert de filters op zinnen uit echte advertenties. Draaien: python test_filters.py"""
import monitor as M

# (tekst, verwachte afwijzingen, verwachte waarschuwingen) - substrings van de meldingen
CASES = [
    ("niet geschikt voor studenten en/of garantstelling - inkomenseis: 3 keer de huur als netto inkomen",
     ["studenten", "garantsteller"], ["netto"]),
    ("derhalve kunnen wij geen garanties verstrekken, noch kunnen wij op enigerlei wijze",
     [], []),
    ("beschrijving niet geschikt om te delen in de stadionbuurt bieden wij",
     ["woningdelers"], []),
    ("woningdelen is toegestaan voor maximaal twee personen; studenten zijn welkom met garantstellers.",
     [], ["max. 2"]),
    ("roken is niet toegestaan - gratis parkeren - 2 woningdelers zijn toegestaan",
     [], ["max. 2"]),
    ("per direct beschikbaar - inkomenseis van toepassing - woningdelers (maximaal 2) is toegestaan",
     [], ["max. 2", "bedrag niet genoemd"]),
    ("huurder dient stabiel, vast en toereikend inkomen uit werk te hebben. • geen woningdelers •",
     ["woningdelers"], ["inkomen uit werk"]),
    ("geen studenten of garantstellers", ["studenten", "garantsteller"], []),
    ("geen delers/ studenten/ garantstelling prachtig gerenoveerd",
     ["woningdelers", "studenten", "garantsteller"], []),
    ("-woning wordt niet verhuurd aan studenten; -friends-contracten worden niet verstrekt; "
     "-garantstelling (door bijvoorbeeld ouders) worden niet geaccepteerd; "
     "-maximaal twee volwassen personen op de huurovereenkomst",
     ["studenten", "garantsteller"], ["friends", "max. 2"]),
    ("no guarantors accepted. no students.", ["garantsteller", "studenten"], []),
    ("belangrijk – dit is een woningruil. deze woning wordt aangeboden als ruilobject.", ["woningruil"], []),
    ("belangrijk! dit geldt uitsluitend voor een woningruil; je moet een huurwoning hebben", ["woningruil"], []),
    ("garantsteller niet nodig. geen garantstelling vereist", [], []),
]


def run():
    failed = 0
    for text, want_rej, want_warn in CASES:
        l = {"price": 2600, "rooms": 4, "postcode": "1054 MD", "description": text}
        _, reasons, warnings = M.evaluate(l)
        problems = [f"mist afwijzing '{w}'" for w in want_rej if not any(w in r for r in reasons)]
        problems += [f"mist waarschuwing '{w}'" for w in want_warn if not any(w in x for x in warnings)]
        if not want_rej and reasons:
            problems.append(f"onterecht afgewezen: {reasons}")
        if problems:
            failed += 1
            print(f"FOUT: {text[:70]!r}\n   {problems}\n   kreeg: {reasons} {warnings}")
    print(f"{len(CASES) - failed}/{len(CASES)} ok")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
