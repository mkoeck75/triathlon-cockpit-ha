# Änderungen

## 0.2.0 – 2026-10-25

- Etappe 2: Schnittstelle für Claude unter /api/v1 (Tagesüberblick, Einheiten, Details,
  Wochen, Erholung, Fitness; Mahlzeiten, Gewicht und Tagesziele eintragen).
- Zugriff nur über die HA-Oberfläche und die HA-MCP-App (Claude-Connector), ohne extra Passwort.

## 0.1.2 – 2026-10-25

- Body Battery und Stress werden als Liste geliefert – jetzt korrekt ausgelesen.

## 0.1.1 – 2026-10-25

- Schlafdauer und andere Gesundheitswerte robuster auslesen (TP-Typnummer + Einheit),
  gespeicherte Rohdaten werden beim Start neu ausgewertet; Feldübersicht im Protokoll.

## 0.1.0 – 2026-10-25

- Erste Version (Etappe 1): TrainingPeaks-Abgleich nur lesend (Einheiten, CTL/ATL/TSB,
  Garmin-Gesundheitswerte), eigene Datenbank inkl. Gerüst für Ernährung, Oberfläche
  in der HA-Seitenleiste.
