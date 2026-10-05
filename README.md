# Rasa Open Source 3.6.20 ↔ WooCommerce bridge (Docker)

A Rasa chatbot that learns your WooCommerce store from its **sitemap.xml** and the
**WooCommerce REST API**, then answers customers about:

| Customers ask | Where the answer comes from |
|---|---|
| "Do you have hoodies?" / "looking for running shoes" | Product + category URLs from the sitemap, enriched with names, prices and stock from the API |
| "How much is it?" / "Is it in stock?" / "What sizes?" | Live product data from the API (price, sale price, stock, attributes) |
| "Is there free shipping?" / "How do I contact you?" / "Return policy?" | Text of the **pages and posts listed in the sitemap**, which the bridge crawls and searches |
| "Where is my order?" / "When will it arrive?" / "Can I return order 1234?" / "Where's my refund?" | Order API: status, items, customer notes, tracking numbers, delivery date, refunds and how long the return window has left |

Orders are only shown after the customer gives **the order number and the billing email
on that order**. The bot gives the same "not found" reply whether the order doesn't exist
or the email is wrong, and stops after 3 failed tries in one conversation.

```
 WooCommerce ── sitemap.xml ──┐
   (WordPress) ── REST API ───┤──► sync (every 6h) ──► bridge_cache/*.json ──► action-server ◄── rasa ◄── website chat
                 ── pages ────┘                    └─► lookups/lookups.yml ──► image build (CI)
```

## 1. Prerequisites
* Docker + Docker Compose v2 (`docker compose version`)
* A WooCommerce **REST API key**: *WooCommerce → Settings → Advanced → REST API → Add key*,
  permission **Read**. Copy the `ck_…` key and the `cs_…` secret.
* Your store on **HTTPS**, which WooCommerce needs for key/secret authentication.
* Pretty permalinks turned on (*Settings → Permalinks*, anything except "Plain") so `/wp-json/` works.

## 2. Configure (credentials stay OUT of the repository)
Set these as **environment variables** in your deployment platform, or put them in a
`.env` file next to `docker-compose.yml` on the server. `.env` is git-ignored, so never commit it.
`.env.example` lists every setting and only contains placeholders.

| Variable | Required | Example |
|---|---|---|
| `WC_URL` | yes | `https://your-store.com` |
| `WC_CONSUMER_KEY` / `WC_CONSUMER_SECRET` | yes | from *WooCommerce → Settings → Advanced → REST API* (Read) |
| `SITEMAP_URL` | no | only if auto-detection fails |
| `RETURN_WINDOW_DAYS`, `DELIVERY_DATE_META_KEYS`, `CRAWL_KINDS`, `SYNC_INTERVAL_SECONDS`, … | no | see `.env.example` |

The sitemap is found automatically (Yoast / Rank Math `sitemap_index.xml`, or WordPress core `wp-sitemap.xml`).

> If WordPress runs on the **same machine** as Docker, don't use `localhost` in `WC_URL`. Inside a container,
> `localhost` means the container itself. Use your domain, your LAN IP or `host.docker.internal`.

## 3. Deploy (prebuilt public images: no build step)
GitHub Actions (`.github/workflows/publish-images.yml`) builds and publishes two images on every push to `main`:

| Image | Contents |
|---|---|
| `ghcr.io/shopjiaexpress-dev/rasa-woocommerce-bridge-rasa:latest` | Rasa 3.6.20 + config + **trained model** |
| `ghcr.io/shopjiaexpress-dev/rasa-woocommerce-bridge-actions:latest` | action server + sync tool |

**One-time step after the first workflow run:** GitHub creates packages as *private*. For each of the two packages, open
*GitHub → Packages → the package → Package settings → Danger Zone → Change visibility → Public*.

On the server:
```bash
docker compose pull
docker compose up -d          # starts rasa, action-server and auto-sync (which syncs immediately)
docker compose ps             # wait until rasa + action-server show (healthy)
docker compose logs auto-sync # shows the sync summary
```
The sync summary looks like:
```json
{ "sitemap": "https://your-store.com/sitemap_index.xml", "urls": "page=12, post=30, product=240, product_cat=18",
  "products": 240, "categories": 18, "page_chunks": 310, "lookups_changed": true }
```
Prefer building on the server instead? `docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`

