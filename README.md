# LAN Party Manager

A self-hosted website and organizer console for LAN parties. The public site lists upcoming and past events, sells tickets through Stripe or PayPal, and lets people sign in with Steam, Discord, Google, Apple or an emailed link. Behind it, organizers get attendees and door check-in, seating with per-circuit power budgeting, solo and team tournament brackets, a schedule, news posts, and one-click dedicated game servers. There's also a big-screen display for the room and a phone page for each player.

The organizer side runs fully offline. htmx and fonts are bundled, so nothing loads from a CDN at the venue. Online sign-in and payments need internet access, so sell tickets from the homelab before the event.

## Run it

```bash
cp .env.example .env      # then fill in ADMIN_PASSWORD, PUBLIC_HOST and DATA_PATH
docker compose up -d --build
```

All settings live in `.env`. `docker-compose.yml` only reads from it.

### Dockhand

1. Build the image once on the Docker host, from this folder: `docker build -t lanparty-manager:latest .` A stack created in Dockhand's UI doesn't have the source code next to it, so it can't build the image itself.
2. In Dockhand, create a stack, paste in `docker-compose.yml`, and delete the `build: .` line.
3. Paste the contents of `.env.example` into the stack's environment variables and fill them in. Use an absolute `DATA_PATH`, because Dockhand runs stacks from its own data folder.
4. Deploy. To update later, rebuild the image with the same tag and redeploy the stack.

Open `http://<host>:1337` for the public site. The organizer console is at `/admin`: sign in with `ADMIN_PASSWORD`, or put your email in `ADMIN_EMAILS` and sign in with your account. Everything lives in `./data/lanparty.db`, so to move between the homelab and a venue box, copy the `data` folder. Older databases are upgraded in place on startup.

New events start as drafts. Fill in the dates and description on the overview, add ticket types under Tickets, then tick "Show this event on the public site".

| Variable | Purpose |
|---|---|
| `DATA_PATH` | Host folder for the database and custom templates. Use an absolute path. |
| `PORT` | Web UI port. Defaults to `1337`. Set `PORT=` in a `.env` file to change it without editing the compose file. |
| `ADMIN_PASSWORD` | Organizer sign-in. Keeps working after you set up accounts. |
| `ADMIN_EMAILS` | Comma-separated emails that become organizers when they sign in. |
| `SITE_NAME` | Name in the public site's header. |
| `PUBLIC_HOST` | LAN IP players type into games to reach hosted servers. |
| `BASE_URL` | Absolute public URL, e.g. `https://lan.example.com`. Used in QR codes, emails, and the return addresses for sign-in providers and payments. |
| `DEFAULT_WATTS` | Power estimate for attendees who don't give one. |
| `SECRET_KEY` | Optional. Otherwise one is generated into `data/.secret_key`. |

Sign-in, email and payment settings are listed with setup links in `.env.example`. Each sign-in option appears only once its keys are set, except Steam, which needs none.

### Sign-in

Register `<BASE_URL>/auth/<provider>/callback` as the redirect URL with each provider (`discord`, `google`, `apple`). Steam needs no registration. Apple requires HTTPS and a paid Apple Developer account. Email sign-in needs the `SMTP_*` settings: people get a one-time link that expires in 15 minutes.

Signing in with a new provider whose verified email matches an existing account joins that account. Otherwise people can link more providers from their account page.

### Payments

- **Stripe:** set `STRIPE_SECRET_KEY`, add a webhook endpoint at `<BASE_URL>/webhooks/stripe` for `checkout.session.completed` and `checkout.session.async_payment_succeeded`, and put its signing secret in `STRIPE_WEBHOOK_SECRET`. Tickets are also confirmed when the buyer lands back on the site, so a slow webhook doesn't hold anyone up.
- **PayPal:** set `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`. Leave `PAYPAL_MODE=sandbox` until you've tested a purchase, then set it to `live`. Payments are captured when the buyer returns from PayPal.

