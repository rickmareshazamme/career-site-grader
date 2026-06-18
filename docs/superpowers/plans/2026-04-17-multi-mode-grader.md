# Multi-Mode Website Grader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 3-mode selector (Recruitment / Career Site / General Website) to the grader, with mode-specific pillar criteria, weights, and recommendations.

**Architecture:** Single `CareerSiteGrader` class accepts a `mode` parameter. Each pillar method branches on `self.mode`. Server validates mode from query param. Frontend adds card selector and passes mode through.

**Tech Stack:** Python 3.11, Flask, aiohttp, BeautifulSoup, vanilla HTML/CSS/JS

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `server.py` | Modify | Parse & validate `mode` param, pass to grader |
| `grader.py` | Modify | Add mode param, mode-specific pillar configs, new checks (ATS, security headers, sitemap, analytics, etc.) |
| `public/index.html` | Modify | Mode selector cards, pass mode in SSE, mode badge in results, conditional Shazamme |

---

### Task 1: Server — Accept and Validate Mode Parameter

**Files:**
- Modify: `server.py:12-18` (run_grader_in_thread function)
- Modify: `server.py:37-70` (grade route)

- [ ] **Step 1: Update `run_grader_in_thread` to accept mode**

```python
def run_grader_in_thread(url: str, mode: str, q: queue.Queue):
    """Run the async grader in a background thread and push events to queue."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def collect():
        grader = CareerSiteGrader(url, mode=mode)
        async for event in grader.grade():
            q.put(event)
        q.put(None)  # sentinel

    try:
        loop.run_until_complete(collect())
    except Exception as e:
        q.put({'type': 'error', 'message': str(e)})
        q.put(None)
    finally:
        loop.close()
```

- [ ] **Step 2: Update grade route to parse and validate mode**

```python
@app.route('/grade')
def grade():
    url = (request.args.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL parameter required'}), 400

    VALID_MODES = ('recruitment', 'career_site', 'general')
    mode = (request.args.get('mode') or '').strip()
    if mode not in VALID_MODES:
        return jsonify({'error': f'mode parameter required. Must be one of: {", ".join(VALID_MODES)}'}), 400

    def generate():
        q: queue.Queue = queue.Queue()
        t = threading.Thread(target=run_grader_in_thread, args=(url, mode, q), daemon=True)
        t.start()

        while True:
            try:
                item = q.get(timeout=60)
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Timeout waiting for analysis'})}\n\n"
                break

            if item is None:
                break

            yield f"data: {json.dumps(item)}\n\n"

        t.join(timeout=5)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Access-Control-Allow-Origin': '*',
        },
    )
```

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "feat: accept mode parameter in grade endpoint"
```

---

### Task 2: Grader — Mode Parameter and Weight Configuration

**Files:**
- Modify: `grader.py:12-37` (class init)
- Modify: `grader.py:67-113` (grade method — pillar configs and weights)

- [ ] **Step 1: Update `__init__` to accept mode**

Replace the `__init__` method (lines 24-37):

```python
def __init__(self, url: str, mode: str = 'recruitment'):
    self.raw_url = url
    self.url = self._normalize_url(url)
    self.parsed = urlparse(self.url)
    self.base_url = f"{self.parsed.scheme}://{self.parsed.netloc}"
    self.mode = mode
    self.soup: Optional[BeautifulSoup] = None
    self.html = ''
    self.response_time = 0.0
    self.headers: Dict[str, str] = {}
    self.status_code = 0
    self.robots_txt = ''
    self.has_llms_txt = False
    self.llm_info_url: Optional[str] = None
    self.has_sitemap = False        # general mode
    self.sitemap_url: Optional[str] = None
    self.errors: List[str] = []
```

- [ ] **Step 2: Update `grade()` method — mode-specific pillar configs and weights**

Replace lines 63-113 of the `grade()` method (from `# Parallel secondary fetches` to the `yield complete`):

```python
        # Parallel secondary fetches
        yield {'type': 'status', 'message': 'Checking AI crawler access and robots.txt...', 'progress': 18}
        secondary_fetches = [self._fetch_robots_txt(), self._check_llms_txt(), self._check_llm_info()]
        if self.mode == 'general':
            secondary_fetches.append(self._check_sitemap())
        await asyncio.gather(*secondary_fetches)

        if self.mode == 'recruitment':
            pillars_config = [
                ('seo',        'SEO & Discoverability',    self._analyze_seo,        28),
                ('geo',        'GEO & AI Visibility',       self._analyze_geo,        40),
                ('cx',         'Candidate Experience',      self._analyze_cx,         55),
                ('brand',      'Employer Brand & Content',  self._analyze_brand,      70),
                ('technical',  'Technical Performance',     self._analyze_technical,  83),
                ('conversion', 'Conversion & Engagement',   self._analyze_conversion, 94),
            ]
            weights = {
                'seo': 0.22, 'geo': 0.20, 'cx': 0.22,
                'brand': 0.14, 'technical': 0.13, 'conversion': 0.09,
            }
        elif self.mode == 'career_site':
            pillars_config = [
                ('seo',        'SEO & Discoverability',    self._analyze_seo,        28),
                ('geo',        'GEO & AI Visibility',       self._analyze_geo,        40),
                ('cx',         'Candidate Experience',      self._analyze_cx,         55),
                ('brand',      'Employer Brand & Content',  self._analyze_brand,      70),
                ('technical',  'Technical Performance',     self._analyze_technical,  83),
                ('conversion', 'Conversion & Engagement',   self._analyze_conversion, 94),
            ]
            weights = {
                'seo': 0.18, 'geo': 0.15, 'cx': 0.25,
                'brand': 0.25, 'technical': 0.10, 'conversion': 0.07,
            }
        else:  # general
            pillars_config = [
                ('seo',        'SEO & Discoverability',    self._analyze_seo,        28),
                ('security',   'Security & Trust',          self._analyze_security,   40),
                ('ux',         'User Experience',            self._analyze_ux,         55),
                ('content',    'Content Quality',            self._analyze_content_quality, 70),
                ('technical',  'Technical Performance',     self._analyze_technical,  83),
                ('conversion', 'Conversion & Engagement',   self._analyze_conversion, 94),
            ]
            weights = {
                'seo': 0.22, 'security': 0.18, 'ux': 0.20,
                'content': 0.15, 'technical': 0.15, 'conversion': 0.10,
            }

        pillar_results: Dict[str, Dict] = {}
        for pillar_id, pillar_name, fn, progress in pillars_config:
            yield {'type': 'status', 'message': f'Analysing {pillar_name}...', 'progress': progress}
            result = await fn()
            pillar_results[pillar_id] = result
            yield {'type': 'pillar_complete', 'pillar': pillar_id, 'data': result}

        overall = sum(pillar_results[p]['score'] * w for p, w in weights.items())
        overall = round(overall)

        recommendations = self._generate_recommendations(pillar_results)
        shazamme_items = self._generate_shazamme_advantage(pillar_results) if self.mode != 'general' else []

        text_content = self.soup.get_text() if self.soup else ''
        word_count = len(re.findall(r'\w+', text_content))

        yield {
            'type': 'complete',
            'overall_score': overall,
            'grade': self._score_to_grade(overall),
            'grade_label': self._grade_label(overall),
            'url': self.url,
            'domain': self.parsed.netloc,
            'response_time': round(self.response_time, 2),
            'word_count': word_count,
            'status_code': self.status_code,
            'pillars': pillar_results,
            'recommendations': recommendations,
            'shazamme_advantage': shazamme_items,
            'mode': self.mode,
            'progress': 100,
        }
```

- [ ] **Step 3: Add `_check_sitemap` fetch method** (after `_check_llm_info` ~line 184)

```python
    async def _check_sitemap(self):
        """Check for sitemap.xml — used in general mode."""
        try:
            url = urljoin(self.base_url, '/sitemap.xml')
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=self.HEADERS, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status == 200:
                        content_type = resp.headers.get('content-type', '')
                        body = await resp.text()
                        if 'xml' in content_type or '<urlset' in body or '<sitemapindex' in body:
                            self.has_sitemap = True
                            self.sitemap_url = url
        except Exception:
            pass
```

- [ ] **Step 4: Commit**

```bash
git add grader.py
git commit -m "feat: add mode parameter, weight configs, and sitemap check"
```

---

### Task 3: Grader — Mode-Specific SEO Pillar

**Files:**
- Modify: `grader.py:249-413` (`_analyze_seo` method)

- [ ] **Step 1: Update `_analyze_seo` to branch on mode**

Replace `_analyze_seo` method. The shared checks (title, meta desc, H1, OG, canonical, indexability, headings) stay the same. The schema check and sector pages change per mode. Add sitemap check for general mode.

