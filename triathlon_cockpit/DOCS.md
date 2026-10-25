# Triathlon-Cockpit

Training (TrainingPeaks), Erholung und – ab den nächsten Versionen – Ernährung in einem
Cockpit in der Home-Assistant-Seitenleiste.

## Stand Version 0.1 (Etappe 1)

- Holt alle 30 Minuten aus TrainingPeaks: geplante und absolvierte Einheiten,
  Fitness/Ermüdung/Form (CTL/ATL/TSB) und die von Garmin übertragenen Gesundheitswerte
  (HRV, Schlaf, Ruhepuls, Body Battery, Stress).
- **Nur lesend** – es wird nichts in TrainingPeaks verändert.
- Oberfläche: „Triathlon“ in der HA-Seitenleiste (auch in der Companion-App).
- Daten liegen in einer eigenen Datenbank im Add-on und sind in den HA-Backups enthalten.

## Einrichtung

1. Im Browser bei [trainingpeaks.com](https://app.trainingpeaks.com) anmelden.
2. Entwicklertools öffnen (F12) → **Application** (Chrome/Edge) bzw. **Web-Speicher** (Firefox)
   → **Cookies** → `https://app.trainingpeaks.com`.
3. Den **Wert** des Cookies `Production_tpAuth` kopieren.
4. In HA: Add-on **Triathlon-Cockpit → Konfiguration** → Feld `tp_auth_cookie` → einfügen → Speichern.
5. Add-on starten bzw. neu starten. Nach ca. einer Minute ist der erste Abgleich durch.

Das Cookie gibt Zugriff auf dein TrainingPeaks-Konto. Es wird nur im Add-on gespeichert
und nur an TrainingPeaks gesendet.

## Wenn das Cookie abläuft

TrainingPeaks verwirft das Cookie nach längerer Inaktivität oder wenn du dich im Browser
abmeldest. Das Cockpit zeigt dann oben einen Hinweis. Einfach die Schritte 1–5 wiederholen.

## Optionen

| Option | Bedeutung | Standard |
| --- | --- | --- |
| `tp_auth_cookie` | Login-Cookie von TrainingPeaks | – |
| `sync_interval_min` | Abgleich alle … Minuten (15–240) | 30 |
| `history_days` | so viele Tage Vergangenheit beim ersten Abgleich | 120 |
| `plan_days_ahead` | so viele Tage geplante Einheiten | 21 |
| `log_level` | Protokoll-Detailgrad | info |

## Sicherheit

- Kein Host-Netzwerk, kein Zugriff auf die HA-Konfiguration, keine Hardware.
- Die Oberfläche ist nur über HA (Ingress) erreichbar; direkte Zugriffe werden abgewiesen.
- Stürzt das Add-on ab, läuft Home Assistant normal weiter; der Watchdog startet es neu.