Use test keys first. Checkouts hold their tickets for 30 minutes. Refunds go back through the same provider from the organizer's Tickets page.
### Homelab vs. portable

At home, put it behind your reverse proxy and set `BASE_URL`. Don't put SSO in front of the public pages (`/`, `/events*`, `/news*`, `/login*`, `/auth/*`, `/account`, `/orders/*`, `/tickets/*`, `/claim/*`, `/webhooks/*`, `/display`, `/t/*`, `/p/*`), since players, payment providers and the TV use them. The organizer pages check for an organizer account or the password themselves.

For a portable box at a venue, use the same compose file on a mini PC or laptop. Before you leave, launch each game server once at home so its image and game files are already downloaded. CS2 alone is about 60 GB.

## Modules

**Public site.** The homepage features the next event and lists upcoming events, past events and news. Each event page shows the description, schedule, tournaments, who's going (by gamertag) and the ticket form. Signed-in players manage their profile, linked sign-ins, tickets and orders at `/account`.

**Tickets.** Organizers add ticket types with a price, a stock limit and a per-order cap. Buyers pay through Stripe or PayPal, and free tickets are issued straight away. Each paid ticket held by someone becomes an attendee, so it flows into the door, seating and tournaments. People can buy extra tickets for friends: every spare comes with a claim link to copy or email, a buyer can take a ticket back before it's used, and each person holds one ticket per event.

**Schedule and news.** Add schedule items per event. They show on the event page, and the next few show on the big screen. News posts go on the homepage and, when linked to an event, on its page.

**Attendees and door.** Add people by hand or import a CSV (`name, handle, contact, gear, watts, paid`), filter by status, and export. `/attendees/badges` prints cut-out cards with a QR code. At the door, search by name on `/checkin`, or scan a badge: it opens the player page, and a signed-in organizer gets a Check in button there.

**Seating and power.** Add one circuit per breaker (amps and volts), then add tables in bulk. Each table is drawn as a switch panel: the left LED means the seat is taken, the right LED means the player has arrived. Loads are checked against 80% of the breaker rating, which is the continuous-load rule. Auto-seat fills open seats and skips any seat whose circuit can't take that person's rig.

**Tournaments.** Single elimination with standard seeding, with byes going to the top seeds, or round robin (3 points for a win, 1 for a draw). Set players per team above 1 for team tournaments. With online sign-ups open, ticket holders enter themselves on `/t/<id>`, or create a team and share its join code. Players report scores from their phone page, any team member can report for their team, and organizers can enter or undo any result. A result can't be undone while the next-round match already has one, so the bracket stays consistent. `/t/<id>` is a public, auto-refreshing bracket.

**Game servers.** Launches sibling containers through the mounted Docker socket. Built-in templates: Minecraft, Valheim, CS2, Factorio and Satisfactory. Add your own in `data/server_templates.json` (see `examples/`). A custom template overrides a built-in one with the same key. Set a different host port to run two copies of a game; multi-port games shift all their ports together. World data is kept in `lanparty_*` volumes when you remove a server.

**Big screen.** `/display` is meant for a TV in kiosk mode. It shows the announcement, up-next matches, running servers with connect addresses, and tournament winners. It refreshes every 10 seconds.

## Security notes

Mounting `docker.sock` gives this container root-equivalent control of the host, and any organizer account can launch containers. Only make people organizers if you trust them with the host, or drop the socket mount if you don't need the game server module. Player pages and ticket claim links are protected only by their unguessable tokens.

Card and PayPal details never reach this server. Tickets are issued only after the provider confirms the full amount for that order, and Stripe webhooks are checked against their signing secret.

## Dev

```bash
pip install -r requirements.txt
DATA_DIR=./data ADMIN_PASSWORD=dev EMAIL_TO_LOG=1 uvicorn app.main:app --reload --port 1337
pip install pytest && python -m pytest
```

With `EMAIL_TO_LOG=1`, sign-in links are printed to the console instead of emailed.