```python
    async def _analyze_seo(self) -> Dict:
        soup = self.soup
        checks = []
        score = 0
        max_score = 0

        # --- Title --- (shared)
        title_tag = soup.find('title')
        title_text = title_tag.get_text().strip() if title_tag else ''
        title_len = len(title_text)
        if title_text:
            if 30 <= title_len <= 60:
                pts, note = 15, f'Title is {title_len} chars — perfect length (30-60)'
            elif 61 <= title_len <= 70:
                pts, note = 11, f'Title is {title_len} chars — slightly long, may truncate in SERPs'
            elif title_len < 30:
                pts, note = 8, f'Title is {title_len} chars — too short, aim for 30-60'
            else:
                pts, note = 6, f'Title is {title_len} chars — too long, Google truncates at ~60'
        else:
            pts, note = 0, 'No <title> tag — this is a critical SEO failure'
        checks.append({'name': 'Title Tag', 'weight': 15, 'score': pts, 'max': 15,
                        'status': self._pts_status(pts, 15), 'detail': note,
                        'value': title_text[:90] if title_text else None})
        score += pts; max_score += 15

        # --- Meta Description --- (shared)
        meta_desc = soup.find('meta', attrs={'name': re.compile(r'^description$', re.I)})
        desc_text = (meta_desc.get('content') or '').strip() if meta_desc else ''
        desc_len = len(desc_text)
        if desc_text:
            if 120 <= desc_len <= 160:
                pts, note = 12, f'Meta description {desc_len} chars — perfect length'
            elif 80 <= desc_len < 120:
                pts, note = 9, f'Meta description {desc_len} chars — a little short (aim for 120-160)'
            elif desc_len > 160:
                pts, note = 8, f'Meta description {desc_len} chars — will be truncated by Google'
            else:
                pts, note = 4, f'Meta description too short ({desc_len} chars)'
        else:
            pts, note = 0, 'No meta description — Google will auto-generate one (often badly)'
        checks.append({'name': 'Meta Description', 'weight': 12, 'score': pts, 'max': 12,
                        'status': self._pts_status(pts, 12), 'detail': note,
                        'value': desc_text[:130] if desc_text else None})
        score += pts; max_score += 12

        # --- H1 --- (shared)
        h1_tags = soup.find_all('h1')
        h1_count = len(h1_tags)
        h1_text = h1_tags[0].get_text().strip() if h1_tags else ''
        if h1_count == 1:
            pts, note = 10, f'Single H1: "{h1_text[:70]}"'
        elif h1_count > 1:
            pts, note = 5, f'{h1_count} H1 tags found — only one H1 allowed per page'
        else:
            pts, note = 0, 'No H1 tag — critical for SEO and screen readers'
        checks.append({'name': 'H1 Heading', 'weight': 10, 'score': pts, 'max': 10,
                        'status': self._pts_status(pts, 10), 'detail': note,
                        'value': h1_text[:80] if h1_text else None})
        score += pts; max_score += 10

        # --- Schema Markup --- (mode-specific)
        schema_types = self._get_schema_types()
        has_any_schema = bool(schema_types)

        if self.mode == 'recruitment':
            has_job_schema = any(t in ['JobPosting'] for t in schema_types)
            has_org_schema = any(t in ['Organization', 'Corporation', 'EmploymentAgency',
                                        'StaffingAgency', 'LocalBusiness', 'RecruitmentAgency'] for t in schema_types)
            has_faq_schema = 'FAQPage' in schema_types
            has_breadcrumb = 'BreadcrumbList' in schema_types
            sch_pts = 0; sch_notes = []
            if has_any_schema:
                sch_pts += 5; unique_types = list(dict.fromkeys(schema_types))[:6]
                sch_notes.append(f'Types: {", ".join(unique_types)}')
            else:
                sch_notes.append('No structured data found')
            if has_org_schema: sch_pts += 7; sch_notes.append('Organization ✓')
            else: sch_notes.append('Missing Organization/StaffingAgency schema')
            if has_job_schema: sch_pts += 8; sch_notes.append('JobPosting ✓')
            if has_faq_schema: sch_pts += 3; sch_notes.append('FAQPage ✓')
            if has_breadcrumb: sch_pts += 2; sch_notes.append('BreadcrumbList ✓')
            sch_pts = min(sch_pts, 25)
            sch_max = 25

        elif self.mode == 'career_site':
            has_job_schema = any(t in ['JobPosting'] for t in schema_types)
            has_org_schema = any(t in ['Organization', 'Corporation'] for t in schema_types)
            has_breadcrumb = 'BreadcrumbList' in schema_types
            has_faq_schema = 'FAQPage' in schema_types
            sch_pts = 0; sch_notes = []
            if has_any_schema:
                sch_pts += 2; unique_types = list(dict.fromkeys(schema_types))[:6]
                sch_notes.append(f'Types: {", ".join(unique_types)}')
            else:
                sch_notes.append('No structured data found')
            if has_job_schema: sch_pts += 10; sch_notes.append('JobPosting ✓')
            else: sch_notes.append('Missing JobPosting schema — critical for career sites')
            if has_org_schema: sch_pts += 7; sch_notes.append('Organization ✓')
            if has_breadcrumb: sch_pts += 3; sch_notes.append('BreadcrumbList ✓')
            if has_faq_schema: sch_pts += 3; sch_notes.append('FAQPage ✓')
            sch_pts = min(sch_pts, 25)
            sch_max = 25

        else:  # general
            has_org_schema = any(t in ['Organization', 'Corporation', 'LocalBusiness'] for t in schema_types)
            has_breadcrumb = 'BreadcrumbList' in schema_types
            has_faq_schema = 'FAQPage' in schema_types
            sch_pts = 0; sch_notes = []
            if has_any_schema:
                sch_pts += 5; unique_types = list(dict.fromkeys(schema_types))[:6]
                sch_notes.append(f'Types: {", ".join(unique_types)}')
            else:
                sch_notes.append('No structured data found')
            if has_org_schema: sch_pts += 5; sch_notes.append('Organization ✓')
            if has_breadcrumb: sch_pts += 3; sch_notes.append('BreadcrumbList ✓')
            if has_faq_schema: sch_pts += 2; sch_notes.append('FAQPage ✓')
            has_local = 'LocalBusiness' in schema_types
            if has_local: sch_pts += 3; sch_notes.append('LocalBusiness ✓')
            sch_pts = min(sch_pts, 18)
            sch_max = 18

        checks.append({'name': 'Schema / Structured Data', 'weight': sch_max, 'score': sch_pts, 'max': sch_max,
                        'status': self._pts_status(sch_pts, sch_max), 'detail': ' | '.join(sch_notes),
                        'value': f'{len(schema_types)} schema type(s) detected'})
        score += sch_pts; max_score += sch_max

        # --- Open Graph --- (shared)
        og_title  = soup.find('meta', property='og:title')
        og_desc   = soup.find('meta', property='og:description')
        og_image  = soup.find('meta', property='og:image')
        og_type   = soup.find('meta', property='og:type')
        og_pts = 0; og_notes = []
        if og_title:  og_pts += 3; og_notes.append('og:title ✓')
        else: og_notes.append('Missing og:title')
        if og_desc:   og_pts += 3; og_notes.append('og:description ✓')
        else: og_notes.append('Missing og:description')
        if og_image:  og_pts += 5; og_notes.append('og:image ✓')
        else: og_notes.append('No og:image — poor appearance on social shares')
        if og_type:   og_pts += 2; og_notes.append('og:type ✓')
        checks.append({'name': 'Open Graph / Social Tags', 'weight': 13, 'score': og_pts, 'max': 13,
                        'status': self._pts_status(og_pts, 13), 'detail': ', '.join(og_notes)})
        score += og_pts; max_score += 13

        # --- Canonical --- (shared)
        canonical = soup.find('link', rel='canonical')
        if canonical:
            pts, note = 8, f'Canonical: {(canonical.get("href") or "")[:80]}'
        else:
            pts, note = 0, 'No canonical tag — duplicate content risk'
        checks.append({'name': 'Canonical URL', 'weight': 8, 'score': pts, 'max': 8,
                        'status': self._pts_status(pts, 8), 'detail': note})
        score += pts; max_score += 8

        # --- Indexability --- (shared)
        robots_meta = soup.find('meta', attrs={'name': re.compile(r'^robots$', re.I)})
        if robots_meta:
            content = (robots_meta.get('content') or '').lower()
            if 'noindex' in content:
                pts, note = 0, 'NOINDEX set — this page will NOT appear in Google!'
            else:
                pts, note = 7, f'Robots meta: {content or "index,follow"}'
        else:
            pts, note = 7, 'No robots meta (defaults to indexable — fine)'
        checks.append({'name': 'Indexability', 'weight': 7, 'score': pts, 'max': 7,
                        'status': self._pts_status(pts, 7), 'detail': note})
        score += pts; max_score += 7

        # --- Heading hierarchy --- (shared, different max for general)
        h2s = len(soup.find_all('h2'))
        h3s = len(soup.find_all('h3'))
        heading_max = 7 if self.mode == 'general' else 10
        if h2s >= 4:
            pts, note = heading_max, f'{h2s} H2s, {h3s} H3s — strong content structure'
        elif h2s >= 2:
            pts, note = round(heading_max * 0.7), f'{h2s} H2s, {h3s} H3s — decent structure'
        elif h2s == 1:
            pts, note = round(heading_max * 0.4), f'Only 1 H2 — more subheadings would improve structure'
        else:
            pts, note = 0, 'No H2 subheadings — flat structure hurts SEO and readability'
        checks.append({'name': 'Heading Hierarchy', 'weight': heading_max, 'score': pts, 'max': heading_max,
                        'status': self._pts_status(pts, heading_max), 'detail': note})
        score += pts; max_score += heading_max

        # --- Mode-specific extras ---
        if self.mode == 'recruitment':
            sector_check = self._check_sector_pages(soup)
            checks.append(sector_check)
            score += sector_check['score']; max_score += sector_check['max']

        elif self.mode == 'general':
            # Sitemap check (10pts)
            if self.has_sitemap:
                pts, note = 10, 'sitemap.xml found ✓'
            else:
                pts, note = 0, 'No sitemap.xml — search engines rely on this for discovery'
            checks.append({'name': 'Sitemap.xml', 'weight': 10, 'score': pts, 'max': 10,
                            'status': self._pts_status(pts, 10), 'detail': note})
            score += pts; max_score += 10

        pct = round(score / max_score * 100) if max_score else 0
        has_job_schema = any(t in ['JobPosting'] for t in schema_types) if self.mode != 'general' else False
        return {
            'name': 'SEO & Discoverability',
            'icon': 'search',
            'color': '#6366f1',
            'score': pct,
            'grade': self._score_to_grade(pct),
            'checks': checks,
            'summary': self._seo_summary(pct, has_any_schema, has_job_schema),
        }
```

- [ ] **Step 2: Commit**

```bash
git add grader.py
git commit -m "feat: mode-specific SEO pillar with schema branching and sitemap check"
```

---

### Task 4: Grader — Career Site CX Pillar (ATS Detection)

**Files:**
- Modify: `grader.py:713-851` (`_analyze_cx` method)

- [ ] **Step 1: Update `_analyze_cx` to branch on mode for career_site**

The career_site mode replaces "Apply Flow & Job Search" (25pts) with two checks: "Apply Flow" (20pts) and "ATS Platform Detection" (15pts). Adjust other weights slightly. Add the ATS detection logic.

After the existing `_analyze_cx` method, the career_site mode needs these changes:
- Mobile: 12pts instead of 15
- Accessibility: 15pts (same)
- Apply Flow: 20pts (was 25, split out ATS)
- ATS Detection: 15pts (NEW)
- Navigation: 10pts (was 15)
- HTTPS: 8pts (was 10)
- TTFB: 10pts (same)
- Form Usability: 10pts (same)

Replace the entire `_analyze_cx` method:

