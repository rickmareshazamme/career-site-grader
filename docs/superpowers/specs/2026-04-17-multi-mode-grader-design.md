# Multi-Mode Website Grader — Design Spec

**Date:** 2026-04-17
**Project:** career-site-grader
**Approach:** Single grader class with mode parameter (Approach A)

---

## Overview

Transform the existing recruitment-only website grader into a multi-mode grader supporting three site types. Users select the mode upfront via the landing page before entering a URL.

## Modes

| Mode ID | Label | Subtitle | Target |
|---------|-------|----------|--------|
| `recruitment` | Recruitment Website | Staffing agencies & job boards | Current grader, unchanged |
| `career_site` | Career Site | Corporate careers pages & employer brand | Corporate /careers pages |
| `general` | Website | Any website — full quality audit | Any website type |

## Architecture

### Grader Class

`CareerSiteGrader` accepts an optional `mode` parameter (default: `recruitment` for backwards compatibility). Each pillar method branches on `self.mode` to apply mode-specific criteria, weights, and labels. Shared checks (HTTPS, TTFB, compression, basic SEO) remain common across modes.

### Server

- **Endpoint:** `GET /grade?url=<URL>&mode=<mode>`
- `mode` is required. Server validates against `recruitment|career_site|general`. Returns 400 if invalid or missing.
- SSE stream format is unchanged. Pillar names and labels adapt per mode.
- `/health` — unchanged.

### Frontend

- Landing page gets a 3-option card selector above the URL input.
- User must select a mode before the "Grade" button is enabled.
- Mode passes as query param to the grade endpoint.
- Results page shows active mode as a badge next to the domain.
- URL sharing includes mode: `?url=...&mode=career_site`

---

## Pillar Definitions by Mode

### Mode: `recruitment` (unchanged)

Current 6 pillars with current weights. No modifications.

| Pillar | Weight |
|--------|--------|
| SEO & Discoverability | 22% |
| GEO & AI Visibility | 20% |
| Candidate Experience | 22% |
| Employer Brand & Content | 14% |
| Technical Performance | 13% |
| Conversion & Engagement | 9% |

All existing checks, scoring, summaries, and recommendations remain as-is.

---

### Mode: `career_site`

Tailored for corporate career pages. Inspired by Happy Dance Job Page Grader (jobpagegrader.com).

| Pillar | Weight | Key Difference from Recruitment |
|--------|--------|---------------------------------|
| SEO & Discoverability | 18% | Drop StaffingAgency schema, sector pages. Add JobPosting depth, careers indexability |
| GEO & AI Visibility | 15% | Same core checks, lower weight |
| Candidate Experience | 25% | ATS detection, mobile apply, application form quality, accessibility |
| Employer Brand & Content | 25% | Culture pages, employee stories, benefits depth, DE&I page, EVP, video |
| Technical Performance | 10% | Same checks, lower weight |
| Conversion & Engagement | 7% | Job alerts, chatbot, social sharing — lighter weight |

#### SEO & Discoverability (18%, 100pts)

| Check | Points | Details |
|-------|--------|---------|
| Title Tag | 15 | Length 30-60 chars, contains career/jobs keywords |
| Meta Description | 12 | Length 120-160 chars |
| H1 Heading | 10 | Exactly 1 H1 |
| Schema / Structured Data | 25 | JobPosting schema (10pts), Organization (7pts), BreadcrumbList (3pts), FAQPage (3pts), any schema (2pts) |
| Open Graph Tags | 13 | og:title, og:description, og:image, og:type |
| Canonical URL | 8 | Present |
| Indexability | 7 | No NOINDEX |
| Heading Hierarchy | 10 | H2/H3 presence and count |

#### GEO & AI Visibility (15%, 100pts)

Same checks as recruitment mode. No changes to criteria, only weight reduced to 15%.

#### Candidate Experience (25%, 100pts)

| Check | Points | Details |
|-------|--------|---------|
| Mobile Readiness | 12 | Correct viewport meta |
| Accessibility / WCAG | 15 | lang attr (5pts), image alt text coverage (10pts) |
| Apply Flow | 20 | Apply buttons detected (10pts), application form present (5pts), low field count bonus (5pts — fewer than 6 visible fields) |
| ATS Platform Detection | 15 | Detects: Greenhouse, Lever, Workday, Taleo, iCIMS, SmartRecruiters, BambooHR, Jobvite, Ashby, Breezy. Score: detected and functional (15pts), detected (10pts), none (0pts) |
| Navigation & Structure | 10 | Semantic nav (5pts), breadcrumbs (5pts) |
| HTTPS | 8 | HTTPS present |
| Page Speed / TTFB | 10 | <0.5s (10pts), <1s (8pts), <2s (5pts), <4s (2pts) |
| Form Usability | 10 | Forms detected (5pts), autocomplete attrs (5pts) |

