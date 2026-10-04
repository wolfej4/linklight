# LAN Party Manager

A self-hosted organizer console for LAN parties. It covers attendees and door check-in, seating with per-circuit power budgeting, tournament brackets, and one-click dedicated game servers. It also has a big-screen display for the room and a phone page for each player.

It runs fully offline. htmx and fonts are bundled, so nothing loads from a CDN at the venue.

## Run it

```bash
# edit ADMIN_PASSWORD and PUBLIC_HOST in docker-compose.yml first
docker compose up -d --build
```

Open `http://<host>:1337` and sign in with `ADMIN_PASSWORD`. Everything lives in `./data/lanparty.db`, so to move between the homelab and a venue box, copy the `data` folder.

| Variable | Purpose |
|---|---|
| `PORT` | Web UI port. Defaults to `1337`. Set `PORT=` in a `.env` file to change it without editing the compose file. |
| `ADMIN_PASSWORD` | Organizer sign-in. |
| `PUBLIC_HOST` | LAN IP players type into games to reach hosted servers. |
| `BASE_URL` | Absolute URL for QR codes when behind a reverse proxy, e.g. `https://lan.example.com`. Leave blank on a flat LAN. |
| `DEFAULT_WATTS` | Power estimate for attendees who don't give one. |
| `SECRET_KEY` | Optional. Otherwise one is generated into `data/.secret_key`. |

### Homelab vs. portable

At home, put it behind your reverse proxy and set `BASE_URL`. Keep `/display`, `/t/*` and `/p/*` reachable without SSO, because players and the TV use them without signing in. Everything else sits behind the organizer password.

For a portable box at a venue, use the same compose file on a mini PC or laptop. Before you leave, launch each game server once at home so its image and game files are already downloaded. CS2 alone is about 60 GB.

## Modules

**Attendees and door.** Add people by hand or import a CSV (`name, handle, contact, gear, watts, paid`), filter by status, and export. `/attendees/badges` prints cut-out cards with a QR code. At the door, search by name on `/checkin`, or scan a badge: it opens the player page, and a signed-in organizer gets a Check in button there.

**Seating and power.** Add one circuit per breaker (amps and volts), then add tables in bulk. Each table is drawn as a switch panel: the left LED means the seat is taken, the right LED means the player has arrived. Loads are checked against 80% of the breaker rating, which is the continuous-load rule. Auto-seat fills open seats and skips any seat whose circuit can't take that person's rig.

**Tournaments.** Single elimination with standard seeding, with byes going to the top seeds, or round robin (3 points for a win, 1 for a draw). Players report scores from their phone page, and organizers can enter or undo any result. A result can't be undone while the next-round match already has one, so the bracket stays consistent. `/t/<id>` is a public, auto-refreshing bracket.

**Game servers.** Launches sibling containers through the mounted Docker socket. Built-in templates: Minecraft, Valheim, CS2, Factorio and Satisfactory. Add your own in `data/server_templates.json` (see `examples/`). A custom template overrides a built-in one with the same key. Set a different host port to run two copies of a game; multi-port games shift all their ports together. World data is kept in `lanparty_*` volumes when you remove a server.

**Big screen.** `/display` is meant for a TV in kiosk mode. It shows the announcement, up-next matches, running servers with connect addresses, and tournament winners. It refreshes every 10 seconds.

## Security notes

Mounting `docker.sock` gives this container root-equivalent control of the host. Only expose the admin side on networks you trust, or drop the socket mount if you don't need the game server module. Player pages are protected only by their unguessable link token.

## Dev

```bash
pip install -r requirements.txt
DATA_DIR=./data ADMIN_PASSWORD=dev uvicorn app.main:app --reload --port 1337
```