```python
    async def _analyze_cx(self) -> Dict:
        soup = self.soup
        checks = []
        score = 0
        max_score = 0

        # --- Mobile Readiness ---
        mobile_max = 12 if self.mode == 'career_site' else 15
        viewport = soup.find('meta', attrs={'name': re.compile(r'^viewport$', re.I)})
        if viewport:
            content = (viewport.get('content') or '').lower()
            if 'width=device-width' in content:
                pts, note = mobile_max, 'Responsive viewport configured correctly ✓'
            else:
                pts, note = round(mobile_max * 0.47), f'Viewport meta present but not optimal: {content[:60]}'
        else:
            pts, note = 0, 'No viewport meta — site will appear broken on mobile (60%+ of job searches)'
        checks.append({'name': 'Mobile Readiness', 'weight': mobile_max, 'score': pts, 'max': mobile_max,
                        'status': self._pts_status(pts, mobile_max), 'detail': note})
        score += pts; max_score += mobile_max

        # --- Accessibility ---
        html_tag = soup.find('html')
        lang = (html_tag.get('lang') or '') if html_tag else ''
        images = soup.find_all('img')
        imgs_with_alt = [i for i in images if i.get('alt') is not None]
        lang_pts = 5 if lang else 0
        lang_note = f'lang="{lang}" ✓' if lang else 'Missing lang attribute on <html>'
        if images:
            ratio = len(imgs_with_alt) / len(images)
            if ratio >= 0.9:
                alt_pts, alt_note = 10, f'{len(imgs_with_alt)}/{len(images)} images have alt text ✓'
            elif ratio >= 0.6:
                alt_pts, alt_note = 6, f'{len(imgs_with_alt)}/{len(images)} images have alt text'
            else:
                alt_pts, alt_note = 2, f'Only {len(imgs_with_alt)}/{len(images)} images have alt text — accessibility fail'
        else:
            alt_pts, alt_note = 5, 'No images found to evaluate'
        a11y_pts = lang_pts + alt_pts
        checks.append({'name': 'Accessibility (WCAG)', 'weight': 15, 'score': a11y_pts, 'max': 15,
                        'status': self._pts_status(a11y_pts, 15), 'detail': f'{lang_note} | {alt_note}'})
        score += a11y_pts; max_score += 15

        # --- Apply & Job Search ---
        apply_btns = soup.find_all(
            lambda t: t.name in ['a', 'button'] and
            re.search(r'\bapply\b', t.get_text(), re.I)
        )
        page_text_lower = soup.get_text().lower()
        search_box = (soup.find('input', attrs={'type': 'search'}) or
                      soup.find('input', placeholder=re.compile(r'search|job|role|keyword', re.I)) or
                      soup.find('input', id=re.compile(r'search|job', re.I)))
        search_form = (soup.find('form', id=re.compile(r'search', re.I)) or
                       soup.find('form', class_=re.compile(r'search|job-search', re.I)))
        has_job_search = bool(search_box or search_form or 'job search' in page_text_lower)

        if self.mode == 'career_site':
            apply_max = 20
            apply_pts = 0; apply_notes = []
            if len(apply_btns) >= 2:
                apply_pts += 10; apply_notes.append(f'{len(apply_btns)} Apply buttons ✓')
            elif apply_btns:
                apply_pts += 6; apply_notes.append(f'{len(apply_btns)} Apply button found')
            else:
                apply_notes.append('No Apply button detected — how do candidates apply?')
            # Check form field count for career site
            forms = soup.find_all('form')
            visible_inputs = [i for i in soup.find_all('input') if i.get('type', 'text') not in ('hidden', 'submit', 'button')]
            if forms and visible_inputs:
                field_count = len(visible_inputs)
                if field_count <= 5:
                    apply_pts += 5; apply_notes.append(f'{field_count} form fields — streamlined application ✓')
                elif field_count <= 10:
                    apply_pts += 3; apply_notes.append(f'{field_count} form fields — consider reducing')
                else:
                    apply_notes.append(f'{field_count} form fields — too many, candidates drop off')
            if has_job_search:
                apply_pts += 5; apply_notes.append('Job search detected ✓')
            apply_pts = min(apply_pts, apply_max)
        else:
            apply_max = 25
            apply_pts = 0; apply_notes = []
            if len(apply_btns) >= 2:
                apply_pts += 15; apply_notes.append(f'{len(apply_btns)} Apply buttons ✓')
            elif apply_btns:
                apply_pts += 10; apply_notes.append(f'{len(apply_btns)} Apply button found')
            else:
                apply_notes.append('No Apply button detected — how do candidates apply?')
            if has_job_search:
                apply_pts += 10; apply_notes.append('Job search detected ✓')
            else:
                apply_notes.append('No job search — candidates cannot find relevant roles')

        checks.append({'name': 'Apply Flow & Job Search', 'weight': apply_max, 'score': apply_pts, 'max': apply_max,
                        'status': self._pts_status(apply_pts, apply_max), 'detail': ' | '.join(apply_notes)})
        score += apply_pts; max_score += apply_max

        # --- ATS Detection (career_site only) ---
        if self.mode == 'career_site':
            ats_check = self._detect_ats(soup)
            checks.append(ats_check)
            score += ats_check['score']; max_score += ats_check['max']

        # --- Navigation ---
        nav_max = 10 if self.mode == 'career_site' else 15
        nav_tags = soup.find_all('nav')
        breadcrumb_html = bool(soup.find(class_=re.compile(r'breadcrumb', re.I)))
        schema_types = self._get_schema_types()
        has_breadcrumb_schema = 'BreadcrumbList' in schema_types
        nav_pts = 0; nav_notes = []
        if nav_tags:
            nav_pts += round(nav_max * 0.67); nav_notes.append(f'{len(nav_tags)} <nav> element(s) ✓')
        else:
            nav_notes.append('No semantic <nav> — navigation not machine-readable')
        if has_breadcrumb_schema or breadcrumb_html:
            nav_pts += round(nav_max * 0.33); nav_notes.append('Breadcrumbs present ✓')
        nav_pts = min(nav_pts, nav_max)
        checks.append({'name': 'Navigation & Structure', 'weight': nav_max, 'score': nav_pts, 'max': nav_max,
                        'status': self._pts_status(nav_pts, nav_max), 'detail': ' | '.join(nav_notes)})
        score += nav_pts; max_score += nav_max

        # --- HTTPS Trust Signal ---
        https_max = 8 if self.mode == 'career_site' else 10
        is_https = self.url.startswith('https://')
        pts = https_max if is_https else 0
        note = 'HTTPS ✓ — candidates trust this site' if is_https else 'Not HTTPS — browsers show "Not Secure" warning'
        checks.append({'name': 'HTTPS Trust Signal', 'weight': https_max, 'score': pts, 'max': https_max,
                        'status': self._pts_status(pts, https_max), 'detail': note})
        score += pts; max_score += https_max

        # --- Page Speed (TTFB) ---
        rt = self.response_time
        if rt < 0.5:   pts, note = 10, f'Excellent TTFB: {rt:.2f}s'
        elif rt < 1.0:  pts, note = 8, f'Good TTFB: {rt:.2f}s'
        elif rt < 2.0:  pts, note = 5, f'Slow TTFB: {rt:.2f}s — investigate server performance'
        elif rt < 4.0:  pts, note = 2, f'Very slow: {rt:.2f}s — 53% of users leave after 3s'
        else:           pts, note = 0, f'Critical: {rt:.2f}s TTFB — site is painfully slow'
        checks.append({'name': 'Page Speed (TTFB)', 'weight': 10, 'score': pts, 'max': 10,
                        'status': self._pts_status(pts, 10), 'detail': note})
        score += pts; max_score += 10

        # --- Form Usability ---
        forms = soup.find_all('form')
        inputs = soup.find_all('input')
        autocomplete = [i for i in inputs if i.get('autocomplete')]
        form_pts = 0; form_notes = []
        if forms:
            form_pts += 5; form_notes.append(f'{len(forms)} form(s) detected')
            if autocomplete:
                form_pts += 5; form_notes.append(f'{len(autocomplete)} fields with autocomplete ✓')
            else:
                form_notes.append('No autocomplete attributes on inputs')
        else:
            form_notes.append('No forms detected')
        form_pts = min(form_pts, 10)
        checks.append({'name': 'Form Usability', 'weight': 10, 'score': form_pts, 'max': 10,
                        'status': self._pts_status(form_pts, 10), 'detail': ' | '.join(form_notes)})
        score += form_pts; max_score += 10

        pct = round(score / max_score * 100) if max_score else 0
        pillar_name = 'Candidate Experience'
        return {
            'name': pillar_name,
            'icon': 'person',
            'color': '#22d3ee',
            'score': pct,
            'grade': self._score_to_grade(pct),
            'checks': checks,
            'summary': self._cx_summary(pct),
        }
```

- [ ] **Step 2: Add `_detect_ats` helper method** (after `_cx_summary`)

```python
    def _detect_ats(self, soup: BeautifulSoup) -> Dict:
        """Detect ATS platforms embedded in career site."""
        html_lower = self.html.lower()
        ATS_PATTERNS = {
            'Greenhouse': r'greenhouse\.io|boards\.greenhouse|grnh\.se',
            'Lever': r'lever\.co|jobs\.lever\.co',
            'Workday': r'workday\.com|myworkdayjobs\.com|wd\d+\.myworkdayjobs',
            'Taleo': r'taleo\.net|oracle.*taleo',
            'iCIMS': r'icims\.com|careers-.*\.icims',
            'SmartRecruiters': r'smartrecruiters\.com|jobs\.smartrecruiters',
            'BambooHR': r'bamboohr\.com',
            'Jobvite': r'jobvite\.com|jobs\.jobvite',
            'Ashby': r'ashbyhq\.com|jobs\.ashbyhq',
            'Breezy': r'breezy\.hr',
            'Workable': r'workable\.com|apply\.workable',
            'JazzHR': r'jazzhr\.com|app\.jazz\.co',
            'Recruitee': r'recruitee\.com',
            'Pinpoint': r'pinpointhq\.com',
        }
        detected = []
        for name, pattern in ATS_PATTERNS.items():
            if re.search(pattern, html_lower):
                detected.append(name)
        # Also check iframes src
        for iframe in soup.find_all('iframe', src=True):
            src = (iframe.get('src') or '').lower()
            for name, pattern in ATS_PATTERNS.items():
                if name not in detected and re.search(pattern, src):
                    detected.append(name)

        if detected:
            pts = 15
            note = f'ATS detected: {", ".join(detected)} ✓'
        else:
            pts = 0
            note = 'No ATS platform detected — ensure your application tracking system is properly integrated'
        return {
            'name': 'ATS Platform Detection', 'weight': 15, 'score': pts, 'max': 15,
            'status': self._pts_status(pts, 15), 'detail': note,
            'value': ', '.join(detected) if detected else None,
        }
```

- [ ] **Step 3: Commit**

```bash
git add grader.py
git commit -m "feat: career site CX pillar with ATS detection"
```

---

### Task 5: Grader — Career Site Brand Pillar (Culture, Employee Stories, DE&I Page)

**Files:**
- Modify: `grader.py:866-987` (`_analyze_brand` method)

- [ ] **Step 1: Update `_analyze_brand` to branch for career_site mode**

Replace the entire `_analyze_brand` method. The career_site mode uses different check distribution:
- Culture & Team Content: 20pts
- Employee Stories/Testimonials: 15pts
- EVP & Pay Transparency: 20pts
- DE&I Commitment: 12pts
- Video Content: 10pts
- Visual Brand: 8pts
- Social Presence: 15pts