#### Employer Brand & Content (25%, 100pts)

| Check | Points | Details |
|-------|--------|---------|
| Culture & Team Content | 20 | Culture page detected (10pts), team/people page (5pts), "life at" content (5pts) |
| Employee Stories / Testimonials | 15 | Testimonial/story signals (10pts), video testimonials (5pts bonus) |
| EVP & Pay Transparency | 20 | Salary/pay signals (8pts), benefits/perks page (7pts), career growth/progression content (5pts) |
| DE&I Commitment | 12 | Dedicated DE&I page (8pts), DE&I keywords in content (4pts) |
| Video Content | 10 | Video embeds or native video present |
| Visual Brand | 8 | og:image (5pts), favicon (3pts) |
| Social Presence | 15 | 3pts per platform (LinkedIn, Glassdoor, Indeed, Twitter/X, Facebook, Instagram, YouTube, TikTok) max 15pts |

#### Technical Performance (10%, 100pts)

Same checks as recruitment mode. No changes to criteria, only weight reduced to 10%.

#### Conversion & Engagement (7%, 100pts)

Same checks as recruitment mode with adjusted CTA keywords:
- Add: "explore roles", "see open positions", "join our team", "work with us", "view opportunities"
- Weight reduced to 7%.

---

### Mode: `general`

Universal website quality audit. Replaces recruitment-specific pillars with generic equivalents.

| Pillar | Weight |
|--------|--------|
| SEO & Discoverability | 22% |
| Security & Trust | 18% |
| User Experience | 20% |
| Content Quality | 15% |
| Technical Performance | 15% |
| Conversion & Engagement | 10% |

#### SEO & Discoverability (22%, 100pts)

| Check | Points | Details |
|-------|--------|---------|
| Title Tag | 15 | Length 30-60 chars |
| Meta Description | 12 | Length 120-160 chars |
| H1 Heading | 10 | Exactly 1 H1 |
| Schema / Structured Data | 18 | Any schema (5pts), Organization (5pts), BreadcrumbList (3pts), LocalBusiness (3pts), FAQPage (2pts) |
| Open Graph Tags | 13 | og:title, og:description, og:image, og:type |
| Canonical URL | 8 | Present |
| Indexability | 7 | No NOINDEX |
| Heading Hierarchy | 7 | H2/H3 presence and count |
| Sitemap.xml | 10 | Fetches /sitemap.xml — present (10pts), missing (0pts) |

#### Security & Trust (18%, 100pts)

| Check | Points | Details |
|-------|--------|---------|
| HTTPS / SSL | 20 | HTTPS present |
| Strict-Transport-Security | 12 | HSTS header present |
| Content-Security-Policy | 12 | CSP header present |
| X-Content-Type-Options | 8 | nosniff header present |
| X-Frame-Options | 8 | DENY or SAMEORIGIN header present |
| Referrer-Policy | 8 | Header present |
| Permissions-Policy | 7 | Header present |
| Privacy / Cookie Policy | 15 | Privacy page link detected (10pts), cookie consent/banner (5pts) |
| Mixed Content | 10 | No HTTP resources loaded on HTTPS page |

#### User Experience (20%, 100pts)

| Check | Points | Details |
|-------|--------|---------|
| Mobile Readiness | 15 | Correct viewport meta |
| Accessibility / WCAG | 20 | lang attr (5pts), image alt text (10pts), skip navigation link (5pts) |
| Navigation | 12 | Semantic nav elements (7pts), breadcrumbs (5pts) |
| Page Speed / TTFB | 15 | <0.5s (15pts), <1s (12pts), <2s (8pts), <4s (4pts) |
| Font Loading | 10 | font-display: swap detected (5pts), preloaded fonts (5pts) |
| Form Usability | 8 | Forms with labels (4pts), autocomplete (4pts) |
| Image Optimization | 10 | Explicit width/height (5pts), lazy loading on below-fold images (5pts) |
| Custom 404 | 10 | Fetches a random path — custom 404 page (10pts), default server 404 (0pts) |

#### Content Quality (15%, 100pts)

