# urheiluopas-data

Urheiluoppaan ohjelmatiedot staattisina JSON-tiedostoina. GitHub Actions ajaa skriptin kerran tunnissa,
ja tiedostot julkaistaan GitHub Pagesiin, josta sovellus lukee ne. Palvelinta ei tarvita.

```
data/channels.yaml   kanavat ja palvelut maittain (Suomi, Ruotsi)
data/rights.yaml     lähetysoikeudet: kilpailu × maa → kanavat (päivitä kausittain)
data/manual.yaml     käsin syötetyt tapahtumat (yksilölajit, darts- ja snooker-sessiot) ja poikkeukset
scripts/build.py     hakee otteluohjelmat (TheSportsDB), yhdistää ja kirjoittaa JSONit
.github/workflows/   ajastettu ajo ja julkaisu GitHub Pagesiin
```

## Käyttöönotto

1. Luo GitHubiin julkinen repositorio `urheiluopas-data` ja lataa tämän kansion sisältö sinne.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. (Valinnainen) TheSportsDB:n oma API-avain: **Settings → Secrets and variables → Actions →
   New repository secret**, nimi `THESPORTSDB_KEY`. Ilman avainta käytetään rajoitettua testiavainta.
4. **Actions → Päivitä ohjelmatiedot → Run workflow.** Ensimmäisen ajon jälkeen tiedostot löytyvät osoitteesta
   `https://<käyttäjä>.github.io/urheiluopas-data/v1/FI/2026-11-22.json`.
5. Sovelluksen `app/build.gradle.kts`:
   ```kotlin
   buildConfigField("String", "DATA_SOURCE", "\"static\"")
   buildConfigField("String", "STATIC_BASE_URL", "\"https://<käyttäjä>.github.io/urheiluopas-data/\"")
   ```

## Ajaminen omalla koneella

```
pip install pyyaml
python scripts/build.py --out public              # hakee lähteestä + manual.yaml
python scripts/build.py --out public --no-fetch   # vain manual.yaml
python scripts/build.py --discover Snooker --date 2026-11-28   # lähteen sarjanimet rights.yaml:ia varten
```

## Ylläpito

- **Uusi laji tai sarja:** lisää kilpailu `rights.yaml`-tiedostoon (laji, kesto, nimet kolmella kielellä,
  kanavat maittain, lähde ja tarkistuspäivä). Jos otteluohjelma löytyy TheSportsDB:stä, lisää sarjan nimi
  kohtaan `thesportsdb.league` (tarkista `--discover`-komennolla). Sovellukseen lisätään lajin nimi
  (`sportLabel` ja `strings.xml`), jos laji on uusi.
- **Darts ja snooker:** turnaukset lähetetään sessioina, joten ne lisätään `manual.yaml`-tiedostoon
  sessio kerrallaan (aika, kesto ja nimi), kun turnauksen ohjelma julkaistaan.
- **Poikkeukset:** maksuttomat lisälähetykset (esim. MTV3, Kutonen) lisätään `overrides`-kohtaan.
- **Oikeudet vaihtuvat:** esim. Mestarien liiga siirtyy Suomessa Ruutuun syksyllä 2027 → päivitä `rights.yaml`.

Käsin syötetty tapahtuma korvaa lähteestä tulevan saman tapahtuman (sama kilpailu, päivä ja joukkueet).

## Tiedostomuoto

`v1/<MAA>/<päivä>.json` (päivä maan aikavyöhykkeessä):

```json
{
  "generatedAt": "2026-11-22T09:15:00Z", "country": "FI", "date": "2026-11-24",
  "matches": [{"id": "e…", "sportId": "football", "leagueId": "ucl", "homeTeam": "Arsenal",
               "awayTeam": "Borussia Dortmund", "titles": null, "startTimeUtc": "2026-11-24T20:00:00Z",
               "durationMin": 110, "note": null}],
  "broadcasts": [{"matchId": "e…", "channelId": "mtv", "countryCode": "FI", "startTimeUtc": "…", "isLive": true}],
  "names": [{"entityId": "ucl", "language": "fi", "name": "Mestarien liiga"}]
}
```