```python
    async def _analyze_brand(self) -> Dict:
        soup = self.soup
        checks = []
        score = 0
        max_score = 0
        page_text = soup.get_text().lower()
        html_lower = self.html.lower()

        if self.mode == 'career_site':
            # --- Culture & Team Content (20pts) ---
            culture_page = bool(re.search(r'culture|our.?team|meet.?the.?team|about.?us|who.?we.?are', page_text))
            team_page = bool(re.search(r'our.?people|the.?team|leadership|meet.?us', page_text))
            life_at = bool(re.search(r'life.?at|working.?at|working.?here|day.?in.?the.?life|what.?it.?s.?like', page_text))
            cult_pts = 0; cult_notes = []
            if culture_page: cult_pts += 10; cult_notes.append('Culture content detected ✓')
            else: cult_notes.append('No culture/about page content')
            if team_page: cult_pts += 5; cult_notes.append('Team/people content ✓')
            if life_at: cult_pts += 5; cult_notes.append('"Life at" content ✓')
            cult_pts = min(cult_pts, 20)
            checks.append({'name': 'Culture & Team Content', 'weight': 20, 'score': cult_pts, 'max': 20,
                            'status': self._pts_status(cult_pts, 20), 'detail': ' | '.join(cult_notes)})
            score += cult_pts; max_score += 20

            # --- Employee Stories / Testimonials (15pts) ---
            review_els = soup.find_all(class_=re.compile(r'review|testimonial|story|quote|employee', re.I))
            has_testimonial = bool(re.search(
                r'testimonial|employee.?stor|our.?people.?say|what.?they.?say|hear.?from|their.?words', page_text))
            has_video_testimonial = bool(re.search(r'video.*testimonial|testimonial.*video|employee.*video', page_text))
            emp_pts = 0; emp_notes = []
            if review_els or has_testimonial:
                emp_pts += 10; emp_notes.append('Employee stories/testimonials detected ✓')
            else:
                emp_notes.append('No employee stories — candidates want to hear from real employees')
            if has_video_testimonial:
                emp_pts += 5; emp_notes.append('Video testimonials ✓')
            emp_pts = min(emp_pts, 15)
            checks.append({'name': 'Employee Stories & Testimonials', 'weight': 15, 'score': emp_pts, 'max': 15,
                            'status': self._pts_status(emp_pts, 15), 'detail': ' | '.join(emp_notes)})
            score += emp_pts; max_score += 15

            # --- EVP & Pay Transparency (20pts) ---
            salary_sig = bool(re.search(r'salary|pay|compens|remunerat|\$|£|€|aud\b|wage|earn', page_text))
            benefit_sig = bool(re.search(
                r'benefit|perk|health|dental|vision|401k|pension|super(annuat)?|pto|vacation|holiday|'
                r'\bremote\b|flexib|hybrid|parental|wellbeing|wellness', page_text))
            growth_sig = bool(re.search(
                r'career.?growth|career.?develop|career.?progress|learning.?&.?develop|'
                r'professional.?develop|training|mentor|promotion|career.?path', page_text))
            evp_pts = 0; evp_notes = []
            if salary_sig: evp_pts += 8; evp_notes.append('Salary/pay transparency ✓')
            else: evp_notes.append('No pay info — 67% of candidates want salary upfront')
            if benefit_sig: evp_pts += 7; evp_notes.append('Benefits/perks highlighted ✓')
            else: evp_notes.append('Benefits not prominently mentioned')
            if growth_sig: evp_pts += 5; evp_notes.append('Career growth/progression content ✓')
            else: evp_notes.append('No career development content')
            evp_pts = min(evp_pts, 20)
            checks.append({'name': 'EVP & Pay Transparency', 'weight': 20, 'score': evp_pts, 'max': 20,
                            'status': self._pts_status(evp_pts, 20), 'detail': ' | '.join(evp_notes)})
            score += evp_pts; max_score += 20

            # --- DE&I Commitment (12pts) ---
            dei_page = bool(soup.find('a', href=re.compile(r'diversity|inclusion|dei|equity|belonging', re.I)))
            dei_content = bool(re.search(
                r'\bdei\b|diversity|equity|inclusion|equal.?opportunit|eeo|belonging|erg\b|'
                r'accessible|disability|neurodiver', page_text))
            dei_pts = 0; dei_notes = []
            if dei_page: dei_pts += 8; dei_notes.append('Dedicated DE&I page linked ✓')
            elif dei_content: dei_pts += 4; dei_notes.append('DE&I keywords present')
            else: dei_notes.append('No DE&I content — increasingly important for talent attraction')
            if dei_content and dei_page: dei_pts = 12
            dei_pts = min(dei_pts, 12)
            checks.append({'name': 'DE&I Commitment', 'weight': 12, 'score': dei_pts, 'max': 12,
                            'status': self._pts_status(dei_pts, 12), 'detail': ' | '.join(dei_notes)})
            score += dei_pts; max_score += 12

        else:
            # --- Social Proof (recruitment mode) ---
            review_els = soup.find_all(class_=re.compile(r'review|testimonial|rating|quote', re.I))
            has_testimonial = bool(re.search(
                r'testimonial|what.{1,20}(say|think)|our clients say|candidates say|heard from|they said',
                page_text, re.I))
            has_stars = bool(soup.find_all(class_=re.compile(r'star|rating|score', re.I)))
            sp_pts = 0; sp_notes = []
            if review_els or has_testimonial:
                sp_pts += 12; sp_notes.append('Testimonials / social proof detected ✓')
            else:
                sp_notes.append('No testimonials found — candidates rely on social proof')
            if has_stars:
                sp_pts += 8; sp_notes.append('Star ratings / scores detected ✓')
            sp_pts = min(sp_pts, 20)
            checks.append({'name': 'Social Proof & Reviews', 'weight': 20, 'score': sp_pts, 'max': 20,
                            'status': self._pts_status(sp_pts, 20), 'detail': ' | '.join(sp_notes)})
            score += sp_pts; max_score += 20

            # --- EVP (recruitment mode) ---
            salary_sig = bool(re.search(r'salary|pay|compens|remunerat|\$|£|€|aud\b|wage|earn', page_text))
            benefit_sig = bool(re.search(
                r'benefit|perk|health|dental|vision|401k|pension|super(annuat)?|pto|vacation|holiday|'
                r'\bremote\b|flexib|hybrid|parental|wellbeing|wellness', page_text))
            culture_sig = bool(re.search(
                r'culture|values?|mission|vision|diversity|inclus|belong|community|team spirit', page_text))
            evp_pts = 0; evp_notes = []
            if salary_sig: evp_pts += 10; evp_notes.append('Salary/pay transparency ✓')
            else: evp_notes.append('No pay info — 67% of candidates want salary upfront')
            if benefit_sig: evp_pts += 10; evp_notes.append('Benefits/perks highlighted ✓')
            else: evp_notes.append('Benefits not prominently mentioned')
            if culture_sig: evp_pts += 5; evp_notes.append('Culture/values content ✓')
            evp_pts = min(evp_pts, 25)
            checks.append({'name': 'EVP & Pay Transparency', 'weight': 25, 'score': evp_pts, 'max': 25,
                            'status': self._pts_status(evp_pts, 25), 'detail': ' | '.join(evp_notes)})
            score += evp_pts; max_score += 25

            # --- DE&I (recruitment mode) ---
            dei_sig = bool(re.search(
                r'\bdei\b|diversity|equity|inclusion|equal opportunit|eeo|belonging|erg\b', page_text))
            dei_pts = 10 if dei_sig else 0
            dei_note = 'DE&I commitment visible ✓' if dei_sig else \
                       'No DE&I content — increasingly important for talent attraction'
            checks.append({'name': 'DE&I Commitment', 'weight': 10, 'score': dei_pts, 'max': 10,
                            'status': self._pts_status(dei_pts, 10), 'detail': dei_note})
            score += dei_pts; max_score += 10

        # --- Video Content --- (shared, different max for career_site)
        video_max = 10 if self.mode == 'career_site' else 15
        videos = soup.find_all('video')
        iframes = soup.find_all('iframe')
        yt_vimeo = [i for i in iframes if re.search(r'youtube|vimeo|loom|wistia', i.get('src', ''), re.I)]
        has_video = bool(videos) or bool(yt_vimeo)
        video_pts = video_max if has_video else 0
        video_note = f'{len(videos) + len(yt_vimeo)} video element(s) — great for engagement ✓' if has_video \
                     else 'No video — video content increases application intent by 34%'
        checks.append({'name': 'Video Content', 'weight': video_max, 'score': video_pts, 'max': video_max,
                        'status': self._pts_status(video_pts, video_max), 'detail': video_note})
        score += video_pts; max_score += video_max

        # --- Visual Brand --- (shared, different max for career_site)
        brand_max = 8 if self.mode == 'career_site' else 15
        og_img = soup.find('meta', property='og:image')
        favicon = soup.find('link', rel=re.compile(r'icon', re.I))
        brand_pts = 0; brand_notes = []
        if og_img:
            brand_pts += round(brand_max * 0.63); brand_notes.append('og:image set ✓')
        else:
            brand_notes.append('No og:image — unbranded appearance on social shares')
        if favicon:
            brand_pts += round(brand_max * 0.37); brand_notes.append('Favicon present ✓')
        else:
            brand_notes.append('No favicon — affects trust and brand recall')
        brand_pts = min(brand_pts, brand_max)
        checks.append({'name': 'Visual Brand Assets', 'weight': brand_max, 'score': brand_pts, 'max': brand_max,
                        'status': self._pts_status(brand_pts, brand_max), 'detail': ' | '.join(brand_notes)})
        score += brand_pts; max_score += brand_max

        # --- Social Presence --- (shared)
        platforms = ['linkedin', 'twitter', 'x.com', 'facebook', 'instagram', 'youtube',
                     'glassdoor', 'indeed', 'tiktok']
        found_platforms = set()
        for link in soup.find_all('a', href=True):
            href = (link.get('href') or '').lower()
            for p in platforms:
                if p in href:
                    found_platforms.add(p)
        soc_pts = min(len(found_platforms) * 3, 15)
        soc_note = f'Social: {", ".join(found_platforms)} ✓' if found_platforms else 'No social media links found'
        checks.append({'name': 'Social Presence', 'weight': 15, 'score': soc_pts, 'max': 15,
                        'status': self._pts_status(soc_pts, 15), 'detail': soc_note})
        score += soc_pts; max_score += 15

        pct = round(score / max_score * 100) if max_score else 0
        return {
            'name': 'Employer Brand & Content',
            'icon': 'star',
            'color': '#f59e0b',
            'score': pct,
            'grade': self._score_to_grade(pct),
            'checks': checks,
            'summary': self._brand_summary(pct),
        }
```

- [ ] **Step 2: Commit**

```bash
git add grader.py
git commit -m "feat: career site brand pillar with culture, employee stories, DE&I page detection"
```

---

### Task 6: Grader — General Mode Pillars (Security, UX, Content Quality)

**Files:**
- Modify: `grader.py` — Add three new methods after `_analyze_brand`

- [ ] **Step 1: Add `_analyze_security` method**

```python
    async def _analyze_security(self) -> Dict:
        """Security & Trust pillar for general mode."""
        soup = self.soup
        checks = []
        score = 0
        max_score = 0

        # --- HTTPS ---
        is_https = self.url.startswith('https://')
        pts = 20 if is_https else 0
        note = 'HTTPS secure ✓' if is_https else 'Not HTTPS — browsers warn visitors and Google ranks lower'
        checks.append({'name': 'HTTPS / SSL', 'weight': 20, 'score': pts, 'max': 20,
                        'status': self._pts_status(pts, 20), 'detail': note})
        score += pts; max_score += 20

        # --- Security Headers ---
        def _check_header(name, header_key, max_pts, present_note, missing_note):
            val = self.headers.get(header_key, '')
            pts = max_pts if val else 0
            note = f'{present_note}: {val[:60]}' if val else missing_note
            return {'name': name, 'weight': max_pts, 'score': pts, 'max': max_pts,
                    'status': self._pts_status(pts, max_pts), 'detail': note,
                    'value': val[:80] if val else None}

        for check_data in [
            ('Strict-Transport-Security', 'strict-transport-security', 12,
             'HSTS enabled ✓', 'No HSTS header — browsers can be downgraded to HTTP'),
            ('Content-Security-Policy', 'content-security-policy', 12,
             'CSP present ✓', 'No CSP header — vulnerable to XSS injection'),
            ('X-Content-Type-Options', 'x-content-type-options', 8,
             'X-Content-Type-Options ✓', 'Missing X-Content-Type-Options: nosniff'),
            ('X-Frame-Options', 'x-frame-options', 8,
             'X-Frame-Options ✓', 'Missing X-Frame-Options — clickjacking risk'),
            ('Referrer-Policy', 'referrer-policy', 8,
             'Referrer-Policy ✓', 'No Referrer-Policy header'),
            ('Permissions-Policy', 'permissions-policy', 7,
             'Permissions-Policy ✓', 'No Permissions-Policy — browser features not restricted'),
        ]:
            check = _check_header(*check_data)
            checks.append(check)
            score += check['score']; max_score += check['max']

        # --- Privacy / Cookie Policy (15pts) ---
        privacy_link = soup.find('a', href=re.compile(r'privacy|cookie|gdpr|data.?protection', re.I))
        cookie_banner = bool(soup.find(class_=re.compile(r'cookie|consent|gdpr', re.I))) or \
                        bool(re.search(r'cookie.?consent|cookie.?banner|accept.?cookies', self.html.lower()))
        priv_pts = 0; priv_notes = []
        if privacy_link:
            priv_pts += 10; priv_notes.append('Privacy policy page linked ✓')
        else:
            priv_notes.append('No privacy policy link found')
        if cookie_banner:
            priv_pts += 5; priv_notes.append('Cookie consent detected ✓')
        priv_pts = min(priv_pts, 15)
        checks.append({'name': 'Privacy & Cookie Policy', 'weight': 15, 'score': priv_pts, 'max': 15,
                        'status': self._pts_status(priv_pts, 15), 'detail': ' | '.join(priv_notes)})
        score += priv_pts; max_score += 15

        # --- Mixed Content (10pts) ---
        http_resources = soup.find_all(src=re.compile(r'^http://', re.I))
        if is_https and http_resources:
            pts = 0
            note = f'{len(http_resources)} HTTP resource(s) on HTTPS page — mixed content warning'
        else:
            pts = 10
            note = 'No mixed content detected ✓' if is_https else 'Site is not HTTPS'
        checks.append({'name': 'Mixed Content', 'weight': 10, 'score': pts, 'max': 10,
                        'status': self._pts_status(pts, 10), 'detail': note})
        score += pts; max_score += 10

        pct = round(score / max_score * 100) if max_score else 0
        return {
            'name': 'Security & Trust',
            'icon': 'shield',
            'color': '#f43f5e',
            'score': pct,
            'grade': self._score_to_grade(pct),
            'checks': checks,
            'summary': self._security_summary(pct),
        }

    def _security_summary(self, score):
        if score >= 80: return 'Strong security posture. Headers, HTTPS, and privacy controls are well-configured.'
        if score >= 60: return 'Decent security but missing some important headers. Review recommendations.'
        if score >= 40: return 'Several security gaps expose your site and users to risk.'
        return 'Critical security issues. Your site lacks basic protections against common attacks.'
```

- [ ] **Step 2: Add `_analyze_ux` method**

