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
                 ── pages ────┘                    └─► data/lookups.yml ──► rasa-train
```

## 1. Prerequisites
* Docker + Docker Compose v2 (`docker compose version`)
* A WooCommerce **REST API key**: *WooCommerce → Settings → Advanced → REST API → Add key*,
  permission **Read**. Copy the `ck_…` key and the `cs_…` secret.
* Your store on **HTTPS**, which WooCommerce needs for key/secret authentication.
* Pretty permalinks turned on (*Settings → Permalinks*, anything except "Plain") so `/wp-json/` works.

## 2. Configure
```bash
cp .env.example .env
nano .env        # set WC_URL, WC_CONSUMER_KEY, WC_CONSUMER_SECRET
# Linux only: put the output of `id -u` / `id -g` into HOST_UID / HOST_GID
```
The sitemap is found automatically (Yoast / Rank Math `sitemap_index.xml`, or WordPress core `wp-sitemap.xml`).
If yours is somewhere else, set `SITEMAP_URL`.

> If WordPress runs on the **same machine** as Docker, don't use `localhost` in `WC_URL`. Inside a container,
> `localhost` means the container itself. Use your domain, your LAN IP or `host.docker.internal`.

## 3. Build, sync, train, run
```bash
docker compose build                      # builds the action-server image
docker compose run --rm sync              # read sitemap + API + pages → bridge_cache/, data/lookups.yml
docker compose run --rm rasa-train        # trains models/*.tar.gz (takes ~2–5 min)
docker compose up -d                      # rasa :5005, action-server :5055, auto-sync
```
The sync prints a summary like:
```json
{ "sitemap": "https://your-store.com/sitemap_index.xml", "urls": "page=12, post=30, product=240, product_cat=18",
  "products": 240, "categories": 18, "page_chunks": 310, "lookups_changed": true }
```

### Talk to it
```bash
docker compose run --rm rasa-shell        # chat in the terminal
# or REST:
curl -s localhost:5005/webhooks/rest/webhook -d '{"sender":"me","message":"do you have hoodies?"}'
```

### Add the chat bubble to WooCommerce
1. Put the Rasa server (port 5005) behind HTTPS, e.g. `chat.your-store.com` → Nginx/Caddy → `localhost:5005`.
2. Paste `webchat/woocommerce-chat-widget.html` into your theme footer, or use the WPCode plugin's footer section.
   Change the URL in it to your chat domain.

## 4. Keeping it up to date
* **auto-sync** re-reads the sitemap, products and pages every `SYNC_INTERVAL_SECONDS` (default 6 h).
  The action server reloads the cache automatically. New prices, stock levels, pages and products
  are used straight away, with no restart.
* If you **add or rename many products**, retrain so the NLU recognises the new names
  (the sync log says when `lookups_changed` is true):
  ```bash
  docker compose run --rm rasa-train && docker compose restart rasa
  ```
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
  store.py           cache loader that reloads automatically for the action server
actions/actions.py Rasa custom actions (products, page answers, order form + verification)
domain.yml, config.yml, data/   Rasa 3.6 project (data/lookups.yml is generated by sync)
docker-compose.yml, Dockerfile.actions
tests/             mock WooCommerce server + 20 end-to-end tests
```

## 7. Customising
* **Bot wording:** edit `responses:` in `domain.yml`.
* **Better intent recognition:** add real customer phrasing to `data/nlu.yml`, then retrain.
  Hinglish and Hindi examples work too.
* **New capabilities** (coupons, cart links, stock alerts, …): add a method to `bridge/woo_client.py`, an
  action in `actions/actions.py`, an intent and a rule, then retrain.
* **Running tests** (no Docker needed):
  `pip install -r requirements-actions.txt pytest && pytest -q tests/`

## 8. Security notes
* Use a **Read-only** API key. The bot never writes to your store.
* Keep `.env` private. It is git- and docker-ignored.
* Port 5055 (action server) does not need to be public. Only expose 5005, and only through HTTPS.
  In production, remove the `ports:` entry from `action-server`.
* `--cors "*"` is convenient for testing. Set it to your store's domain in `docker-compose.yml` for production.
