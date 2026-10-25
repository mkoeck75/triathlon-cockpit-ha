# Triathlon-Cockpit

Training (TrainingPeaks), Erholung und Ernährung in einem Cockpit in der
Home-Assistant-Seitenleiste – mit Schnittstelle für Claude (Coach und Koch).

## Was die App kann (Version 0.4)

- **Training:** holt alle 30 Minuten aus TrainingPeaks geplante und absolvierte Einheiten,
  Fitness/Ermüdung/Form (CTL/ATL/TSB) und die von Garmin übertragenen Gesundheitswerte
  (HRV, Schlaf, Ruhepuls, Body Battery, Stress).
- **Ernährung:** Tagesbilanz Soll/Ist mit Makros, Essen per Freitext (Schätzung über den
  HA-KI-Task, Vorschlag prüfen und korrigieren), Favoriten, Gewicht, Rezepte, Regeln, Quellen.
- **Trainingsänderungen:** Claude kann Einheiten verschieben, ändern oder neu anlegen –
  immer erst als **Vorschlag**. In TrainingPeaks geschrieben wird nur nach deinem OK
  (im Chat oder mit „Übernehmen“ auf der Heute-Seite). Absolvierte und vergangene Einheiten
  werden nie geändert, gelöscht wird nichts.
- **HA-Sensoren** für Automationen und Push-Meldungen (siehe unten).
- Daten liegen in einer eigenen Datenbank in der App und sind in den HA-Backups enthalten.

## Einrichtung

1. Im Browser bei [trainingpeaks.com](https://app.trainingpeaks.com) anmelden.
2. Entwicklertools öffnen (F12) → **Application** (Chrome/Edge) bzw. **Web-Speicher** (Firefox)
   → **Cookies** → `https://app.trainingpeaks.com`.
3. Den **Wert** des Cookies `Production_tpAuth` kopieren.
4. In HA: **Einstellungen → Apps → Triathlon-Cockpit → Konfiguration** → Feld `tp_auth_cookie`
   → einfügen → Speichern.
5. App starten bzw. neu starten. Nach ca. einer Minute ist der erste Abgleich durch.

Das Cookie gibt Zugriff auf dein TrainingPeaks-Konto. Es wird nur in der App gespeichert
und nur an TrainingPeaks gesendet.

## Wenn das Cookie abläuft

TrainingPeaks verwirft das Cookie nach längerer Inaktivität oder wenn du dich im Browser
abmeldest. Das Cockpit zeigt dann oben einen Hinweis und
`binary_sensor.triathlon_trainingpeaks_problem` geht auf „an“. Schritte 1–5 wiederholen.

## Sensoren

| Entität | Inhalt |
| --- | --- |
| `sensor.triathlon_fitness_ctl`, `_ermuedung_atl`, `_form_tsb` | TrainingPeaks-Fitness |
| `sensor.triathlon_hrv`, `_ruhepuls`, `_schlaf` | letzte Nacht, Attribut `schnitt_7_tage` |
| `sensor.triathlon_kcal_heute`, `_kcal_rest`, `_eiweiss_heute` | Ernährung heute vs. Soll |
| `sensor.triathlon_gewicht` | letzter Wert, Attribut `tage_seit_letzter_messung` |
| `sensor.triathlon_einheiten_heute`, `_naechste_einheit` | Trainingsplan |
| `sensor.triathlon_offene_trainingsaenderungen` | Anzahl offener Vorschläge |
| `binary_sensor.triathlon_trainingpeaks_problem` | an = Anmeldung abgelaufen/Fehler |
| `sensor.triathlon_letzter_abgleich` | Zeitpunkt des letzten TP-Abgleichs |

Die Werte werden alle 5 Minuten und nach jedem Abgleich gesetzt. Nach einem HA-Neustart
sind sie spätestens nach 5 Minuten wieder da (sie haben keine eindeutige ID und lassen sich
daher in der Oberfläche nicht umbenennen).

## Optionen

| Option | Bedeutung | Standard |
| --- | --- | --- |
| `tp_auth_cookie` | Login-Cookie von TrainingPeaks | – |
| `sync_interval_min` | Abgleich alle … Minuten (15–240) | 30 |
| `history_days` | so viele Tage Vergangenheit beim ersten Abgleich | 120 |
| `plan_days_ahead` | so viele Tage geplante Einheiten | 21 |
| `log_level` | Protokoll-Detailgrad | info |
| `ai_task_entity` | KI-Task für die Freitext-Schätzung | `ai_task.claude_ai_task` |

## Sicherheit

- Kein Host-Netzwerk, kein Zugriff auf die HA-Konfiguration, keine Hardware.
- Die Oberfläche ist nur über HA (Ingress) erreichbar; die API zusätzlich nur für die
  HA-MCP-App (Claude). Direkte Zugriffe werden abgewiesen.
- Stürzt die App ab, läuft Home Assistant normal weiter; der Watchdog startet sie neu.