```python
    async def _analyze_ux(self) -> Dict:
        """User Experience pillar for general mode."""
        soup = self.soup
        checks = []
        score = 0
        max_score = 0

        # --- Mobile Readiness (15pts) ---
        viewport = soup.find('meta', attrs={'name': re.compile(r'^viewport$', re.I)})
        if viewport:
            content = (viewport.get('content') or '').lower()
            if 'width=device-width' in content:
                pts, note = 15, 'Responsive viewport ✓'
            else:
                pts, note = 7, f'Viewport present but not optimal: {content[:60]}'
        else:
            pts, note = 0, 'No viewport meta — site broken on mobile'
        checks.append({'name': 'Mobile Readiness', 'weight': 15, 'score': pts, 'max': 15,
                        'status': self._pts_status(pts, 15), 'detail': note})
        score += pts; max_score += 15

        # --- Accessibility (20pts) ---
        html_tag = soup.find('html')
        lang = (html_tag.get('lang') or '') if html_tag else ''
        images = soup.find_all('img')
        imgs_with_alt = [i for i in images if i.get('alt') is not None]
        skip_link = bool(soup.find('a', href='#main')) or bool(soup.find('a', class_=re.compile(r'skip', re.I)))
        a11y_pts = 0; a11y_notes = []
        if lang: a11y_pts += 5; a11y_notes.append(f'lang="{lang}" ✓')
        else: a11y_notes.append('Missing lang attribute')
        if images:
            ratio = len(imgs_with_alt) / len(images)
            if ratio >= 0.9: a11y_pts += 10; a11y_notes.append(f'{len(imgs_with_alt)}/{len(images)} images have alt ✓')
            elif ratio >= 0.6: a11y_pts += 6; a11y_notes.append(f'{len(imgs_with_alt)}/{len(images)} images have alt')
            else: a11y_pts += 2; a11y_notes.append(f'Only {len(imgs_with_alt)}/{len(images)} images have alt')
        else:
            a11y_pts += 5; a11y_notes.append('No images to evaluate')
        if skip_link: a11y_pts += 5; a11y_notes.append('Skip navigation ✓')
        a11y_pts = min(a11y_pts, 20)
        checks.append({'name': 'Accessibility (WCAG)', 'weight': 20, 'score': a11y_pts, 'max': 20,
                        'status': self._pts_status(a11y_pts, 20), 'detail': ' | '.join(a11y_notes)})
        score += a11y_pts; max_score += 20

        # --- Navigation (12pts) ---
        nav_tags = soup.find_all('nav')
        breadcrumb_html = bool(soup.find(class_=re.compile(r'breadcrumb', re.I)))
        nav_pts = 0; nav_notes = []
        if nav_tags: nav_pts += 7; nav_notes.append(f'{len(nav_tags)} <nav> element(s) ✓')
        else: nav_notes.append('No semantic <nav>')
        if breadcrumb_html: nav_pts += 5; nav_notes.append('Breadcrumbs ✓')
        nav_pts = min(nav_pts, 12)
        checks.append({'name': 'Navigation', 'weight': 12, 'score': nav_pts, 'max': 12,
                        'status': self._pts_status(nav_pts, 12), 'detail': ' | '.join(nav_notes)})
        score += nav_pts; max_score += 12

        # --- TTFB (15pts) ---
        rt = self.response_time
        if rt < 0.5:   pts, note = 15, f'Excellent TTFB: {rt:.2f}s'
        elif rt < 1.0:  pts, note = 12, f'Good TTFB: {rt:.2f}s'
        elif rt < 2.0:  pts, note = 8, f'Average: {rt:.2f}s'
        elif rt < 4.0:  pts, note = 4, f'Slow: {rt:.2f}s'
        else:           pts, note = 0, f'Critical: {rt:.2f}s'
        checks.append({'name': 'Page Speed (TTFB)', 'weight': 15, 'score': pts, 'max': 15,
                        'status': self._pts_status(pts, 15), 'detail': note})
        score += pts; max_score += 15

        # --- Font Loading (10pts) ---
        font_display = bool(re.search(r'font-display\s*:\s*swap', self.html))
        preload_font = bool(soup.find('link', rel='preload', attrs={'as': 'font'}))
        font_pts = 0; font_notes = []
        if font_display: font_pts += 5; font_notes.append('font-display: swap ✓')
        else: font_notes.append('No font-display: swap — risk of invisible text flash')
        if preload_font: font_pts += 5; font_notes.append('Font preload ✓')
        checks.append({'name': 'Font Loading', 'weight': 10, 'score': font_pts, 'max': 10,
                        'status': self._pts_status(font_pts, 10), 'detail': ' | '.join(font_notes)})
        score += font_pts; max_score += 10

        # --- Form Usability (8pts) ---
        forms = soup.find_all('form')
        labels = soup.find_all('label')
        autocomplete = [i for i in soup.find_all('input') if i.get('autocomplete')]
        form_pts = 0; form_notes = []
        if forms:
            if labels: form_pts += 4; form_notes.append(f'{len(labels)} labels ✓')
            else: form_notes.append('Forms without labels')
            if autocomplete: form_pts += 4; form_notes.append('Autocomplete ✓')
        else:
            form_pts = 4; form_notes.append('No forms on page')
        form_pts = min(form_pts, 8)
        checks.append({'name': 'Form Usability', 'weight': 8, 'score': form_pts, 'max': 8,
                        'status': self._pts_status(form_pts, 8), 'detail': ' | '.join(form_notes)})
        score += form_pts; max_score += 8

        # --- Image Optimization (10pts) ---
        imgs = soup.find_all('img')
        with_dims = [i for i in imgs if i.get('width') and i.get('height')]
        lazy = [i for i in imgs if i.get('loading') == 'lazy']
        img_pts = 0; img_notes = []
        if imgs:
            dim_ratio = len(with_dims) / len(imgs)
            if dim_ratio >= 0.8: img_pts += 5; img_notes.append(f'{len(with_dims)}/{len(imgs)} images have dimensions ✓')
            elif dim_ratio >= 0.4: img_pts += 3; img_notes.append(f'{len(with_dims)}/{len(imgs)} images have dimensions')
            else: img_notes.append('Most images missing width/height — causes CLS')
            if lazy: img_pts += 5; img_notes.append(f'{len(lazy)} images lazy-loaded ✓')
            else: img_notes.append('No lazy loading detected')
        else:
            img_pts = 5; img_notes.append('No images on page')
        img_pts = min(img_pts, 10)
        checks.append({'name': 'Image Optimization', 'weight': 10, 'score': img_pts, 'max': 10,
                        'status': self._pts_status(img_pts, 10), 'detail': ' | '.join(img_notes)})
        score += img_pts; max_score += 10

        # --- Custom 404 (10pts) --- skipped for now, would need async fetch
        # We skip this to avoid adding another HTTP request — can be added later

        pct = round(score / max_score * 100) if max_score else 0
        return {
            'name': 'User Experience',
            'icon': 'touch_app',
            'color': '#22d3ee',
            'score': pct,
            'grade': self._score_to_grade(pct),
            'checks': checks,
            'summary': self._ux_summary(pct),
        }

    def _ux_summary(self, score):
        if score >= 80: return 'Excellent user experience. Mobile-ready, accessible, and well-structured.'
        if score >= 60: return 'Good UX foundations with some areas for improvement.'
        if score >= 40: return 'User experience has gaps that are driving visitors away.'
        return 'Poor UX across multiple dimensions. Visitors are struggling to use this site.'
```

- [ ] **Step 3: Add `_analyze_content_quality` method**

```python
    async def _analyze_content_quality(self) -> Dict:
        """Content Quality pillar for general mode."""
        soup = self.soup
        checks = []
        score = 0
        max_score = 0
        text = soup.get_text()
        word_count = len(re.findall(r'\w+', text))

        # --- Content Depth (20pts) ---
        if word_count >= 1000: pts, note = 20, f'{word_count:,} words — excellent depth'
        elif word_count >= 500: pts, note = 14, f'{word_count:,} words — decent depth'
        elif word_count >= 250: pts, note = 7, f'{word_count:,} words — thin content'
        else: pts, note = 0, f'Only {word_count:,} words — too thin'
        checks.append({'name': 'Content Depth', 'weight': 20, 'score': pts, 'max': 20,
                        'status': self._pts_status(pts, 20), 'detail': note})
        score += pts; max_score += 20

        # --- Heading Structure (15pts) ---
        h2s = len(soup.find_all('h2'))
        h3s = len(soup.find_all('h3'))
        hd_pts = 0
        if h2s >= 4: hd_pts += 10
        elif h2s >= 2: hd_pts += 6
        if h3s >= 2: hd_pts += 5
        elif h3s >= 1: hd_pts += 3
        hd_pts = min(hd_pts, 15)
        checks.append({'name': 'Heading Structure', 'weight': 15, 'score': hd_pts, 'max': 15,
                        'status': self._pts_status(hd_pts, 15),
                        'detail': f'{h2s} H2s, {h3s} H3s'})
        score += hd_pts; max_score += 15

        # --- Image Alt Text (15pts) ---
        images = soup.find_all('img')
        imgs_with_alt = [i for i in images if i.get('alt') is not None]
        if images:
            ratio = len(imgs_with_alt) / len(images)
            if ratio >= 0.9: pts = 15
            elif ratio >= 0.6: pts = 9
            else: pts = 3
            note = f'{len(imgs_with_alt)}/{len(images)} images have alt text'
        else:
            pts = 10; note = 'No images to evaluate'
        checks.append({'name': 'Image Alt Text', 'weight': 15, 'score': pts, 'max': 15,
                        'status': self._pts_status(pts, 15), 'detail': note})
        score += pts; max_score += 15

        # --- Image Dimensions (10pts) ---
        with_dims = [i for i in images if i.get('width') and i.get('height')]
        if images:
            ratio = len(with_dims) / len(images)
            pts = round(10 * min(ratio / 0.8, 1))
            note = f'{len(with_dims)}/{len(images)} images have explicit dimensions'
        else:
            pts = 10; note = 'No images'
        checks.append({'name': 'Image Dimensions', 'weight': 10, 'score': pts, 'max': 10,
                        'status': self._pts_status(pts, 10), 'detail': note})
        score += pts; max_score += 10

        # --- Structured Content (10pts) ---
        lists = soup.find_all(['ul', 'ol'])
        tables = soup.find_all('table')
        sc_pts = 0; sc_notes = []
        if lists: sc_pts += 5; sc_notes.append(f'{len(lists)} list(s) ✓')
        if tables: sc_pts += 5; sc_notes.append(f'{len(tables)} table(s) ✓')
        if not lists and not tables: sc_notes.append('No lists or tables — add structured content')
        sc_pts = min(sc_pts, 10)
        checks.append({'name': 'Structured Content', 'weight': 10, 'score': sc_pts, 'max': 10,
                        'status': self._pts_status(sc_pts, 10), 'detail': ' | '.join(sc_notes)})
        score += sc_pts; max_score += 10

        # --- Internal Linking (15pts) ---
        domain = self.parsed.netloc
        internal_links = [a for a in soup.find_all('a', href=True)
                          if a.get('href', '').startswith('/') or domain in a.get('href', '')]
        if len(internal_links) >= 5: pts, note = 15, f'{len(internal_links)} internal links ✓'
        elif len(internal_links) >= 3: pts, note = 10, f'{len(internal_links)} internal links'
        elif len(internal_links) >= 1: pts, note = 5, f'{len(internal_links)} internal link(s) — add more'
        else: pts, note = 0, 'No internal links detected'
        checks.append({'name': 'Internal Linking', 'weight': 15, 'score': pts, 'max': 15,
                        'status': self._pts_status(pts, 15), 'detail': note})
        score += pts; max_score += 15

        # --- External Links (5pts) ---
        external_links = [a for a in soup.find_all('a', href=True)
                          if a.get('href', '').startswith('http') and domain not in a.get('href', '')]
        pts = 5 if external_links else 0
        note = f'{len(external_links)} outbound link(s) ✓' if external_links else 'No outbound links'
        checks.append({'name': 'External Links', 'weight': 5, 'score': pts, 'max': 5,
                        'status': self._pts_status(pts, 5), 'detail': note})
        score += pts; max_score += 5

        # --- FAQ Content (10pts) ---
        schema_types = self._get_schema_types()
        has_faq_schema = 'FAQPage' in schema_types
        has_faq_html = bool(soup.find(class_=re.compile(r'faq|accordion', re.I))) or \
                       bool(soup.find(lambda t: t.name in ['h2', 'h3'] and
                            re.search(r'faq|frequen|question', t.get_text(), re.I)))
        faq_pts = 0
        if has_faq_schema: faq_pts += 7
        if has_faq_html: faq_pts += 3
        faq_pts = min(faq_pts, 10)
        note = 'FAQ content detected ✓' if faq_pts else 'No FAQ section — consider adding one'
        checks.append({'name': 'FAQ Content', 'weight': 10, 'score': faq_pts, 'max': 10,
                        'status': self._pts_status(faq_pts, 10), 'detail': note})
        score += faq_pts; max_score += 10

        pct = round(score / max_score * 100) if max_score else 0
        return {
            'name': 'Content Quality',
            'icon': 'article',
            'color': '#f59e0b',
            'score': pct,
            'grade': self._score_to_grade(pct),
            'checks': checks,
            'summary': self._content_summary(pct),
        }

    def _content_summary(self, score):
        if score >= 80: return 'Excellent content quality. Well-structured, deep, and properly optimised.'
        if score >= 60: return 'Good content foundations with room for improvement in structure and depth.'
        if score >= 40: return 'Content quality is below standard. Add depth, structure, and alt text.'
        return 'Critical content issues. Thin, unstructured content is hurting SEO and engagement.'
```

- [ ] **Step 4: Commit**

```bash
git add grader.py
git commit -m "feat: add Security, UX, and Content Quality pillars for general mode"
```

---

### Task 7: Grader — General Mode Technical & Conversion Adjustments

**Files:**
- Modify: `grader.py` — `_analyze_technical` and `_analyze_conversion`

- [ ] **Step 1: Update `_analyze_technical` for general mode extras**

Add HTTP/2 detection and analytics detection for general mode. Add these checks at the end of the existing method, before the return statement. Replace the full method:

