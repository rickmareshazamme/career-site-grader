# Admin / Operations

## Super-admin cache bypass (`fresh=1`)

The grader caches per-domain **Core Web Vitals** and **backlink authority** so
repeat grades, monitoring and competitor lookups don't re-measure / re-charge.
Admins can force a completely fresh run with a token-protected override.

### Usage

Append `&fresh=1&token=<ADMIN_TOKEN>` to any grade endpoint:

```
# Streaming grade (UI / SSE)
/grade?url=https://example.com&mode=recruitment&fresh=1&token=<TOKEN>

# JSON grade (Client Portal / integrations)
/api/grade?url=https://example.com&mode=recruitment&fresh=1&token=<TOKEN>

# Force a fresh Core Web Vitals measurement
/api/cwv?url=https://example.com&mode=recruitment&fresh=1&token=<TOKEN>
```

What it bypasses:
- **Authority cache** — forces a fresh backlink-provider (DataForSEO/Moz) lookup
  instead of the 7-day cached value.
- **Core Web Vitals cache** — forces a fresh PageSpeed run and skips the cached
  fallback.

### Token

`fresh=1` is honoured only when `token` matches the `ADMIN_TOKEN` env var, or
`CRON_TOKEN` if `ADMIN_TOKEN` is unset. Without a valid token the `fresh` flag is
ignored, so public traffic can never bust the cache or drive up provider spend.

Set a dedicated admin token on the master (`web`) service:

```
railway variables --set "ADMIN_TOKEN=<random-secret>"
```

## Services

- **Master:** `web` service → https://webgrader.shazamme.com (volume `/data`,
  PageSpeed key, seeded benchmark). Deploy with:
  `railway link --project "Career Site Grader" --service web && railway up`
- **Scratch/test:** `career-site-grader` service. Use for risky changes via
  `railway up` (no GitHub push = master untouched).

## Relevant env vars (master)

| Var | Purpose |
|-----|---------|
| `DATA_DIR=/data` | SQLite persistence on the Railway volume |
| `PAGESPEED_API_KEY` | Google PageSpeed Insights (Core Web Vitals) |
| `CRON_TOKEN` | Auth for `/cron/rescore`, `/cron/seed`, and cache bypass |
| `ADMIN_TOKEN` | (optional) dedicated token for the cache bypass |
| `ENABLE_HEADLESS=1` | Playwright headless render (kill switch: `0`) |
| `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` | Backlink authority (DataForSEO) |
| `MOZ_TOKEN` | Backlink authority (Moz, alternative) |
| `SENDGRID_API_KEY` | Report / monitor-digest emails |
