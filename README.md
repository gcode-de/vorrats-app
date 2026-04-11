# Vorrats-App

Eine einfache Web-App zur Verwaltung von Vorräten mit Barcode-Scanner, entwickelt mit FastAPI (Backend) und Vanilla JavaScript (Frontend). Vollständig lokal laufend, keine Cloud-Abhängigkeiten.

## Features

- ✅ Produkte hinzufügen/bearbeiten/löschen
- ✅ Mengen verwalten (Einheiten, Kategorien)
- ✅ Mindesthaltbarkeitsdatum (MHD) mit Warnungen
- ✅ Barcode-Scanner (ZXing)
- ✅ Responsive Design (Dark Mode Support)
- ✅ Suche und Filter
- ✅ FIFO-Hinweise für ablaufende Produkte
- ✅ SQLite-Datenbank
- ✅ Preis pro Stück und Geschäft (Store) tracking
- ✅ PWA-Unterstützung (Offline-fähig, installierbar)
- ✅ FontAwesome-Icons für Kategorien
- ✅ Benutzerdefinierte Kategorien
- ✅ Deutsche Lokalisierung (Preise in EUR, deutsche Texte)

## Lokale Entwicklung

### Voraussetzungen

- Python 3.8+
- Git

### Setup

1. **Repository klonen:**

   ```
   git clone https://github.com/dein-username/vorrats-app.git
   cd vorrats-app
   ```

2. **Virtuelle Umgebung erstellen und aktivieren:**

   ```
   python3 -m venv venv
   source venv/bin/activate  # macOS/Linux
   # venv\Scripts\activate   # Windows
   ```

3. **Abhängigkeiten installieren:**

   ```
   pip install -r requirements.txt
   ```

4. **Server starten:**

   ```
   python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```

   **Hinweis:** Die App initialisiert automatisch die SQLite-Datenbank mit den neuen Feldern für Preis und Geschäft. Bestehende Datenbanken werden migriert.

5. **App öffnen:**
   Öffne `http://localhost:8000` im Browser.

### Barcode-Scanner

- Erfordert HTTPS oder localhost für Kamera-Zugriff.
- Lokal funktioniert es, im Homelab über HTTP nicht – verwende HTTPS (siehe Deployment).

### PWA-Features

- **Offline-Unterstützung:** Service Worker cached statische Assets
- **Installierbar:** Manifest für App-Installation auf Mobilgeräten
- **Responsive:** Funktioniert auf Desktop und Mobile
- **Icons:** FontAwesome-Icons für Kategorien, benutzerdefinierte Kategorien möglich

## Deployment mit Podman

### Voraussetzungen

- Podman installiert
- Docker Hub Account (für Images)

### Schritte

1. **GitHub Actions einrichten:**
   - Repository auf GitHub pushen.
   - Secrets setzen: `DOCKERHUB_USERNAME` und `DOCKERHUB_TOKEN`.
   - Pipeline baut automatisch Images und pusht zu Docker Hub.

2. **Pod erstellen:**

   ```
   podman pod create --name vorrats-pod -p 8082:8000
   ```

3. **Named Volume für Datenbank:**

   ```
   podman volume create vorrat-data
   ```

4. **Container starten:**

   ```
   podman run -d --pod vorrats-pod --name vorrats-app \
     -v vorrat-data:/data \
     dein-username/vorrats-app:latest
   ```

5. **App öffnen:**
   `http://deine-server-ip:8082`

### Updates

```
podman stop vorrats-app
podman rm vorrats-app
podman pull dein-username/vorrats-app:latest
podman run -d --pod vorrats-pod --name vorrats-app -v vorrat-data:/data dein-username/vorrats-app:latest
```

## HTTPS mit Traefik (Reverse Proxy)

Für sicheren Zugriff und Barcode-Scanner im Homelab:

1. **Proxy-Pod erstellen:**

   ```
   podman pod create --name proxy-pod -p 80:80 -p 443:443
   ```

2. **traefik.yml auf Server kopieren** (aus Repo).

3. **Traefik starten:**

   ```
   podman run -d --pod proxy-pod --name traefik \
     -v /var/run/docker.sock:/var/run/docker.sock \
     -v ./traefik.yml:/etc/traefik/traefik.yml \
     traefik:v2.10
   ```

4. **App im Proxy-Pod starten:**

   ```
   podman run -d --pod proxy-pod --name vorrats-app \
     --label "traefik.enable=true" \
     --label "traefik.http.routers.vorrat.rule=Host(\`vorrat.deine-domain.com\`)" \
     --label "traefik.http.routers.vorrat.tls.certresolver=letsencrypt" \
     dein-username/vorrats-app:latest
   ```

5. **DNS einrichten:** Domain auf Server-IP zeigen.

6. **App öffnen:** `https://vorrat.deine-domain.com`

## API-Endpunkte

- `GET /api/items` – Alle Produkte
- `POST /api/items` – Produkt hinzufügen
- `PUT /api/items/{id}` – Produkt bearbeiten
- `DELETE /api/items/{id}` – Produkt löschen
- `POST /api/items/{id}/qty` – Menge ändern
- `GET /api/stores` – Alle gespeicherten Geschäfte
- `GET /api/barcode/{barcode}` – Barcode nachschlagen (OpenFoodFacts)

## Struktur

```
vorrats-app/
├── main.py              # FastAPI Backend mit CORS
├── static/
│   ├── index.html       # Frontend (PWA, deutsche Lokalisierung)
│   ├── manifest.json    # PWA-Manifest
│   └── sw.js            # Service Worker für Offline
├── requirements.txt     # Python-Abhängigkeiten
├── Dockerfile           # Container-Build
├── docker-compose.prod.yml  # Prod-Setup
├── traefik.yml          # Reverse Proxy Config
└── .github/workflows/   # CI/CD Pipeline
```

## Lizenz

MIT License
docker run --rm -v vorrat-data:/data -v $(pwd):/backup alpine \
  tar czf /backup/vorrat-backup-$(date +%Y%m%d).tar.gz -C /data .

# Restore

docker run --rm -v vorrat-data:/data -v $(pwd):/backup alpine \
 tar xzf /backup/vorrat-backup-DATUM.tar.gz -C /data

````

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
````

# vorrats-app