```python
    async def _analyze_technical(self) -> Dict:
        soup = self.soup
        checks = []
        score = 0
        max_score = 0

        # --- HTTPS ---
        https_max = 15 if self.mode == 'general' else 20
        is_https = self.url.startswith('https://')
        pts = https_max if is_https else 0
        note = 'HTTPS secure ✓' if is_https else 'Not HTTPS — Google ranks HTTPS pages higher'
        checks.append({'name': 'HTTPS / SSL', 'weight': https_max, 'score': pts, 'max': https_max,
                        'status': self._pts_status(pts, https_max), 'detail': note})
        score += pts; max_score += https_max

        # --- Server Response Time ---
        ttfb_max = 20 if self.mode == 'general' else 25
        rt = self.response_time
        if rt < 0.5:   pts, note = ttfb_max, f'Excellent TTFB: {rt:.2f}s — top 10%'
        elif rt < 1.0:  pts, note = round(ttfb_max * 0.8), f'Good TTFB: {rt:.2f}s'
        elif rt < 2.0:  pts, note = round(ttfb_max * 0.56), f'Average: {rt:.2f}s — target <1s'
        elif rt < 4.0:  pts, note = round(ttfb_max * 0.28), f'Slow: {rt:.2f}s'
        else:           pts, note = round(ttfb_max * 0.08), f'Critical: {rt:.2f}s'
        checks.append({'name': 'Server Response (TTFB)', 'weight': ttfb_max, 'score': pts, 'max': ttfb_max,
                        'status': self._pts_status(pts, ttfb_max), 'detail': note})
        score += pts; max_score += ttfb_max

        # --- Compression ---
        encoding = self.headers.get('content-encoding', '')
        if 'br' in encoding:
            pts, note = 15, 'Brotli compression enabled — best available'
        elif 'gzip' in encoding:
            pts, note = 12, 'Gzip compression enabled ✓'
        else:
            pts, note = 0, 'No compression — enable gzip/Brotli to reduce transfer size'
        checks.append({'name': 'Content Compression', 'weight': 15, 'score': pts, 'max': 15,
                        'status': self._pts_status(pts, 15), 'detail': note})
        score += pts; max_score += 15

        # --- Caching ---
        cache_max = 12 if self.mode == 'general' else 15
        cache_ctrl = self.headers.get('cache-control', '')
        etag = self.headers.get('etag', '')
        last_mod = self.headers.get('last-modified', '')
        cache_pts = 0; cache_notes = []
        if cache_ctrl:
            cache_pts += round(cache_max * 0.53); cache_notes.append(f'Cache-Control: {cache_ctrl[:50]}')
        else:
            cache_notes.append('No Cache-Control header')
        if etag or last_mod:
            cache_pts += round(cache_max * 0.47); cache_notes.append('Conditional caching ✓')
        cache_pts = min(cache_pts, cache_max)
        checks.append({'name': 'Caching Strategy', 'weight': cache_max, 'score': cache_pts, 'max': cache_max,
                        'status': self._pts_status(cache_pts, cache_max), 'detail': ' | '.join(cache_notes)})
        score += cache_pts; max_score += cache_max

        # --- Resource Hints ---
        hint_max = 8 if self.mode == 'general' else 10
        preload = soup.find_all('link', rel='preload')
        preconnect = soup.find_all('link', rel='preconnect')
        hint_pts = 0; hint_notes = []
        if preconnect: hint_pts += round(hint_max * 0.5); hint_notes.append(f'{len(preconnect)} preconnect ✓')
        if preload: hint_pts += round(hint_max * 0.5); hint_notes.append(f'{len(preload)} preload ✓')
        if not preconnect and not preload: hint_notes.append('No resource hints')
        hint_pts = min(hint_pts, hint_max)
        checks.append({'name': 'Resource Hints', 'weight': hint_max, 'score': hint_pts, 'max': hint_max,
                        'status': self._pts_status(hint_pts, hint_max), 'detail': ' | '.join(hint_notes)})
        score += hint_pts; max_score += hint_max

        # --- Render-Blocking ---
        blocking_js = [s for s in soup.find_all('script', src=True)
                       if not s.get('defer') and not s.get('async') and s.get('type') != 'module']
        block_max = 10 if self.mode == 'general' else 15
        if len(blocking_js) == 0: pts, note = block_max, 'No render-blocking scripts ✓'
        elif len(blocking_js) <= 2: pts, note = round(block_max * 0.67), f'{len(blocking_js)} blocking script(s)'
        elif len(blocking_js) <= 5: pts, note = round(block_max * 0.33), f'{len(blocking_js)} blocking scripts'
        else: pts, note = 0, f'{len(blocking_js)} blocking scripts — critical'
        checks.append({'name': 'Render-Blocking Scripts', 'weight': block_max, 'score': pts, 'max': block_max,
                        'status': self._pts_status(pts, block_max), 'detail': note})
        score += pts; max_score += block_max

        # --- General mode extras ---
        if self.mode == 'general':
            # HTTP/2+ (10pts)
            # We can't reliably detect HTTP/2 from aiohttp response, so check for h2 push hints
            # Use server header as a proxy
            server = self.headers.get('server', '').lower()
            alt_svc = self.headers.get('alt-svc', '')
            h2_detected = bool(alt_svc) or 'h2' in server or 'nginx' in server or 'cloudflare' in server
            h2_pts = 10 if h2_detected else 0
            h2_note = 'Modern server/HTTP2+ indicators detected ✓' if h2_detected else 'No HTTP/2 indicators found'
            checks.append({'name': 'HTTP/2+', 'weight': 10, 'score': h2_pts, 'max': 10,
                            'status': self._pts_status(h2_pts, 10), 'detail': h2_note,
                            'value': alt_svc[:60] if alt_svc else None})
            score += h2_pts; max_score += 10

            # Analytics Detection (10pts)
            html_lower = self.html.lower()
            analytics = []
            for name, pattern in [
                ('Google Analytics', r'gtag|google-analytics|googletagmanager|ga\.js|analytics\.js'),
                ('GTM', r'googletagmanager\.com/gtm'),
                ('Plausible', r'plausible\.io'),
                ('Fathom', r'usefathom\.com|cdn\.usefathom'),
                ('Matomo', r'matomo|piwik'),
                ('Hotjar', r'hotjar\.com|static\.hotjar'),
                ('Mixpanel', r'mixpanel\.com|cdn\.mxpnl'),
                ('Segment', r'segment\.com|cdn\.segment'),
                ('Heap', r'heap\.io|heapanalytics'),
            ]:
                if re.search(pattern, html_lower):
                    analytics.append(name)
            an_pts = 10 if analytics else 0
            an_note = f'Analytics: {", ".join(analytics)} ✓' if analytics else 'No analytics detected — you have no visibility into visitor behaviour'
            checks.append({'name': 'Analytics & Tracking', 'weight': 10, 'score': an_pts, 'max': 10,
                            'status': self._pts_status(an_pts, 10), 'detail': an_note})
            score += an_pts; max_score += 10

        pct = round(score / max_score * 100) if max_score else 0
        return {
            'name': 'Technical Performance',
            'icon': 'bolt',
            'color': '#10b981',
            'score': pct,
            'grade': self._score_to_grade(pct),
            'checks': checks,
            'summary': self._tech_summary(pct),
        }
```

- [ ] **Step 2: Update `_analyze_conversion` for general mode**

Replace the CTA regex and job-specific checks for general mode:

```python
    async def _analyze_conversion(self) -> Dict:
        soup = self.soup
        checks = []
        score = 0
        max_score = 0
        page_text = soup.get_text().lower()
        html_lower = self.html.lower()

        # --- CTA Quality ---
        if self.mode == 'general':
            cta_re = re.compile(
                r'\b(get started|contact us|learn more|sign up|try free|request demo|'
                r'buy now|subscribe|download|book (a )?call|schedule|start free|'
                r'create account|get in touch|shop now|add to cart|free trial)\b', re.I)
        elif self.mode == 'career_site':
            cta_re = re.compile(
                r'\b(apply now|apply today|search jobs|find jobs|browse jobs|get started|'
                r'upload (your |a )?cv|submit (your |a )?resume|register|sign up|join us|'
                r'view jobs|explore jobs|explore roles|see open positions|join our team|'
                r'work with us|view opportunities|start (your )?job search|see (all )?jobs)\b', re.I)
        else:  # recruitment
            cta_re = re.compile(
                r'\b(apply now|apply today|search jobs|find jobs|browse jobs|get started|'
                r'upload (your |a )?cv|submit (your |a )?resume|register|sign up|join us|'
                r'view jobs|explore jobs|start (your )?job search|see (all )?jobs)\b', re.I)

        cta_els = soup.find_all(lambda t: t.name in ['a', 'button'] and cta_re.search(t.get_text()))
        if len(cta_els) >= 4: pts, note = 25, f'{len(cta_els)} strong CTAs ✓'
        elif len(cta_els) >= 2: pts, note = 18, f'{len(cta_els)} CTAs — add more throughout'
        elif len(cta_els) == 1: pts, note = 10, '1 CTA — not enough to guide visitors'
        else: pts, note = 0, 'No clear CTAs — visitors have no next step'
        checks.append({'name': 'Call-to-Action Strength', 'weight': 25, 'score': pts, 'max': 25,
                        'status': self._pts_status(pts, 25), 'detail': note})
        score += pts; max_score += 25

        # --- Lead Capture ---
        email_inputs = soup.find_all('input', type=re.compile(r'email', re.I))
        if self.mode == 'general':
            alert_sig = bool(re.search(r'subscribe|newsletter|notify|get notified|mailing list', page_text))
            lead_label = 'Lead Capture / Newsletter'
        else:
            alert_sig = bool(re.search(
                r'job alert|email alert|notify me|get notified|job match|saved search|subscribe', page_text))
            lead_label = 'Job Alerts & Lead Capture'
        alert_pts = 0; alert_notes = []
        if email_inputs: alert_pts += 12; alert_notes.append(f'{len(email_inputs)} email capture(s) ✓')
        if alert_sig: alert_pts += 8; alert_notes.append('Subscription/alert detected ✓')
        else:
            if self.mode == 'general':
                alert_notes.append('No newsletter/subscribe — missing lead capture opportunity')
            else:
                alert_notes.append('No job alert feature — critical for re-engaging passive candidates')
        alert_pts = min(alert_pts, 20)
        checks.append({'name': lead_label, 'weight': 20, 'score': alert_pts, 'max': 20,
                        'status': self._pts_status(alert_pts, 20), 'detail': ' | '.join(alert_notes)})
        score += alert_pts; max_score += 20

        # --- Live Chat / Chatbot ---
        chat_sig = re.search(
            r'intercom|drift|crisp|tawk|zendesk|livechat|tidio|freshchat|hubspot|'
            r'chatbot|live.?chat|chat.?widget|widget.?chat|genesys|qualified', html_lower)
        chat_pts = 20 if chat_sig else 0
        chat_note = 'Live chat / chatbot detected ✓' if chat_sig else 'No chat detected — missed engagement opportunity'
        checks.append({'name': 'Live Chat & Chatbot', 'weight': 20, 'score': chat_pts, 'max': 20,
                        'status': self._pts_status(chat_pts, 20), 'detail': chat_note})
        score += chat_pts; max_score += 20

        # --- Search (general) or Job Search (recruitment/career_site) ---
        if self.mode == 'general':
            search_box = soup.find('input', attrs={'type': 'search'}) or \
                         soup.find('input', placeholder=re.compile(r'search', re.I))
            srch_pts = 15 if search_box else 0
            srch_note = 'Site search detected ✓' if search_box else 'No search functionality'
            checks.append({'name': 'Search Functionality', 'weight': 15, 'score': srch_pts, 'max': 15,
                            'status': self._pts_status(srch_pts, 15), 'detail': srch_note})
            score += srch_pts; max_score += 15
        else:
            search_box = (soup.find('input', attrs={'type': 'search'}) or
                          soup.find('input', placeholder=re.compile(r'search|job|role|keyword', re.I)) or
                          soup.find('input', id=re.compile(r'search|job', re.I)))
            search_form = soup.find('form', id=re.compile(r'search', re.I))
            filter_els = soup.find_all(class_=re.compile(r'filter|facet|refine', re.I))
            srch_pts = 0; srch_notes = []
            if search_box or search_form:
                srch_pts += 15; srch_notes.append('Job search detected ✓')
            else:
                srch_notes.append('No job search — candidates expect instant search')
            if filter_els:
                srch_pts += 5; srch_notes.append(f'{len(filter_els)} filter element(s) ✓')
            srch_pts = min(srch_pts, 20)
            checks.append({'name': 'Job Search & Filters', 'weight': 20, 'score': srch_pts, 'max': 20,
                            'status': self._pts_status(srch_pts, 20), 'detail': ' | '.join(srch_notes)})
            score += srch_pts; max_score += 20

        # --- Social Sharing ---
        if self.mode == 'general':
            social_links = soup.find_all('a', href=re.compile(r'linkedin|twitter|x\.com|facebook|instagram', re.I))
            share_els = soup.find_all(class_=re.compile(r'\bshare\b|social', re.I))
            soc_pts = 10 if (social_links or share_els) else 0
            soc_note = 'Social links/sharing detected ✓' if soc_pts else 'No social links or sharing'
            checks.append({'name': 'Social Links & Sharing', 'weight': 10, 'score': soc_pts, 'max': 10,
                            'status': self._pts_status(soc_pts, 10), 'detail': soc_note})
            score += soc_pts; max_score += 10

            # Analytics (10pts for general conversion)
            analytics_detected = bool(re.search(
                r'gtag|google-analytics|googletagmanager|plausible|fathom|matomo|hotjar|mixpanel', html_lower))
            an_pts = 10 if analytics_detected else 0
            an_note = 'Analytics/tracking present ✓' if analytics_detected else 'No analytics — no conversion tracking'
            checks.append({'name': 'Analytics & Tracking', 'weight': 10, 'score': an_pts, 'max': 10,
                            'status': self._pts_status(an_pts, 10), 'detail': an_note})
            score += an_pts; max_score += 10
        else:
            share_els = soup.find_all(class_=re.compile(r'\bshare\b|social-share', re.I))
            share_sig = bool(re.search(r'share.*job|refer.*friend|share.*role', html_lower))
            share_pts = 15 if (share_els or share_sig) else 0
            share_note = 'Social sharing detected ✓' if (share_els or share_sig) else \
                         'No social sharing — candidates cannot easily refer friends'
            checks.append({'name': 'Social Sharing & Referrals', 'weight': 15, 'score': share_pts, 'max': 15,
                            'status': self._pts_status(share_pts, 15), 'detail': share_note})
            score += share_pts; max_score += 15

        pct = round(score / max_score * 100) if max_score else 0
        return {
            'name': 'Conversion & Engagement',
            'icon': 'trending_up',
            'color': '#f43f5e',
            'score': pct,
            'grade': self._score_to_grade(pct),
            'checks': checks,
            'summary': self._conversion_summary(pct),
        }
```

