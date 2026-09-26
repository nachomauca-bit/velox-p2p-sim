# Sharing the demo with a link (Cloudflare Tunnel)

The quickest way to show the simulator to someone who is not in the room: run it on your own machine as usual and open a Cloudflare Tunnel to it. They get an `https://…trycloudflare.com` link; nothing is deployed, and the link stops working when you close the tunnel. For a permanent URL, use Cloud Run instead ([DEPLOY_GCP.md](DEPLOY_GCP.md)).

Cloudflare Pages and Workers are not an option: they host static sites and JavaScript functions, not a Python app with a SQLite file.

## Steps (Windows, PowerShell)

1. **Set a password.** The link is public, so put basic auth in front of the app. In `.env`:

   ```
   APP_USERNAME=velox
   APP_PASSWORD=choose-a-long-password
   ```

   Every page asks for these credentials (the browser remembers them for the session). Only `/health` stays open.

2. **Start the app** in one terminal, from the `velox-p2p-sim` folder:

   ```
   make run
   ```

   Without make: `.venv\Scripts\python -m uvicorn app.main:app --port 8010`.

3. **Install cloudflared** (once):

   ```
   winget install --id Cloudflare.cloudflared
   ```

   Open a new terminal afterwards so `cloudflared` is on the PATH.

4. **Open the tunnel** in a second terminal:

   ```
   cloudflared tunnel --url http://localhost:8010
   ```

   After a few seconds it prints a line with `https://<random-words>.trycloudflare.com`. Send that link and the password, by separate channels if you can. Quick tunnels need no Cloudflare account.

5. **Close it** with Ctrl+C in the tunnel terminal when the session ends. The link dies with it. Remove `APP_PASSWORD` from `.env` again if you prefer to work locally without the login prompt.

## Before you share

- **Reset the data**: press **Reset demo** in the header (or run `make seed`), so that the viewer starts from the demo's starting point. Everyone who opens the link works on the same database: a "Reset demo", a "Receive next email" or a "Run scenario" by one viewer changes what the others see.
- **Real model output without API calls**: the Gemini readings and owner-message drafts of the case documents are in `data/cache/`, so Reset demo, Run scenario and Run the control gate never call Gemini (they read the cache only). The API is called only by an owner-message draft that is not cached yet. Each call costs a fraction of a US cent (see [LIVE_VALIDATION.md](LIVE_VALIDATION.md)). To rule calls out completely, leave `GEMINI_API_KEY` blank in `.env` and restart `make run`. Everything cached still shows, labelled with the model that produced it.
- **Keep the terminal running**: the app and the tunnel run on your machine. If it sleeps or loses the network, the link stops working until both are up again.
- **Speed**: pages go through Cloudflare's network to your machine and back, which adds a little latency but is fine for a walkthrough. Follow the four-minute script in [DEMO.md](DEMO.md).

## Alternatives

| Option | When |
|---|---|
| Screen share (Teams, Meet, Zoom) of `http://127.0.0.1:8010` | A live presentation you drive yourself. Nothing is exposed. |
| Cloudflare quick tunnel (above) | Viewers should click through the app themselves, for an hour or a day. |
| Cloud Run ([DEPLOY_GCP.md](DEPLOY_GCP.md), phase 3) | A link that works without your machine. Needs a Google Cloud project with billing. |