| Check | Points | Details |
|-------|--------|---------|
| Content Depth | 20 | 1000+ words (20pts), 500-999 (14pts), 250-499 (7pts), <250 (0pts) |
| Heading Structure | 15 | H2 count 4+ (10pts), 2+ (6pts). H3 usage (5pts) |
| Image Alt Text Coverage | 15 | 90%+ (15pts), 60-90% (9pts), <60% (3pts) |
| Image Dimensions | 10 | % of images with explicit width/height attrs |
| List & Structured Content | 10 | Lists present (5pts), tables present (5pts) |
| Internal Linking | 15 | 5+ internal links (15pts), 3+ (10pts), 1+ (5pts) |
| External / Outbound Links | 5 | At least 1 outbound link (5pts) |
| FAQ Content | 10 | FAQ section or FAQ schema detected |

#### Technical Performance (15%, 100pts)

| Check | Points | Details |
|-------|--------|---------|
| HTTPS / SSL | 15 | HTTPS present |
| Server Response / TTFB | 20 | <0.5s (20pts), <1s (16pts), <2s (10pts), <4s (5pts) |
| Content Compression | 15 | Brotli (15pts), gzip (12pts), none (0pts) |
| Caching Strategy | 12 | Cache-Control (7pts), ETag/Last-Modified (5pts) |
| Resource Hints | 8 | preconnect (4pts), preload (4pts) |
| Render-Blocking Scripts | 10 | 0 blocking (10pts), 1-2 (7pts), 3-5 (4pts), 5+ (0pts) |
| HTTP/2+ | 10 | HTTP/2 or HTTP/3 detected (10pts), HTTP/1.1 (0pts) |
| Analytics Detection | 10 | GA4, GTM, Plausible, Fathom, Matomo, Hotjar, Mixpanel detected (10pts) |

#### Conversion & Engagement (10%, 100pts)

| Check | Points | Details |
|-------|--------|---------|
| Call-to-Action Strength | 25 | Generic CTAs: "get started", "contact us", "learn more", "sign up", "try free", "request demo", "buy now", "subscribe", "download" |
| Lead Capture / Forms | 20 | Email input (12pts), newsletter/subscribe signals (8pts) |
| Live Chat & Chatbot | 20 | Same detection as recruitment mode |
| Search Functionality | 15 | Search input detected (15pts) |
| Social Sharing & Links | 10 | Share buttons or social links |
| Analytics & Tracking | 10 | Tracking pixel or analytics present |

---

## Grading Scale

Unchanged across all modes:

| Score | Grade | Label |
|-------|-------|-------|
| 93+ | A+ | World Class |
| 85-92 | A | Excellent |
| 75-84 | B+ | Good |
| 65-74 | B | Average |
| 55-64 | C+ | Below Average |
| 45-54 | C | Poor |
| 35-44 | D | Poor |
| <35 | F | Critical |

---

## Recommendations Engine

Same mechanics across all modes. Changes per mode:

- **Recruitment:** Current impact statements (unchanged)
- **Career Site:** Reframed impact statements — "Top talent expects...", "Your employer brand is weakened by...", "Candidates drop off when..."
- **General:** Generic impact statements — "Visitors are bouncing because...", "Your site is missing...", "Search engines penalize..."

Each check gets a mode-specific `impact` string.

---

## Shazamme Advantage Section

| Mode | Behavior |
|------|----------|
| `recruitment` | Current features, unchanged |
| `career_site` | Reframed features for corporate: "Employer Brand Studio", "ATS Integration Ready", "Career Page Builder" |
| `general` | Hidden entirely |

---

## Frontend Changes

### Landing Page

- 3 mode selector cards above the URL input
- Cards show icon, label, and subtitle
- Selected card gets highlighted border/background
- Grade button disabled until a mode is selected
- Mode stored in URL params for sharing: `?url=...&mode=...`
- Auto-load respects both params

### Results Page

- Mode badge displayed next to domain name in results header
- Pillar names and icons adapt per mode
- Shazamme Advantage section hidden for `general` mode
- All other result UI (score ring, grade badge, pillar cards, recommendations) unchanged

---

## Files Changed

| File | Changes |
|------|---------|
| `grader.py` | Add `mode` param, branch pillar methods per mode, add new checks (ATS, security headers, sitemap, analytics, etc.), mode-specific weights/labels/impacts |
| `server.py` | Parse `mode` query param, validate, pass to grader |
| `public/index.html` | Mode selector UI, pass mode in SSE request, display mode badge in results, conditional Shazamme section |

No new files needed.