- [ ] **Step 3: Commit**

```bash
git add grader.py
git commit -m "feat: mode-specific technical and conversion pillars with analytics/HTTP2 detection"
```

---

### Task 8: Grader — Mode-Specific Recommendations and Shazamme Advantage

**Files:**
- Modify: `grader.py` — `_generate_recommendations` and `_generate_shazamme_advantage`

- [ ] **Step 1: Update `_generate_recommendations` with mode-specific impacts**

Replace the IMPACT dict section to be mode-aware:

```python
    def _generate_recommendations(self, pillars: Dict) -> List[Dict]:
        IMPACT_RECRUITMENT = {
            'Title Tag': 'Directly controls click-through rate from search results',
            'Meta Description': 'Google uses this in SERPs — affects CTR by up to 30%',
            'Schema / Structured Data': 'Enables rich snippets — up to 3× higher CTR from search',
            'FAQ & Q&A Schema': 'Surfaces your answers directly in Google and AI search engines',
            'AI Crawler Access': 'Required for ChatGPT, Perplexity, and Claude to surface your jobs',
            'llms.txt File': 'Instructs AI models which content to use from your site',
            'llm-info File': 'Gives AI models structured data about your brand and services',
            'Mobile Readiness': '60%+ of job searches happen on mobile — this is table stakes',
            'Apply Flow & Job Search': 'Every missing apply button = lost applications',
            'Job Alerts & Lead Capture': 'Most candidates are passive — alerts re-engage them automatically',
            'Live Chat & Chatbot': 'Reduces application abandonment by up to 40%',
            'Video Content': 'Increases application intent by 34% and time-on-page significantly',
            'EVP & Pay Transparency': '67% of candidates research compensation before applying',
            'Social Proof & Reviews': 'Candidates check reviews before deciding — it builds trust',
            'Server Response (TTFB)': 'Each second of delay reduces conversions by 7%',
            'HTTPS / SSL': 'Non-HTTPS sites show security warnings and rank lower in Google',
            'Industry & Sector Pages': 'Sector pages capture 60-80% of recruiter organic traffic',
            'Content Depth': 'Thin pages rank poorly and give AI engines nothing to work with',
        }
        IMPACT_CAREER = {
            'Title Tag': 'Top talent judges your company within seconds of seeing search results',
            'Meta Description': 'Controls how your careers page appears in Google — affects talent CTR by 30%',
            'Schema / Structured Data': 'JobPosting schema gets your roles into Google Jobs — critical for career sites',
            'ATS Platform Detection': 'A properly integrated ATS ensures candidates can actually complete applications',
            'Culture & Team Content': 'Top talent wants to see the real culture before applying',
            'Employee Stories & Testimonials': 'Authentic employee voices are the #1 factor in career site credibility',
            'EVP & Pay Transparency': '67% of candidates research compensation before applying — transparency wins',
            'DE&I Commitment': 'Diverse candidates actively seek out DE&I commitment before applying',
            'Mobile Readiness': '60%+ of career site visits come from mobile',
            'Apply Flow & Job Search': 'Complex application forms cause 60% of candidates to abandon',
            'Video Content': 'Video on career sites increases application intent by 34%',
            'Live Chat & Chatbot': 'Chatbots reduce career site bounce by up to 40%',
        }
        IMPACT_GENERAL = {
            'Title Tag': 'Controls click-through rate from search results',
            'Meta Description': 'Directly affects how your page appears in Google — up to 30% CTR impact',
            'Schema / Structured Data': 'Enables rich snippets and better AI understanding',
            'Sitemap.xml': 'Search engines need this to discover and crawl all your pages',
            'Strict-Transport-Security': 'Without HSTS, browsers can be downgraded to insecure HTTP',
            'Content-Security-Policy': 'Without CSP, your site is vulnerable to XSS injection attacks',
            'Privacy & Cookie Policy': 'Required by GDPR/CCPA — missing this is a legal risk',
            'Mobile Readiness': '60%+ of web traffic is mobile — this is table stakes',
            'Accessibility (WCAG)': 'Accessibility failures exclude users and create legal liability',
            'Content Depth': 'Thin content ranks poorly and provides no value to visitors',
            'Analytics & Tracking': 'Without analytics you have zero visibility into what works',
            'Server Response (TTFB)': 'Each second of delay reduces conversions by 7%',
            'HTTPS / SSL': 'Non-HTTPS sites show warnings and rank lower in Google',
            'Search Functionality': 'Visitors who use search convert at 2-3× higher rates',
        }

        if self.mode == 'career_site':
            IMPACT = IMPACT_CAREER
        elif self.mode == 'general':
            IMPACT = IMPACT_GENERAL
        else:
            IMPACT = IMPACT_RECRUITMENT

        recs = []
        for pillar_data in pillars.values():
            for check in pillar_data.get('checks', []):
                if check['status'] in ('fail', 'warn'):
                    recs.append({
                        'priority': 'critical' if check['status'] == 'fail' else 'high',
                        'pillar': pillar_data['name'],
                        'pillar_color': pillar_data.get('color', '#6366f1'),
                        'check': check['name'],
                        'detail': check['detail'],
                        'impact': IMPACT.get(check['name'], 'Affects overall site performance'),
                    })
        recs.sort(key=lambda x: 0 if x['priority'] == 'critical' else 1)
        return recs[:15]
```

- [ ] **Step 2: Update `_generate_shazamme_advantage` for career_site mode**

Add career_site-specific feature framing. Replace the method:

```python
    def _generate_shazamme_advantage(self, pillars: Dict) -> List[Dict]:
        """Only called for recruitment and career_site modes."""
        all_checks: Dict[str, str] = {}
        for pd in pillars.values():
            for check in pd.get('checks', []):
                all_checks[check['name']] = check['status']

        features = []

        if all_checks.get('Schema / Structured Data') != 'pass':
            features.append({
                'gap': 'Missing or weak schema markup',
                'feature': 'Auto Schema Engine',
                'description': 'Auto-generates JobPosting, Organization, FAQPage, and BreadcrumbList schema on every page — zero config.',
                'icon': 'label',
                'stat': '3× higher CTR from rich snippets',
            })

        if all_checks.get('Apply Flow & Job Search') != 'pass':
            if self.mode == 'career_site':
                features.append({
                    'gap': 'Application flow needs improvement',
                    'feature': 'Streamlined Career Application Flow',
                    'description': 'Mobile-first application experience with ATS integration, smart forms, and one-click apply from any device.',
                    'icon': 'search', 'stat': 'Up to 60% more completed applications',
                })
            else:
                features.append({
                    'gap': 'No job search or weak apply flow',
                    'feature': 'AI-Powered Job Search & One-Click Apply',
                    'description': 'Instant AI-matched job search, smart filters, and a streamlined mobile-first apply flow — all built in.',
                    'icon': 'search', 'stat': 'Up to 60% more completed applications',
                })

        if all_checks.get('Job Alerts & Lead Capture') != 'pass' and all_checks.get('Lead Capture / Newsletter') != 'pass':
            features.append({
                'gap': 'No job alerts or lead capture',
                'feature': 'Intelligent Job Alert Engine',
                'description': 'Candidates subscribe to AI-matched alerts. Re-engages them the moment a relevant role is posted.',
                'icon': 'notifications', 'stat': 'Re-engages 4× more passive candidates',
            })

        if all_checks.get('Live Chat & Chatbot') != 'pass':
            features.append({
                'gap': 'No chatbot or live engagement',
                'feature': 'AI Recruitment Chatbot' if self.mode == 'recruitment' else 'AI Career Site Chatbot',
                'description': 'Built-in AI chatbot qualifies candidates 24/7, answers FAQs, and guides applicants through the process.',
                'icon': 'smart_toy', 'stat': 'Reduces drop-off by up to 40%',
            })

        if all_checks.get('AI Crawler Access') != 'pass' or not self.has_llms_txt or not self.llm_info_url:
            features.append({
                'gap': 'Invisible to AI search engines',
                'feature': 'GEO-Ready Out of the Box',
                'description': 'Ships with llms.txt, llm-info, FAQPage schema, and AI-optimised architecture — visible in ChatGPT, Perplexity, Claude.',
                'icon': 'auto_awesome', 'stat': 'Ranks in AI-generated search answers',
            })

        if all_checks.get('Mobile Readiness') != 'pass':
            features.append({
                'gap': 'Poor mobile experience',
                'feature': 'Mobile-First Architecture',
                'description': 'Built mobile-first with lightning performance — 90+ Google PageSpeed without configuration.',
                'icon': 'smartphone', 'stat': '95+ Google Mobile Speed Score',
            })

        if all_checks.get('Video Content') != 'pass':
            features.append({
                'gap': 'No employer brand video',
                'feature': 'Employer Brand Content Studio',
                'description': 'Drag-and-drop content blocks for team videos, culture storytelling, and employee testimonials.',
                'icon': 'videocam', 'stat': '+34% application intent with video',
            })

        if all_checks.get('EVP & Pay Transparency') != 'pass':
            features.append({
                'gap': 'Weak EVP and pay transparency',
                'feature': 'EVP & Transparency Framework',
                'description': 'Templates for salary ranges, benefits, flexibility, and DE&I — attract higher-quality candidates.',
                'icon': 'workspace_premium', 'stat': '2× candidate quality with transparent EVP',
            })

        if self.mode == 'recruitment' and all_checks.get('Industry & Sector Pages') != 'pass':
            features.append({
                'gap': 'Missing sector/industry pages',
                'feature': 'Sector Page Generator',
                'description': "Auto-generates SEO-optimised sector pages for every industry you recruit in with live job feeds.",
                'icon': 'category', 'stat': '60-80% of organic traffic from sector keywords',
            })

        if all_checks.get('Server Response (TTFB)') != 'pass':
            features.append({
                'gap': 'Slow server response times',
                'feature': 'Global Edge CDN',
                'description': 'Delivered from a global edge network with automatic image optimisation for sub-second load times.',
                'icon': 'bolt', 'stat': '<0.5s TTFB on Shazamme sites',
            })

        return features[:6]
```

