# weewx-php-ingest

Liest Wetterstationen mit WeeWX-Treibern aus und sendet Messwerte an weewx-php.

## Installieren

Für Debian 12+, Ubuntu 24.04+ und Raspberry Pi OS Bookworm+ mit Python 3.11+ und systemd.

```sh
curl -fsSL https://raw.githubusercontent.com/weewx-php/wwex-php-ingest/main/install.sh | sudo bash
```

Installiert den Collector, alle mitgelieferten WeeWX-Treiber und den Dienst.
Token und Collector-ID werden automatisch erzeugt.

Kein Download von Hand, kein GitHub-Konto nötig.

## Einrichten

Die geführte Einrichtung startet nach der Installation:

1. Zieldomain eingeben.
2. Angeschlossene USB-/Seriell-Geräte scannen und Hardware auswählen.
3. Verbindung konfigurieren und Messung testen.
4. Station im weewx-php-Webinterface mit **Adopt** übernehmen.

Kein Token kopieren, keine Registrierung. Der PHP-Empfänger benötigt aktiviertes
Ingest und [native Stationserkennung](integrations/weewx-php-adoption.patch).

Einrichtung erneut öffnen:

```sh
sudo weewx-php-ingest configure
```

Treiber, URL und Token stehen in `/etc/weewx-php-ingest/weewx.conf`.
[Beispielkonfiguration](examples/weewx.conf) · [Mehrere Stationen](examples/weewx-multi.conf)

## Aktualisieren

```sh
sudo weewx-php-ingest update
```

Aktualisiert Collector und mitgelieferte Treiber. Eine GitHub Action prüft täglich
auf neue stabile WeeWX-Versionen. Konfiguration und gepufferte Messwerte bleiben erhalten.

## Zusätzliche Treiber von GitHub

Ja, die mitgelieferte WeeWX-CLI unterstützt `weectl extension install` mit einer
GitHub-ZIP-/Tarball-URL. Das Paket muss eine WeeWX-Erweiterung mit `install.py` sein.

[Installation und Konfiguration Schritt für Schritt](docs/operations.md#treiber-von-github-installieren).
Zusätzliche Treiber werden in der `weewx.conf` ausgewählt; der Assistent zeigt nur
mitgelieferte Treiber. Drittanbieter-Treiber werden separat aktualisiert.

[Betrieb und Diagnose](docs/operations.md) · [WeeWX-Lizenz](THIRD_PARTY.md)
