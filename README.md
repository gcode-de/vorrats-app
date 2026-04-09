# Vorratsverwaltung

Selbst gehostete Vorratsverwaltung mit FIFO-Unterstützung, Barcode-Scanner und SQLite-Datenbank.

## Schnellstart

```bash
git clone <dein-repo>
cd vorrat
docker compose up -d
```

Danach erreichbar unter: **http://localhost:8080**

Im Homelab (z.B. Proxmox-VM): `http://<IP-der-VM>:8080`

---

## Barcode-Scanner

Der Scanner funktioniert **nur über HTTPS oder localhost** (Browser-Sicherheitsrichtlinie für Kamerazugriff).

Lokal (localhost:8080) funktioniert es direkt.

Für Zugriff aus dem Heimnetz (z.B. vom Handy) brauchst du HTTPS — am einfachsten per Reverse Proxy:

### Nginx Proxy Manager (empfohlen für Homelab)
1. Nginx Proxy Manager als Docker-Container laufen lassen
2. Subdomain anlegen z.B. `vorrat.home.dein-domain.de`
3. Zertifikat per Let's Encrypt ausstellen lassen
4. Proxy auf `http://vorrat:8000` zeigen lassen

### Traefik (Alternative)
Traefik-Labels in docker-compose.yml eintragen:
```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.vorrat.rule=Host(`vorrat.home.dein-domain.de`)"
  - "traefik.http.routers.vorrat.entrypoints=websecure"
  - "traefik.http.routers.vorrat.tls.certresolver=letsencrypt"
```

---

## Datensicherung

Die SQLite-Datenbank liegt im Docker Volume `vorrat-data`.

```bash
# Backup
docker run --rm -v vorrat-data:/data -v $(pwd):/backup alpine \
  tar czf /backup/vorrat-backup-$(date +%Y%m%d).tar.gz -C /data .

# Restore
docker run --rm -v vorrat-data:/data -v $(pwd):/backup alpine \
  tar xzf /backup/vorrat-backup-DATUM.tar.gz -C /data
```

---

## API-Übersicht

| Methode | Pfad | Beschreibung |
|---------|------|--------------|
| GET | /api/items | Alle Produkte |
| POST | /api/items | Produkt anlegen |
| PUT | /api/items/{id} | Produkt bearbeiten |
| DELETE | /api/items/{id} | Produkt löschen |
| POST | /api/items/{id}/qty | Menge ändern (delta) |
| GET | /api/items/{id}/history | Mengenhistorie |
| GET | /api/barcode/{barcode} | Barcode-Lookup (OpenFoodFacts) |
| GET | /docs | Swagger UI |

---

## Port ändern

In `docker-compose.yml` die Zeile `"8080:8000"` anpassen:
```yaml
ports:
  - "3000:8000"  # z.B. auf Port 3000
```
# vorrats-app