- [ ] **Step 3: Commit**

```bash
git add grader.py
git commit -m "feat: mode-specific recommendations and Shazamme advantage"
```

---

### Task 9: Frontend — Mode Selector and UI Updates

**Files:**
- Modify: `public/index.html`

- [ ] **Step 1: Add mode selector CSS** (after the `.example-links a:hover` rule ~line 191)

```css
/* ── Mode Selector ── */
.mode-selector {
  display: flex;
  gap: 12px;
  justify-content: center;
  margin-bottom: 32px;
  max-width: 680px;
  width: 100%;
}
.mode-card {
  flex: 1;
  background: rgba(255,255,255,0.03);
  border: 1.5px solid var(--border2);
  border-radius: 14px;
  padding: 18px 14px;
  cursor: pointer;
  text-align: center;
  transition: border-color 0.2s, background 0.2s, transform 0.15s;
}
.mode-card:hover {
  border-color: rgba(139,92,246,0.3);
  background: rgba(139,92,246,0.05);
  transform: translateY(-2px);
}
.mode-card.selected {
  border-color: var(--purple);
  background: rgba(139,92,246,0.1);
  box-shadow: 0 0 0 3px rgba(139,92,246,0.15);
}
.mode-card-icon {
  font-size: 28px;
  margin-bottom: 8px;
  display: block;
}
.mode-card-label {
  font-size: 14px;
  font-weight: 700;
  margin-bottom: 4px;
}
.mode-card-sub {
  font-size: 11px;
  color: var(--text3);
  line-height: 1.3;
}
.mode-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: rgba(139,92,246,0.12);
  border: 1px solid rgba(139,92,246,0.3);
  border-radius: 100px;
  padding: 3px 10px;
  font-size: 11px;
  font-weight: 600;
  color: var(--purple-l);
  margin-left: 8px;
}

@media (max-width: 640px) {
  .mode-selector { flex-direction: column; gap: 8px; }
  .mode-card { padding: 14px 12px; }
}
```

- [ ] **Step 2: Add mode selector HTML** (replace the form section in hero, lines 780-804)

```html
  <form class="grade-form" id="grade-form" onsubmit="startGrading(event)">
    <div class="mode-selector" id="mode-selector">
      <div class="mode-card" data-mode="recruitment" onclick="selectMode('recruitment')">
        <span class="mode-card-icon material-icons-round" style="color:var(--cyan)">business</span>
        <div class="mode-card-label">Recruitment Website</div>
        <div class="mode-card-sub">Staffing agencies & job boards</div>
      </div>
      <div class="mode-card" data-mode="career_site" onclick="selectMode('career_site')">
        <span class="mode-card-icon material-icons-round" style="color:var(--purple-l)">work</span>
        <div class="mode-card-label">Career Site</div>
        <div class="mode-card-sub">Corporate careers & employer brand</div>
      </div>
      <div class="mode-card" data-mode="general" onclick="selectMode('general')">
        <span class="mode-card-icon material-icons-round" style="color:var(--green)">language</span>
        <div class="mode-card-label">Website</div>
        <div class="mode-card-sub">Any website — full quality audit</div>
      </div>
    </div>
    <div class="input-wrap">
      <span class="url-icon"><span class="material-icons-round">language</span></span>
      <input
        type="text"
        id="url-input"
        placeholder="https://yourwebsite.com"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
        aria-label="Website URL"
      >
      <button type="submit" id="grade-btn" disabled>
        <span class="material-icons-round">insights</span>
        Grade My Site
      </button>
    </div>
    <p class="example-links" id="example-links">
      Select a mode above to get started
    </p>
  </form>
```

- [ ] **Step 3: Update JS — add mode state and selectMode function** (in the STATE section ~line 969)

```javascript
let gradeData = null;
let selectedMode = null;

function selectMode(mode) {
  selectedMode = mode;
  document.querySelectorAll('.mode-card').forEach(c => c.classList.remove('selected'));
  document.querySelector(`.mode-card[data-mode="${mode}"]`).classList.add('selected');
  $('grade-btn').disabled = false;

  // Update examples per mode
  const examples = {
    recruitment: 'Try: <a href="#" onclick="fillUrl(\'https://www.hays.com/en_GB/candidates\');return false">hays.com</a> <a href="#" onclick="fillUrl(\'https://www.michaelpage.com.au\');return false">michaelpage.com</a> <a href="#" onclick="fillUrl(\'https://www.randstad.com\');return false">randstad.com</a>',
    career_site: 'Try: <a href="#" onclick="fillUrl(\'https://careers.google.com\');return false">careers.google.com</a> <a href="#" onclick="fillUrl(\'https://www.atlassian.com/company/careers\');return false">atlassian.com</a> <a href="#" onclick="fillUrl(\'https://jobs.netflix.com\');return false">netflix.com</a>',
    general: 'Try: <a href="#" onclick="fillUrl(\'https://stripe.com\');return false">stripe.com</a> <a href="#" onclick="fillUrl(\'https://www.shopify.com\');return false">shopify.com</a> <a href="#" onclick="fillUrl(\'https://www.notion.so\');return false">notion.so</a>',
  };
  $('example-links').innerHTML = examples[mode] || '';

  // Update placeholder
  const placeholders = {
    recruitment: 'https://www.recruitmentsite.com',
    career_site: 'https://careers.yourcompany.com',
    general: 'https://www.yourwebsite.com',
  };
  $('url-input').placeholder = placeholders[mode] || '';
}
```

- [ ] **Step 4: Update `startGrading` to pass mode** (line 1051)

Replace this line:
```javascript
  const evtSource = new EventSource('/grade?url=' + encodeURIComponent(url));
```
With:
```javascript
  if (!selectedMode) { alert('Please select a grading mode first.'); return; }
  const evtSource = new EventSource('/grade?url=' + encodeURIComponent(url) + '&mode=' + selectedMode);
```

- [ ] **Step 5: Update `renderResults` to show mode badge** (in the header rendering, ~line 1131)

Replace:
```javascript
  $('res-domain').textContent = data.domain;
```
With:
```javascript
  const modeLabels = { recruitment: 'Recruitment', career_site: 'Career Site', general: 'Website' };
  $('res-domain').innerHTML = escHtml(data.domain) +
    (data.mode ? ` <span class="mode-badge">${escHtml(modeLabels[data.mode] || data.mode)}</span>` : '');
```

- [ ] **Step 6: Update `renderResults` to hide Shazamme for general mode** (replace shazamme block ~line 1161)

Replace:
```javascript
  if (data.shazamme_advantage && data.shazamme_advantage.length) {
    renderShazamme(data.shazamme_advantage);
  } else {
    $('shazamme-section').style.display = 'none';
  }
```
With:
```javascript
  if (data.mode !== 'general' && data.shazamme_advantage && data.shazamme_advantage.length) {
    $('shazamme-section').style.display = '';
    renderShazamme(data.shazamme_advantage);
  } else {
    $('shazamme-section').style.display = 'none';
  }
```

- [ ] **Step 7: Update `renderPillars` ORDER to be dynamic** (replace ORDER ~line 1173)

Replace:
```javascript
  const ORDER = ['seo','geo','cx','brand','technical','conversion'];
```
With:
```javascript
  const ORDER = selectedMode === 'general'
    ? ['seo','security','ux','content','technical','conversion']
    : ['seo','geo','cx','brand','technical','conversion'];
```

- [ ] **Step 8: Update `copyLink` to include mode** (line 1304)

Replace:
```javascript
  const shareUrl = window.location.origin + '/?url=' + encodeURIComponent(url);
```
With:
```javascript
  const shareUrl = window.location.origin + '/?url=' + encodeURIComponent(url) + (selectedMode ? '&mode=' + selectedMode : '');
```

- [ ] **Step 9: Update auto-load to handle mode param** (replace the auto-load IIFE ~lines 1318-1326)

```javascript
(function() {
  const params = new URLSearchParams(window.location.search);
  const urlParam = params.get('url');
  const modeParam = params.get('mode');
  if (modeParam && ['recruitment', 'career_site', 'general'].includes(modeParam)) {
    selectMode(modeParam);
  }
  if (urlParam) {
    $('url-input').value = urlParam;
    if (selectedMode) {
      setTimeout(() => startGrading({ preventDefault: () => {} }), 300);
    }
  }
})();
```

- [ ] **Step 10: Update `gradeAnother` to reset mode**

Replace:
```javascript
function gradeAnother() {
  hide('loading');
  hide('results');
  show('hero');
  document.querySelector('.site-footer').style.display = '';
  $('url-input').value = '';
  $('url-input').focus();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
```
With:
```javascript
function gradeAnother() {
  hide('loading');
  hide('results');
  show('hero');
  document.querySelector('.site-footer').style.display = '';
  $('url-input').value = '';
  // Keep mode selected so user can quickly grade another site in same mode
  $('url-input').focus();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
```

- [ ] **Step 11: Update hero title and subtitle to be mode-agnostic**

Replace:
```html
  <h1 class="hero-title">
    Is your career site<br>
    <span class="grad">actually winning talent?</span>
  </h1>

  <p class="hero-sub">
    The most comprehensive career site grader on the planet. Analyse SEO, AI visibility, candidate experience, employer brand, technical performance, and conversion — in seconds.
  </p>
```
With:
```html
  <h1 class="hero-title">
    Is your website<br>
    <span class="grad">actually performing?</span>
  </h1>

  <p class="hero-sub">
    The most comprehensive website grader on the planet. Analyse SEO, AI visibility, performance, security, and conversion — in seconds.
  </p>
```

- [ ] **Step 12: Update page title and meta**

Replace:
```html
<title>Career Site Grader — Powered by Shazamme</title>
<meta name="description" content="The most comprehensive career site and staffing website grader. Analyse SEO, AI visibility, candidate experience, employer brand, and conversion in seconds.">
```
With:
```html
<title>Website Grader — Powered by Shazamme</title>
<meta name="description" content="The most comprehensive website grader. Grade recruitment sites, career pages, or any website across SEO, AI visibility, performance, security, and conversion.">
```

- [ ] **Step 13: Update footer**

Replace:
```html
    Career Site Grader v2.0 &nbsp;·&nbsp;
    Covering SEO · GEO · CX · Employer Brand · Performance · Conversion
```
With:
```html
    Website Grader v3.0 &nbsp;·&nbsp;
    Recruitment · Career Sites · Any Website
```

- [ ] **Step 14: Commit**

```bash
git add public/index.html
git commit -m "feat: add mode selector UI, dynamic pillars, mode badge, and updated branding"
```

---

### Task 10: Smoke Test and Final Verification

- [ ] **Step 1: Start the server locally**

```bash
cd /Users/rickmare/Desktop/ClaudeCode/career-site-grader
python server.py &
```

- [ ] **Step 2: Test recruitment mode**

```bash
curl -s "http://localhost:7070/grade?url=https://www.hays.com&mode=recruitment" | head -5
```

Expected: SSE events streaming, pillar_complete events for seo, geo, cx, brand, technical, conversion.

- [ ] **Step 3: Test career_site mode**

```bash
curl -s "http://localhost:7070/grade?url=https://careers.google.com&mode=career_site" | head -5
```

Expected: SSE events streaming with career_site-specific checks (ATS detection, culture content).

- [ ] **Step 4: Test general mode**

```bash
curl -s "http://localhost:7070/grade?url=https://stripe.com&mode=general" | head -5
```

Expected: SSE events with security, ux, content pillars instead of geo/cx/brand.

- [ ] **Step 5: Test invalid mode returns 400**

```bash
curl -s "http://localhost:7070/grade?url=https://example.com&mode=invalid"
```

Expected: `{"error": "mode parameter required. Must be one of: recruitment, career_site, general"}`

- [ ] **Step 6: Test missing mode returns 400**

```bash
curl -s "http://localhost:7070/grade?url=https://example.com"
```

Expected: Same 400 error.

- [ ] **Step 7: Stop the server and commit**

```bash
kill %1 2>/dev/null
git add -A
git commit -m "chore: verify all three modes work correctly"
```