### Talk to it
```bash
docker compose run --rm rasa-shell        # chat in the terminal
# or REST:
curl -s localhost:5005/webhooks/rest/webhook -d '{"sender":"me","message":"do you have hoodies?"}'
```

### Connect Chatwoot (Agent Bot)
The `chatwoot-connector` service receives Chatwoot's bot webhooks, asks Rasa, and posts the replies back.
Conversations stay **pending** while the bot handles them. When the customer asks for a person, or the
bot can't help with an order, the conversation switches to **open** and your agents take over. The bot stays
silent until an agent sets the conversation back to *pending*.

1. **Chatwoot → Settings → Bots → Add bot.** Webhook URL: `https://<your-rasa-domain>/chatwoot/webhook`.
   Copy the bot's **access token** (and the **webhook secret**, if your Chatwoot version shows one).
2. **Chatwoot → Settings → Inboxes → your inbox → Bot configuration:** select the bot and save.
3. Set on the server: `CHATWOOT_URL`, `CHATWOOT_BOT_TOKEN`, `CHATWOOT_WEBHOOK_SECRET`.
   No secret shown in your version? Set `CHATWOOT_URL_TOKEN` to a long random string instead, and append
   `?token=<that string>` to the webhook URL.
4. Route `/chatwoot/` on your Rasa domain to the connector (`127.0.0.1:5056`). With Caddy:
   ```
   ai.your-domain.com {
       handle /chatwoot/* {
           reverse_proxy localhost:5056
       }
       reverse_proxy localhost:5005
   }
   ```
5. `docker compose up -d`, then check `docker compose ps`: `chatwoot-connector` should be *(healthy)*.

### Add the chat bubble to WooCommerce
1. Put the Rasa server (port 5005) behind HTTPS, e.g. `chat.your-store.com` → Nginx/Caddy → `localhost:5005`.
2. Paste `webchat/woocommerce-chat-widget.html` into your theme footer, or use the WPCode plugin's footer section.
   Change the URL in it to your chat domain.

## 4. Keeping it up to date
* **auto-sync** re-reads the sitemap, products and pages every `SYNC_INTERVAL_SECONDS` (default 6 h).
  The action server reloads the cache automatically. New prices, stock levels, pages and products
  are used straight away, with no restart or retraining.
* **Changing the bot** (`domain.yml`, `config.yml`, `data/*.yml`, `actions/`, `bridge/`): push to `main`.
  GitHub Actions retrains and republishes the images (~5–8 min). Then on the server:
  `docker compose pull && docker compose up -d`
* **Teaching the NLU your exact product names** (optional): run a sync locally
  (`python -m bridge.sync --no-crawl` with your env vars set), then commit the generated `lookups/lookups.yml`.
  It only contains public product/category names. Push, and the next image build trains with them.
  Without this the bot still finds products by fuzzy search.
* Order data is always fetched live. It is never cached.

## 5. Plugin-specific settings
| You use… | Set in `.env` |
|---|---|
| A delivery-date plugin (Order Delivery Date, Iconic WDS, …) | `DELIVERY_DATE_META_KEYS`: the order meta key your plugin saves. To find it, open an order with `GET /wp-json/wc/v3/orders/<id>` and look in `meta_data`. |
| WooCommerce Shipment Tracking / AfterShip | Nothing. The bridge reads `_wc_shipment_tracking_items` / `_aftership_tracking_items` and the Shipment Tracking REST endpoint. |
| Sequential Order Numbers | Nothing. The bridge searches by the visible order number if it doesn't match the ID. |
| A different return window | `RETURN_WINDOW_DAYS` |
| Pages that mostly aren't FAQs | `CRAWL_KINDS=page` (skip blog posts) and/or lower `CRAWL_MAX_PAGES` |
| Answers coming from the wrong page | Raise `PAGE_ANSWER_MIN_SCORE` (default 2.0), or add words to `QUERY_SYNONYMS` in `bridge/page_index.py` |

## 6. Project layout
```
bridge/            the bridge (plain Python, no Rasa dependency)
  sitemap.py         finds and reads sitemaps, sorts URLs into product / category / page / post
  woo_client.py      WooCommerce REST v3 client (products, orders, notes, refunds, tracking)
  catalog.py         product/category catalogue + fuzzy search
  page_index.py      crawls pages + BM25 search for FAQ-style answers
  sync.py            CLI: python -m bridge.sync [--no-api] [--no-crawl]
  chatwoot_connector.py  Chatwoot Agent Bot webhook <-> Rasa (+ human handoff)
  store.py           cache loader that reloads automatically for the action server
actions/actions.py Rasa custom actions (products, page answers, order form + verification)
domain.yml, config.yml, data/   Rasa 3.6 project
lookups/lookups.yml           product/category lookup tables (generated by sync)
docker-compose.yml           deploy from public images (no build)
docker-compose.build.yml     optional override to build locally
Dockerfile.rasa, Dockerfile.actions
.github/workflows/           test + build + publish images to GHCR
tests/             mock WooCommerce server, fake Chatwoot, 28 tests
```

## 7. Customising
* **Bot wording:** edit `responses:` in `domain.yml`.
* **Better intent recognition:** add real customer phrasing to `data/nlu.yml`, then push (CI retrains).
  Hinglish and Hindi examples work too.
* **New capabilities** (coupons, cart links, stock alerts, …): add a method to `bridge/woo_client.py`, an
  action in `actions/actions.py`, an intent and a rule, then push.
* **Running tests** (no Docker needed):
  `pip install -r requirements-actions.txt pytest && pytest -q tests/`

## 8. Troubleshooting
| Symptom | Cause / fix |
|---|---|
| Chatwoot bot never replies | `docker compose logs chatwoot-connector`. A `rejected webhook` warning means the secret/token doesn't match. No log lines at all means Chatwoot can't reach `/chatwoot/webhook` (check the proxy route). A `Chatwoot ... 401` error means the bot token is wrong. |
| Bot replies stop after a while | The conversation was moved to *open* (handoff). Set it back to *pending* to return it to the bot. |
| `docker compose pull` says `denied` / `unauthorized` for `ghcr.io/...` | The GHCR packages are still private. Make both public (see step 3). |
| GitHub Actions run fails at "login" / "push" with 403 | *Repo → Settings → Actions → General → Workflow permissions*: allow **Read and write**, then re-run. |
| `PermissionError: ... /app/actions/__init__.py` and containers restarting | You're on an old version. The current images fix their own permissions. Run `docker compose down`, update, then `docker compose pull && docker compose up -d`. |
| `rasa` shows **(unhealthy)**, and the bot replies with nothing (`[]`) | The image has no model loaded. Pull the latest image (`docker compose pull && docker compose up -d`) and check the GitHub Actions run succeeded. |
| `rasa` keeps restarting with exit code 137 | Out of memory. Rasa needs about 2 GB free. Add swap or use a 4 GB+ server. |
| Sync fails with `401 woocommerce_rest_cannot_view` | Wrong key/secret, or the key isn't **Read** permission. Some hosts strip the `Authorization` header; ask the host to allow it. |
| Sync can't reach the store | `WC_URL` must be reachable *from inside Docker*. Don't use `localhost` (see step 2). |

Useful commands: `docker compose ps`, `docker compose logs -f rasa action-server`,
`docker compose run --rm sync --no-crawl` (fast product refresh),
`docker compose down -v` (**deletes** chat history and cache volumes).

## 9. Security notes
* Use a **Read-only** API key. The bot never writes to your store.
* **Never commit real credentials.** `.env` is git- and docker-ignored, and `.env.example` must only contain placeholders.
  If a key is ever pushed, revoke it in WooCommerce right away and create a new one. Deleting the file
  doesn't remove it from git history.
* Rasa is published on `127.0.0.1:5005` only and the action server isn't published at all, so neither is
  reachable from the internet. Expose the bot only through your HTTPS reverse proxy.
* `--cors "*"` is convenient for testing. Set it to your store's domain in `docker-compose.yml` for production.
