import asyncio
import aiohttp
import time
import json
import re
import ssl
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from typing import AsyncGenerator, Dict, Any, List, Optional


class CareerSiteGrader:

    HEADERS = {
        'User-Agent': (
            'ShazammeGrader/2.0 (Career Site Intelligence; contact@shazamme.com) '
            'Mozilla/5.0 (compatible; Googlebot/2.1)'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
    }

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
        self.llm_info_url: Optional[str] = None   # path that returned 200 for llm-info
        self.has_sitemap = False
        self.sitemap_url: Optional[str] = None
        self.errors: List[str] = []

    def _normalize_url(self, url: str) -> str:
        url = url.strip()
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        return url

    async def grade(self) -> AsyncGenerator[Dict, None]:
        yield {'type': 'status', 'message': 'Connecting to site...', 'progress': 5}

        success = await self._fetch_main_page()
        if not success:
            err_detail = self.errors[-1] if self.errors else 'Unknown error'
            yield {
                'type': 'error',
                'message': f'Could not analyse the site: {err_detail}',
            }
            return

        yield {
            'type': 'status',
            'message': f'Connected! Page size: {len(self.html):,} bytes — running deep analysis...',
            'progress': 12,
        }

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

    # -------------------------------------------------------------------------
    # Fetching
    # -------------------------------------------------------------------------

    async def _fetch_main_page(self) -> bool:
        try:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            timeout = aiohttp.ClientTimeout(total=45, sock_read=30)
            start = time.time()
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
                async with sess.get(self.url, headers=self.HEADERS, allow_redirects=True) as resp:
                    self.response_time = time.time() - start
                    self.status_code = resp.status
                    self.headers = {k.lower(): v for k, v in resp.headers.items()}
                    self.html = await resp.text(encoding='utf-8', errors='replace')
                    self.url = str(resp.url)
                    self.parsed = urlparse(self.url)
                    self.base_url = f"{self.parsed.scheme}://{self.parsed.netloc}"
            self.soup = BeautifulSoup(self.html, 'html.parser')
            return True
        except Exception as e:
            self.errors.append(str(e))
            return False

    async def _fetch_robots_txt(self):
        try:
            url = urljoin(self.base_url, '/robots.txt')
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=self.HEADERS, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status == 200:
                        self.robots_txt = await resp.text()
        except Exception:
            pass

    async def _check_llms_txt(self):
        try:
            url = urljoin(self.base_url, '/llms.txt')
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=self.HEADERS, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    self.has_llms_txt = resp.status == 200
        except Exception:
            pass

    async def _check_llm_info(self):
        """Check for llm-info at common paths — a structured machine-readable file
        that tells AI models who you are, what you do, and how to represent you."""
        candidates = [
            '/.well-known/llm-info',
            '/llm-info',
            '/llm-info.json',
            '/llm-info.txt',
        ]
        try:
            timeout = aiohttp.ClientTimeout(total=6)
            async with aiohttp.ClientSession() as sess:
                for path in candidates:
                    url = urljoin(self.base_url, path)
                    try:
                        async with sess.get(url, headers=self.HEADERS, timeout=timeout) as resp:
                            if resp.status == 200:
                                self.llm_info_url = path
                                return
                    except Exception:
                        continue
        except Exception:
            pass

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

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _score_to_grade(self, score: float) -> str:
        if score >= 93: return 'A+'
        if score >= 85: return 'A'
        if score >= 75: return 'B+'
        if score >= 65: return 'B'
        if score >= 55: return 'C+'
        if score >= 45: return 'C'
        if score >= 35: return 'D'
        return 'F'

    def _grade_label(self, score: float) -> str:
        if score >= 93: return 'World Class'
        if score >= 85: return 'Excellent'
        if score >= 75: return 'Good'
        if score >= 65: return 'Average'
        if score >= 55: return 'Below Average'
        if score >= 45: return 'Poor'
        return 'Critical'

    def _pts_status(self, pts: float, max_pts: float) -> str:
        if max_pts == 0:
            return 'pass'
        ratio = pts / max_pts
        if ratio >= 0.8:
            return 'pass'
        if ratio >= 0.45:
            return 'warn'
        return 'fail'

    def _get_schema_types(self) -> List[str]:
        if not self.soup:
            return []
        types: List[str] = []
        for script in self.soup.find_all('script', type='application/ld+json'):
            try:
                raw = script.string or ''
                data = json.loads(raw)
                self._extract_types(data, types)
            except Exception:
                pass
        return types

    def _extract_types(self, data, types: List[str]):
        if isinstance(data, dict):
            t = data.get('@type', '')
            if isinstance(t, list):
                types.extend(t)
            elif t:
                types.append(t)
            for item in data.get('@graph', []):
                self._extract_types(item, types)
        elif isinstance(data, list):
            for item in data:
                self._extract_types(item, types)

    # -------------------------------------------------------------------------
    # Pillar: SEO & Discoverability
    # -------------------------------------------------------------------------

    async def _analyze_seo(self) -> Dict:
        soup = self.soup
        checks = []
        score = 0
        max_score = 0

        # --- Title ---
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

        # --- Meta Description ---
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

        # --- H1 ---
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
            has_job_schema = False
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

        # --- Open Graph ---
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
        else: og_notes.append('No og:image — poor appearance on social/LinkedIn shares')
        if og_type:   og_pts += 2; og_notes.append('og:type ✓')
        checks.append({'name': 'Open Graph / Social Tags', 'weight': 13, 'score': og_pts, 'max': 13,
                        'status': self._pts_status(og_pts, 13), 'detail': ', '.join(og_notes)})
        score += og_pts; max_score += 13

        # --- Canonical ---
        canonical = soup.find('link', rel='canonical')
        if canonical:
            pts, note = 8, f'Canonical: {(canonical.get("href") or "")[:80]}'
        else:
            pts, note = 0, 'No canonical tag — duplicate content risk'
        checks.append({'name': 'Canonical URL', 'weight': 8, 'score': pts, 'max': 8,
                        'status': self._pts_status(pts, 8), 'detail': note})
        score += pts; max_score += 8

        # --- Indexability ---
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

    def _check_sector_pages(self, soup: BeautifulSoup) -> Dict:
        """
        Check whether the site has dedicated industry/sector landing pages that
        target high-value keyword combinations like '[sector] recruitment' and
        '[sector] jobs'.  We look at:
          1. Internal links whose href or anchor text matches the pattern
          2. Navigation menu items
          3. The current page's own headings and body text (catches sector homepages)
        """
        SECTORS = [
            'accounting', 'finance', 'financial', 'banking', 'insurance',
            'technology', 'tech', r'\bit\b', 'software', 'digital', 'data',
            'engineering', 'construction', 'infrastructure', 'civil',
            'healthcare', 'health care', 'medical', 'nursing', 'clinical',
            'pharmaceutical', 'pharma', 'life sciences',
            'legal', 'law',
            'marketing', 'creative', 'design', 'media', 'communications',
            r'\bhr\b', 'human resources', 'people.?&.?culture',
            'manufacturing', 'industrial', 'operations',
            'retail', r'\bfmcg\b', 'consumer goods',
            'sales', 'business development', 'commercial',
            'education', 'teaching',
            'administration', 'admin', 'office support', 'business support',
            'executive', 'leadership', r'\bc.suite\b', 'board',
            'logistics', 'supply chain', 'procurement', 'warehousing',
            'mining', 'resources', 'energy', 'oil.?&.?gas',
            'property', 'real estate', 'facilities',
            'hospitality', 'tourism', 'events',
            'science', 'research', 'environment',
            'government', 'public sector',
            r'not.for.profit', r'\bnfp\b', 'charity', 'ngo',
            'agriculture', 'agribusiness',
        ]
        JOB_TERMS = re.compile(
            r'\b(jobs?|recruitment|recruiting|staffing|talent|careers?|'
            r'hiring|positions?|roles?|vacancies|vacanci|placements?|opportunities)\b',
            re.I
        )
        SECTOR_RE = re.compile('|'.join(SECTORS), re.I)

        # Collect all internal link texts + hrefs
        domain = self.parsed.netloc
        sector_links: List[str] = []       # hrefs of matched links
        sector_anchors: List[str] = []     # anchor texts of matched links
        sector_nav_items: List[str] = []   # nav items that match

        for a in soup.find_all('a', href=True):
            href = (a.get('href') or '').strip()
            text = a.get_text(separator=' ').strip()

            # Only internal links
            is_internal = (
                href.startswith('/') or
                href.startswith('#') or
                (domain and domain in href)
            )
            if not is_internal:
                continue

            href_lower = href.lower()
            text_lower = text.lower()
            combined = f"{href_lower} {text_lower}"

            has_sector  = bool(SECTOR_RE.search(combined))
            has_job_term = bool(JOB_TERMS.search(combined))

            if has_sector and has_job_term:
                sector_links.append(href[:80])
                sector_anchors.append(text[:60])
                # Check if it sits inside a nav element
                if a.find_parent('nav') or a.find_parent(class_=re.compile(r'nav|menu|header', re.I)):
                    sector_nav_items.append(text[:60])

        # De-duplicate by href
        unique_sector_hrefs = list(dict.fromkeys(sector_links))

        # Also scan headings on the *current* page
        page_sector_headings = []
        for tag in soup.find_all(['h1', 'h2', 'h3']):
            text = tag.get_text().strip()
            if SECTOR_RE.search(text) and JOB_TERMS.search(text):
                page_sector_headings.append(text[:80])

        # Score
        num_pages = len(unique_sector_hrefs)
        num_nav   = len(set(sector_nav_items))

        notes = []
        if num_pages >= 8:
            pts = 20
            notes.append(f'{num_pages} sector/industry pages linked ✓')
        elif num_pages >= 4:
            pts = 16
            notes.append(f'{num_pages} sector/industry pages linked — aim for 8+')
        elif num_pages >= 2:
            pts = 11
            notes.append(f'{num_pages} sector/industry pages found — needs expansion')
        elif num_pages == 1:
            pts = 6
            notes.append('Only 1 sector page found — major gap')
        else:
            pts = 0
            notes.append('No sector/industry pages detected (e.g. "accounting jobs", "IT recruitment")')

        if num_nav > 0:
            pts = min(pts + 3, 20)
            notes.append(f'{num_nav} sector link(s) in navigation ✓')

        if page_sector_headings:
            pts = min(pts + 2, 20)
            notes.append(f'Sector headings on this page: "{page_sector_headings[0]}"')

        # Build value preview
        sample = [a for a in sector_anchors[:4] if a.strip()]
        value = ', '.join(f'"{s}"' for s in sample) if sample else None

        return {
            'name': 'Industry & Sector Pages',
            'weight': 20,
            'score': pts,
            'max': 20,
            'status': self._pts_status(pts, 20),
            'detail': ' | '.join(notes) if notes else 'No sector-specific pages found',
            'value': value,
        }

    def _seo_summary(self, score, has_schema, has_job):
        if score >= 80:
            return 'Strong SEO foundations. Your site is well-positioned to rank for talent searches.'
        if score >= 60:
            return 'Decent SEO basics, but structured data and rich snippets need work to compete.'
        if score >= 40:
            return 'Several SEO gaps are limiting your organic visibility. Priority fixes needed now.'
        return 'Critical SEO issues are preventing this site from appearing in Google talent searches.'

    # -------------------------------------------------------------------------
    # Pillar: GEO & AI Visibility
    # -------------------------------------------------------------------------

    async def _analyze_geo(self) -> Dict:
        soup = self.soup
        schema_types = self._get_schema_types()
        checks = []
        score = 0
        max_score = 0

        # --- AI Crawler Access ---
        gpt_blocked = False
        claude_blocked = False
        current_agent = None
        if self.robots_txt:
            for line in self.robots_txt.lower().splitlines():
                line = line.strip()
                if line.startswith('user-agent:'):
                    current_agent = line.split(':', 1)[1].strip()
                elif line.startswith('disallow:') and line.split(':', 1)[1].strip() in ('/', '/*'):
                    if current_agent in ('gptbot', 'chatgpt-user', 'openai'):
                        gpt_blocked = True
                    if current_agent in ('claudebot', 'anthropic-ai', 'claude-web'):
                        claude_blocked = True

        ai_pts = 0; ai_notes = []
        if self.robots_txt:
            ai_pts += 5; ai_notes.append('robots.txt found ✓')
        else:
            ai_notes.append('No robots.txt found')
        if not gpt_blocked:
            ai_pts += 8; ai_notes.append('GPTBot can crawl ✓')
        else:
            ai_notes.append('GPTBot BLOCKED — invisible to ChatGPT browsing & indexing')
        if not claude_blocked:
            ai_pts += 7; ai_notes.append('ClaudeBot can crawl ✓')
        else:
            ai_notes.append('ClaudeBot BLOCKED')
        checks.append({'name': 'AI Crawler Access', 'weight': 20, 'score': ai_pts, 'max': 20,
                        'status': self._pts_status(ai_pts, 20), 'detail': ' | '.join(ai_notes)})
        score += ai_pts; max_score += 20

        # --- llms.txt ---
        if self.has_llms_txt:
            pts, note = 10, 'llms.txt present — outstanding AI readiness signal'
        else:
            pts, note = 0, 'No llms.txt — add one to guide AI models on your content'
        checks.append({'name': 'llms.txt File', 'weight': 10, 'score': pts, 'max': 10,
                        'status': self._pts_status(pts, 10), 'detail': note})
        score += pts; max_score += 10

        # --- llm-info ---
        if self.llm_info_url:
            pts  = 10
            note = f'llm-info found at {self.llm_info_url} — AI models can read structured brand/service data ✓'
        else:
            pts  = 0
            note = ('No llm-info file found (checked /.well-known/llm-info, /llm-info, '
                    '/llm-info.json, /llm-info.txt) — add one so AI models accurately '
                    'represent your services, sectors, and locations')
        checks.append({'name': 'llm-info File', 'weight': 10, 'score': pts, 'max': 10,
                        'status': self._pts_status(pts, 10), 'detail': note,
                        'value': self.llm_info_url or None})
        score += pts; max_score += 10

        # --- FAQ Schema ---
        has_faq_schema = 'FAQPage' in schema_types
        faq_html = bool(soup.find(class_=re.compile(r'faq|accordion|q.?a', re.I)))
        faq_heading = bool(soup.find(lambda t: t.name in ['h2', 'h3'] and
                                      re.search(r'faq|frequen|question', t.get_text(), re.I)))
        faq_pts = 0; faq_notes = []
        if has_faq_schema:
            faq_pts += 15; faq_notes.append('FAQPage schema ✓ — eligible for Google FAQ rich results')
        else:
            faq_notes.append('No FAQPage schema — AI engines love structured Q&A to surface in answers')
        if faq_html or faq_heading:
            faq_pts += 5; faq_notes.append('FAQ content detected in HTML ✓')
        faq_pts = min(faq_pts, 20)
        checks.append({'name': 'FAQ & Q&A Schema', 'weight': 20, 'score': faq_pts, 'max': 20,
                        'status': self._pts_status(faq_pts, 20), 'detail': ' | '.join(faq_notes)})
        score += faq_pts; max_score += 20

        # --- Content Structure for AI ---
        h2_count = len(soup.find_all('h2'))
        h3_count = len(soup.find_all('h3'))
        list_count = len(soup.find_all(['ul', 'ol']))
        struct_pts = 0; struct_notes = []
        if h2_count >= 4:
            struct_pts += 8; struct_notes.append(f'{h2_count} H2 sections — excellent structure ✓')
        elif h2_count >= 2:
            struct_pts += 5; struct_notes.append(f'{h2_count} H2 sections')
        else:
            struct_notes.append('Few subheadings — AI cannot parse content topics')
        if list_count >= 3:
            struct_pts += 7; struct_notes.append(f'{list_count} lists — scannable content ✓')
        elif list_count >= 1:
            struct_pts += 4; struct_notes.append(f'{list_count} list(s)')
        else:
            struct_notes.append('No lists — add bullet points for AI readability')
        struct_pts = min(struct_pts, 15)
        checks.append({'name': 'Content Structure', 'weight': 15, 'score': struct_pts, 'max': 15,
                        'status': self._pts_status(struct_pts, 15), 'detail': ' | '.join(struct_notes)})
        score += struct_pts; max_score += 15

        # --- Entity & Authority Signals ---
        has_org  = any(t in ['Organization', 'Corporation', 'EmploymentAgency',
                              'StaffingAgency', 'RecruitmentAgency'] for t in schema_types)
        has_contact = 'ContactPoint' in schema_types
        has_person  = 'Person' in schema_types
        ent_pts = 0; ent_notes = []
        if has_org:
            ent_pts += 10; ent_notes.append('Organization entity defined ✓')
        else:
            ent_notes.append('No Organization entity — AI cannot reliably identify your brand')
        if has_contact:
            ent_pts += 5; ent_notes.append('ContactPoint in schema ✓')
        if has_person:
            ent_pts += 5; ent_notes.append('Person entities defined ✓')
        ent_pts = min(ent_pts, 20)
        checks.append({'name': 'Entity & Authority', 'weight': 20, 'score': ent_pts, 'max': 20,
                        'status': self._pts_status(ent_pts, 20),
                        'detail': ' | '.join(ent_notes) if ent_notes else 'No entity signals detected'})
        score += ent_pts; max_score += 20

        # --- Content Depth ---
        word_count = len(re.findall(r'\w+', soup.get_text()))
        if word_count >= 1000:
            depth_pts, depth_note = 15, f'{word_count:,} words — excellent content depth for AI'
        elif word_count >= 500:
            depth_pts, depth_note = 10, f'{word_count:,} words — decent depth'
        elif word_count >= 250:
            depth_pts, depth_note = 5, f'{word_count:,} words — thin content'
        else:
            depth_pts, depth_note = 0, f'Only {word_count:,} words — too thin for AI to answer questions about you'
        checks.append({'name': 'Content Depth', 'weight': 15, 'score': depth_pts, 'max': 15,
                        'status': self._pts_status(depth_pts, 15), 'detail': depth_note})
        score += depth_pts; max_score += 15

        pct = round(score / max_score * 100) if max_score else 0
        return {
            'name': 'GEO & AI Visibility',
            'icon': 'auto_awesome',
            'color': '#8b5cf6',
            'score': pct,
            'grade': self._score_to_grade(pct),
            'checks': checks,
            'summary': self._geo_summary(pct),
        }

    def _geo_summary(self, score):
        if score >= 80:
            return 'Excellent AI visibility. Your content surfaces effectively in ChatGPT, Perplexity, and AI search.'
        if score >= 60:
            return 'Good start on GEO, but AI-specific optimisations will significantly lift discoverability.'
        if score >= 40:
            return 'Your site is largely invisible to AI-powered search. GEO work is urgently needed.'
        return 'Critical GEO gaps. AI search engines cannot effectively understand or surface your content.'

    # -------------------------------------------------------------------------
    # Pillar: Candidate Experience
    # -------------------------------------------------------------------------

    async def _analyze_cx(self) -> Dict:
        soup = self.soup
        checks = []
        score = 0
        max_score = 0

        if self.mode == 'career_site':
            # --- Mobile Readiness (12pts) ---
            mobile_max = 12
            viewport = soup.find('meta', attrs={'name': re.compile(r'^viewport$', re.I)})
            if viewport:
                content = (viewport.get('content') or '').lower()
                if 'width=device-width' in content:
                    pts, note = mobile_max, 'Responsive viewport configured correctly ✓'
                else:
                    pts, note = 6, f'Viewport meta present but not optimal: {content[:60]}'
            else:
                pts, note = 0, 'No viewport meta — site will appear broken on mobile (60%+ of job searches)'
            checks.append({'name': 'Mobile Readiness', 'weight': mobile_max, 'score': pts, 'max': mobile_max,
                            'status': self._pts_status(pts, mobile_max), 'detail': note})
            score += pts; max_score += mobile_max

            # --- Accessibility (15pts) ---
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

            # --- Apply Flow (20pts max) ---
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

            # Count visible form fields (inputs/selects/textareas) in all forms
            all_field_inputs = soup.find_all(['input', 'select', 'textarea'])
            field_count = len([
                f for f in all_field_inputs
                if f.get('type', 'text') not in ('hidden', 'submit', 'button', 'reset', 'image')
            ])

            apply_pts = 0; apply_notes = []
            if len(apply_btns) >= 2:
                apply_pts += 10; apply_notes.append(f'{len(apply_btns)} Apply buttons ✓')
            elif apply_btns:
                apply_pts += 6; apply_notes.append(f'{len(apply_btns)} Apply button found')
            else:
                apply_notes.append('No Apply button detected — how do candidates apply?')

            if field_count <= 5:
                apply_pts += 5; apply_notes.append(f'{field_count} form fields — streamlined ✓')
            elif field_count <= 10:
                apply_pts += 3; apply_notes.append(f'{field_count} form fields — acceptable')
            elif field_count > 10:
                apply_notes.append(f'{field_count} form fields — too many, simplify the apply flow')

            if has_job_search:
                apply_pts += 5; apply_notes.append('Job search detected ✓')
            else:
                apply_notes.append('No job search — candidates cannot find relevant roles')

            apply_pts = min(apply_pts, 20)
            checks.append({'name': 'Apply Flow & Job Search', 'weight': 20, 'score': apply_pts, 'max': 20,
                            'status': self._pts_status(apply_pts, 20), 'detail': ' | '.join(apply_notes)})
            score += apply_pts; max_score += 20

            # --- ATS Platform Detection (15pts) ---
            ats_check = self._detect_ats()
            checks.append(ats_check)
            score += ats_check['score']; max_score += ats_check['max']

            # --- Navigation (10pts) ---
            nav_tags = soup.find_all('nav')
            breadcrumb_html = bool(soup.find(class_=re.compile(r'breadcrumb', re.I)))
            schema_types = self._get_schema_types()
            has_breadcrumb_schema = 'BreadcrumbList' in schema_types
            nav_pts = 0; nav_notes = []
            if nav_tags:
                nav_pts += 7; nav_notes.append(f'{len(nav_tags)} <nav> element(s) ✓')
            else:
                nav_notes.append('No semantic <nav> — navigation not machine-readable')
            if has_breadcrumb_schema or breadcrumb_html:
                nav_pts += 3; nav_notes.append('Breadcrumbs present ✓')
            checks.append({'name': 'Navigation & Structure', 'weight': 10, 'score': nav_pts, 'max': 10,
                            'status': self._pts_status(nav_pts, 10), 'detail': ' | '.join(nav_notes)})
            score += nav_pts; max_score += 10

            # --- HTTPS Trust Signal (8pts) ---
            is_https = self.url.startswith('https://')
            pts = 8 if is_https else 0
            note = 'HTTPS ✓ — candidates trust this site' if is_https else 'Not HTTPS — browsers show "Not Secure" warning to candidates'
            checks.append({'name': 'HTTPS Trust Signal', 'weight': 8, 'score': pts, 'max': 8,
                            'status': self._pts_status(pts, 8), 'detail': note})
            score += pts; max_score += 8

            # --- Page Speed (TTFB) (10pts) ---
            rt = self.response_time
            if rt < 0.5:
                pts, note = 10, f'Excellent TTFB: {rt:.2f}s'
            elif rt < 1.0:
                pts, note = 8, f'Good TTFB: {rt:.2f}s'
            elif rt < 2.0:
                pts, note = 5, f'Slow TTFB: {rt:.2f}s — investigate server performance'
            elif rt < 4.0:
                pts, note = 2, f'Very slow: {rt:.2f}s — 53% of users leave after 3s'
            else:
                pts, note = 0, f'Critical: {rt:.2f}s TTFB — site is painfully slow'
            checks.append({'name': 'Page Speed (TTFB)', 'weight': 10, 'score': pts, 'max': 10,
                            'status': self._pts_status(pts, 10), 'detail': note})
            score += pts; max_score += 10

            # --- Form Usability (10pts) ---
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

        else:
            # recruitment mode (and fallback) — original logic unchanged
            # --- Mobile Readiness ---
            viewport = soup.find('meta', attrs={'name': re.compile(r'^viewport$', re.I)})
            if viewport:
                content = (viewport.get('content') or '').lower()
                if 'width=device-width' in content:
                    pts, note = 15, 'Responsive viewport configured correctly ✓'
                else:
                    pts, note = 7, f'Viewport meta present but not optimal: {content[:60]}'
            else:
                pts, note = 0, 'No viewport meta — site will appear broken on mobile (60%+ of job searches)'
            checks.append({'name': 'Mobile Readiness', 'weight': 15, 'score': pts, 'max': 15,
                            'status': self._pts_status(pts, 15), 'detail': note})
            score += pts; max_score += 15

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
            checks.append({'name': 'Apply Flow & Job Search', 'weight': 25, 'score': apply_pts, 'max': 25,
                            'status': self._pts_status(apply_pts, 25), 'detail': ' | '.join(apply_notes)})
            score += apply_pts; max_score += 25

            # --- Navigation ---
            nav_tags = soup.find_all('nav')
            breadcrumb_html = bool(soup.find(class_=re.compile(r'breadcrumb', re.I)))
            schema_types = self._get_schema_types()
            has_breadcrumb_schema = 'BreadcrumbList' in schema_types
            nav_pts = 0; nav_notes = []
            if nav_tags:
                nav_pts += 10; nav_notes.append(f'{len(nav_tags)} <nav> element(s) ✓')
            else:
                nav_notes.append('No semantic <nav> — navigation not machine-readable')
            if has_breadcrumb_schema or breadcrumb_html:
                nav_pts += 5; nav_notes.append('Breadcrumbs present ✓')
            checks.append({'name': 'Navigation & Structure', 'weight': 15, 'score': nav_pts, 'max': 15,
                            'status': self._pts_status(nav_pts, 15), 'detail': ' | '.join(nav_notes)})
            score += nav_pts; max_score += 15

            # --- HTTPS Trust Signal ---
            is_https = self.url.startswith('https://')
            pts = 10 if is_https else 0
            note = 'HTTPS ✓ — candidates trust this site' if is_https else 'Not HTTPS — browsers show "Not Secure" warning to candidates'
            checks.append({'name': 'HTTPS Trust Signal', 'weight': 10, 'score': pts, 'max': 10,
                            'status': self._pts_status(pts, 10), 'detail': note})
            score += pts; max_score += 10

            # --- Page Speed (TTFB) ---
            rt = self.response_time
            if rt < 0.5:
                pts, note = 10, f'Excellent TTFB: {rt:.2f}s'
            elif rt < 1.0:
                pts, note = 8, f'Good TTFB: {rt:.2f}s'
            elif rt < 2.0:
                pts, note = 5, f'Slow TTFB: {rt:.2f}s — investigate server performance'
            elif rt < 4.0:
                pts, note = 2, f'Very slow: {rt:.2f}s — 53% of users leave after 3s'
            else:
                pts, note = 0, f'Critical: {rt:.2f}s TTFB — site is painfully slow'
            checks.append({'name': 'Page Speed (TTFB)', 'weight': 10, 'score': pts, 'max': 10,
                            'status': self._pts_status(pts, 10), 'detail': note})
            score += pts; max_score += 10

            # --- Form Usability ---
            forms = soup.find_all('form')
            inputs = soup.find_all('input')
            labels = soup.find_all('label')
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
        return {
            'name': 'Candidate Experience',
            'icon': 'person',
            'color': '#22d3ee',
            'score': pct,
            'grade': self._score_to_grade(pct),
            'checks': checks,
            'summary': self._cx_summary(pct),
        }

    def _detect_ats(self) -> Dict:
        """Detect ATS platforms from HTML content and iframe sources."""
        ATS_PATTERNS = {
            'Greenhouse':      r'greenhouse\.io|boards\.greenhouse|grnh\.se',
            'Lever':           r'lever\.co|jobs\.lever\.co',
            'Workday':         r'workday\.com|myworkdayjobs\.com|wd\d+\.myworkdayjobs',
            'Taleo':           r'taleo\.net|oracle.*taleo',
            'iCIMS':           r'icims\.com|careers-.*\.icims',
            'SmartRecruiters': r'smartrecruiters\.com|jobs\.smartrecruiters',
            'BambooHR':        r'bamboohr\.com',
            'Jobvite':         r'jobvite\.com|jobs\.jobvite',
            'Ashby':           r'ashbyhq\.com|jobs\.ashbyhq',
            'Breezy':          r'breezy\.hr',
            'Workable':        r'workable\.com|apply\.workable',
            'JazzHR':          r'jazzhr\.com|app\.jazz\.co',
            'Recruitee':       r'recruitee\.com',
            'Pinpoint':        r'pinpointhq\.com',
        }

        html_lower = self.html.lower()
        # Also gather iframe src values
        iframes = self.soup.find_all('iframe') if self.soup else []
        iframe_srcs = ' '.join((f.get('src', '') or '') for f in iframes).lower()
        combined = f'{html_lower} {iframe_srcs}'

        detected = []
        for name, pattern in ATS_PATTERNS.items():
            if re.search(pattern, combined, re.I):
                detected.append(name)

        if detected:
            pts = 15
            note = f'ATS detected: {", ".join(detected)} ✓ — integrated hiring technology confirmed'
        else:
            pts = 0
            note = 'No ATS platform detected — consider integrating an ATS for streamlined hiring'

        return {
            'name': 'ATS Platform Detection',
            'weight': 15,
            'score': pts,
            'max': 15,
            'status': self._pts_status(pts, 15),
            'detail': note,
            'value': ', '.join(detected) if detected else None,
        }

    def _cx_summary(self, score):
        if score >= 80:
            return 'Excellent candidate experience. Your site makes finding and applying for jobs frictionless.'
        if score >= 60:
            return 'Good experience with some friction points costing you applications.'
        if score >= 40:
            return "Candidate experience needs work — you're losing applicants at critical touchpoints."
        return "Poor candidate experience is costing you applications every day. Immediate action needed."

    # -------------------------------------------------------------------------
    # Pillar: Employer Brand & Content
    # -------------------------------------------------------------------------

    async def _analyze_brand(self) -> Dict:
        soup = self.soup
        checks = []
        score = 0
        max_score = 0
        page_text = soup.get_text().lower()
        html_lower = self.html.lower()

        if self.mode == 'career_site':
            # ----------------------------------------------------------------
            # career_site brand checks
            # ----------------------------------------------------------------

            # --- Culture & Team Content (20pts) ---
            culture_page_sig = bool(re.search(
                r'culture|our.?team|meet.?the.?team|about.?us|who.?we.?are', page_text, re.I))
            team_people_sig = bool(re.search(
                r'our.?people|the.?team|leadership|meet.?us', page_text, re.I))
            life_at_sig = bool(re.search(
                r'life.?at|working.?at|working.?here|day.?in.?the.?life|what.?it.?s.?like',
                page_text, re.I))
            culture_pts = 0; culture_notes = []
            if culture_page_sig:
                culture_pts += 10; culture_notes.append('Culture/team content detected ✓')
            else:
                culture_notes.append('No culture or team content — candidates want to see who they\'ll work with')
            if team_people_sig:
                culture_pts += 5; culture_notes.append('People/team content ✓')
            if life_at_sig:
                culture_pts += 5; culture_notes.append('"Life at" / working experience content ✓')
            culture_pts = min(culture_pts, 20)
            checks.append({'name': 'Culture & Team Content', 'weight': 20, 'score': culture_pts, 'max': 20,
                            'status': self._pts_status(culture_pts, 20), 'detail': ' | '.join(culture_notes)})
            score += culture_pts; max_score += 20

            # --- Employee Stories & Testimonials (15pts) ---
            story_els = soup.find_all(class_=re.compile(r'review|testimonial|story|quote|employee', re.I))
            has_employee_story = bool(re.search(
                r'testimonial|employee.?stor|our.?people.?say|what.?they.?say|hear.?from|their.?words',
                page_text, re.I))
            has_video_testimonial = bool(re.search(
                r'video.*testimonial|testimonial.*video|employee.*video', page_text, re.I))
            emp_pts = 0; emp_notes = []
            if story_els or has_employee_story:
                emp_pts += 10; emp_notes.append('Employee stories / testimonials detected ✓')
            else:
                emp_notes.append('No employee stories — candidates trust peer voices over corporate messaging')
            if has_video_testimonial:
                emp_pts += 5; emp_notes.append('Video testimonials detected ✓')
            emp_pts = min(emp_pts, 15)
            checks.append({'name': 'Employee Stories & Testimonials', 'weight': 15, 'score': emp_pts, 'max': 15,
                            'status': self._pts_status(emp_pts, 15), 'detail': ' | '.join(emp_notes)})
            score += emp_pts; max_score += 15

            # --- EVP & Pay Transparency (20pts) ---
            salary_sig  = bool(re.search(r'salary|pay|compens|remunerat|\$|£|€|aud\b|wage|earn', page_text, re.I))
            benefit_sig = bool(re.search(
                r'benefit|perk|health|dental|vision|401k|pension|super(annuat)?|pto|vacation|holiday|'
                r'\bremote\b|flexib|hybrid|parental|wellbeing|wellness', page_text, re.I))
            growth_sig = bool(re.search(
                r'career.?growth|career.?develop|career.?progress|learning.?&.?develop|'
                r'professional.?develop|training|mentor|promotion|career.?path', page_text, re.I))
            evp_pts = 0; evp_notes = []
            if salary_sig:
                evp_pts += 8; evp_notes.append('Salary/pay transparency ✓')
            else:
                evp_notes.append('No pay info — 67% of candidates want salary upfront')
            if benefit_sig:
                evp_pts += 7; evp_notes.append('Benefits/perks highlighted ✓')
            else:
                evp_notes.append('Benefits not prominently mentioned')
            if growth_sig:
                evp_pts += 5; evp_notes.append('Career growth / development content ✓')
            else:
                evp_notes.append('No career growth content — candidates want to see a future here')
            evp_pts = min(evp_pts, 20)
            checks.append({'name': 'EVP & Pay Transparency', 'weight': 20, 'score': evp_pts, 'max': 20,
                            'status': self._pts_status(evp_pts, 20), 'detail': ' | '.join(evp_notes)})
            score += evp_pts; max_score += 20

            # --- DE&I Commitment (12pts) ---
            dei_links = soup.find_all('a', href=re.compile(r'diversity|inclusion|dei|equity|belonging', re.I))
            has_dei_page = bool(dei_links)
            dei_content_sig = bool(re.search(
                r'\bdei\b|diversity|equity|inclusion|equal opportunit|eeo|belonging|erg\b|'
                r'accessible|disability|neurodiver', page_text, re.I))
            dei_pts = 0; dei_notes = []
            if has_dei_page:
                dei_pts += 8; dei_notes.append(f'Dedicated DE&I page linked ✓ ({dei_links[0].get("href", "")[:60]})')
            else:
                dei_notes.append('No dedicated DE&I page — link to a diversity/inclusion page')
            if dei_content_sig:
                dei_pts += 4; dei_notes.append('DE&I keywords in content ✓')
            else:
                dei_notes.append('No DE&I content — increasingly important for talent attraction')
            dei_pts = min(dei_pts, 12)
            checks.append({'name': 'DE&I Commitment', 'weight': 12, 'score': dei_pts, 'max': 12,
                            'status': self._pts_status(dei_pts, 12), 'detail': ' | '.join(dei_notes)})
            score += dei_pts; max_score += 12

            # --- Video Content (10pts) ---
            videos = soup.find_all('video')
            iframes = soup.find_all('iframe')
            yt_vimeo = [i for i in iframes if re.search(r'youtube|vimeo|loom|wistia', i.get('src', ''), re.I)]
            has_video = bool(videos) or bool(yt_vimeo)
            video_pts = 10 if has_video else 0
            video_note = (f'{len(videos) + len(yt_vimeo)} video element(s) — great for engagement ✓'
                          if has_video else 'No video — video content increases application intent by 34%')
            checks.append({'name': 'Video Content', 'weight': 10, 'score': video_pts, 'max': 10,
                            'status': self._pts_status(video_pts, 10), 'detail': video_note})
            score += video_pts; max_score += 10

            # --- Visual Brand Assets (8pts) ---
            og_img  = soup.find('meta', property='og:image')
            favicon = soup.find('link', rel=re.compile(r'icon', re.I))
            brand_pts = 0; brand_notes = []
            if og_img:
                brand_pts += 5; brand_notes.append('og:image set ✓ — great social sharing brand')
            else:
                brand_notes.append('No og:image — unbranded appearance on LinkedIn/WhatsApp shares')
            if favicon:
                brand_pts += 3; brand_notes.append('Favicon present ✓')
            else:
                brand_notes.append('No favicon — affects trust and brand recall')
            checks.append({'name': 'Visual Brand Assets', 'weight': 8, 'score': brand_pts, 'max': 8,
                            'status': self._pts_status(brand_pts, 8), 'detail': ' | '.join(brand_notes)})
            score += brand_pts; max_score += 8

            # --- Social Presence (15pts) ---
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

        else:
            # ----------------------------------------------------------------
            # recruitment mode — original logic unchanged
            # ----------------------------------------------------------------

            # --- Social Proof ---
            review_els = soup.find_all(class_=re.compile(r'review|testimonial|rating|quote', re.I))
            has_testimonial = bool(re.search(
                r'testimonial|what.{1,20}(say|think)|our clients say|candidates say|heard from|they said',
                page_text, re.I
            ))
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

            # --- Video Content ---
            videos = soup.find_all('video')
            iframes = soup.find_all('iframe')
            yt_vimeo = [i for i in iframes if re.search(r'youtube|vimeo|loom|wistia', i.get('src', ''), re.I)]
            has_video = bool(videos) or bool(yt_vimeo)
            video_pts = 15 if has_video else 0
            video_note = (f'{len(videos) + len(yt_vimeo)} video element(s) — great for engagement ✓'
                          if has_video else 'No video — video content increases application intent by 34%')
            checks.append({'name': 'Video Content', 'weight': 15, 'score': video_pts, 'max': 15,
                            'status': self._pts_status(video_pts, 15), 'detail': video_note})
            score += video_pts; max_score += 15

            # --- EVP Signals ---
            salary_sig  = bool(re.search(r'salary|pay|compens|remunerat|\$|£|€|aud\b|wage|earn', page_text, re.I))
            benefit_sig = bool(re.search(
                r'benefit|perk|health|dental|vision|401k|pension|super(annuat)?|pto|vacation|holiday|'
                r'\bremote\b|flexib|hybrid|parental|wellbeing|wellness', page_text, re.I))
            culture_sig = bool(re.search(
                r'culture|values?|mission|vision|diversity|inclus|belong|community|team spirit', page_text, re.I))
            evp_pts = 0; evp_notes = []
            if salary_sig:
                evp_pts += 10; evp_notes.append('Salary/pay transparency ✓')
            else:
                evp_notes.append('No pay info — 67% of candidates want salary upfront')
            if benefit_sig:
                evp_pts += 10; evp_notes.append('Benefits/perks highlighted ✓')
            else:
                evp_notes.append('Benefits not prominently mentioned')
            if culture_sig:
                evp_pts += 5; evp_notes.append('Culture/values content ✓')
            evp_pts = min(evp_pts, 25)
            checks.append({'name': 'EVP & Pay Transparency', 'weight': 25, 'score': evp_pts, 'max': 25,
                            'status': self._pts_status(evp_pts, 25), 'detail': ' | '.join(evp_notes)})
            score += evp_pts; max_score += 25

            # --- Visual Brand ---
            og_img   = soup.find('meta', property='og:image')
            favicon  = soup.find('link', rel=re.compile(r'icon', re.I))
            brand_pts = 0; brand_notes = []
            if og_img:
                brand_pts += 10; brand_notes.append('og:image set ✓ — great social sharing brand')
            else:
                brand_notes.append('No og:image — unbranded appearance on LinkedIn/WhatsApp shares')
            if favicon:
                brand_pts += 5; brand_notes.append('Favicon present ✓')
            else:
                brand_notes.append('No favicon — affects trust and brand recall')
            checks.append({'name': 'Visual Brand Assets', 'weight': 15, 'score': brand_pts, 'max': 15,
                            'status': self._pts_status(brand_pts, 15), 'detail': ' | '.join(brand_notes)})
            score += brand_pts; max_score += 15

            # --- Social Presence ---
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

            # --- DE&I ---
            dei_sig = bool(re.search(
                r'\bdei\b|diversity|equity|inclusion|equal opportunit|eeo|belonging|erg\b', page_text, re.I))
            dei_pts = 10 if dei_sig else 0
            dei_note = 'DE&I commitment visible ✓' if dei_sig else \
                       'No DE&I content — increasingly important for talent attraction'
            checks.append({'name': 'DE&I Commitment', 'weight': 10, 'score': dei_pts, 'max': 10,
                            'status': self._pts_status(dei_pts, 10), 'detail': dei_note})
            score += dei_pts; max_score += 10

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

    def _brand_summary(self, score):
        if score >= 80:
            return 'Compelling employer brand. Your content attracts, inspires, and converts the right talent.'
        if score >= 60:
            return 'Good brand foundations but EVP and social proof need strengthening to stand out.'
        if score >= 40:
            return 'Employer brand content is thin. Candidates struggle to see why they should choose you.'
        return "Very weak employer brand. You're competing for talent with nothing to differentiate you."

    # -------------------------------------------------------------------------
    # Pillar: Security & Trust  (general mode)
    # -------------------------------------------------------------------------

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

    # -------------------------------------------------------------------------
    # Pillar: User Experience  (general mode)
    # -------------------------------------------------------------------------

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

    # -------------------------------------------------------------------------
    # Pillar: Content Quality  (general mode)
    # -------------------------------------------------------------------------

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

    # -------------------------------------------------------------------------
    # Pillar: Technical Performance
    # -------------------------------------------------------------------------

    async def _analyze_technical(self) -> Dict:
        soup = self.soup
        checks = []
        score = 0
        max_score = 0

        # --- HTTPS ---
        is_https = self.url.startswith('https://')
        pts = 20 if is_https else 0
        note = 'HTTPS secure ✓' if is_https else 'Not HTTPS — Google ranks HTTPS pages higher'
        checks.append({'name': 'HTTPS / SSL', 'weight': 20, 'score': pts, 'max': 20,
                        'status': self._pts_status(pts, 20), 'detail': note})
        score += pts; max_score += 20

        # --- Server Response Time ---
        rt = self.response_time
        if rt < 0.5:
            pts, note = 25, f'Excellent TTFB: {rt:.2f}s — top 10% performance'
        elif rt < 1.0:
            pts, note = 20, f'Good TTFB: {rt:.2f}s'
        elif rt < 2.0:
            pts, note = 14, f'Average: {rt:.2f}s TTFB — target <1s for Core Web Vitals'
        elif rt < 4.0:
            pts, note = 7, f'Slow: {rt:.2f}s — investigate server/hosting performance'
        else:
            pts, note = 2, f'Critical: {rt:.2f}s — will fail Core Web Vitals'
        checks.append({'name': 'Server Response (TTFB)', 'weight': 25, 'score': pts, 'max': 25,
                        'status': self._pts_status(pts, 25), 'detail': note})
        score += pts; max_score += 25

        # --- Compression ---
        encoding = self.headers.get('content-encoding', '')
        if 'br' in encoding:
            pts, note = 15, 'Brotli compression enabled — best available'
        elif 'gzip' in encoding:
            pts, note = 12, 'Gzip compression enabled ✓'
        else:
            pts, note = 0, 'No compression detected — enable gzip/Brotli to reduce transfer size'
        checks.append({'name': 'Content Compression', 'weight': 15, 'score': pts, 'max': 15,
                        'status': self._pts_status(pts, 15), 'detail': note})
        score += pts; max_score += 15

        # --- Caching ---
        cache_ctrl = self.headers.get('cache-control', '')
        etag       = self.headers.get('etag', '')
        last_mod   = self.headers.get('last-modified', '')
        cache_pts = 0; cache_notes = []
        if cache_ctrl:
            cache_pts += 8; cache_notes.append(f'Cache-Control: {cache_ctrl[:50]}')
        else:
            cache_notes.append('No Cache-Control header')
        if etag or last_mod:
            cache_pts += 7; cache_notes.append('Conditional caching supported ✓')
        checks.append({'name': 'Caching Strategy', 'weight': 15, 'score': cache_pts, 'max': 15,
                        'status': self._pts_status(cache_pts, 15), 'detail': ' | '.join(cache_notes)})
        score += cache_pts; max_score += 15

        # --- Resource Hints ---
        preload    = soup.find_all('link', rel='preload')
        preconnect = soup.find_all('link', rel='preconnect')
        prefetch   = soup.find_all('link', rel='prefetch')
        hint_pts = 0; hint_notes = []
        if preconnect:
            hint_pts += 5; hint_notes.append(f'{len(preconnect)} preconnect hint(s) ✓')
        if preload:
            hint_pts += 5; hint_notes.append(f'{len(preload)} preload resource(s) ✓')
        if not preconnect and not preload:
            hint_notes.append('No resource hints — add preconnect for 3rd-party origins')
        hint_pts = min(hint_pts, 10)
        checks.append({'name': 'Resource Hints', 'weight': 10, 'score': hint_pts, 'max': 10,
                        'status': self._pts_status(hint_pts, 10),
                        'detail': ' | '.join(hint_notes) if hint_notes else 'None detected'})
        score += hint_pts; max_score += 10

        # --- Render-Blocking JS ---
        blocking_js = [
            s for s in soup.find_all('script', src=True)
            if not s.get('defer') and not s.get('async') and s.get('type') != 'module'
        ]
        if len(blocking_js) == 0:
            pts, note = 15, 'No render-blocking scripts ✓'
        elif len(blocking_js) <= 2:
            pts, note = 10, f'{len(blocking_js)} render-blocking script(s) — add defer/async'
        elif len(blocking_js) <= 5:
            pts, note = 5, f'{len(blocking_js)} render-blocking scripts — significant performance impact'
        else:
            pts, note = 0, f'{len(blocking_js)} render-blocking scripts — critical performance issue'
        checks.append({'name': 'Render-Blocking Scripts', 'weight': 15, 'score': pts, 'max': 15,
                        'status': self._pts_status(pts, 15), 'detail': note})
        score += pts; max_score += 15

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

    def _tech_summary(self, score):
        if score >= 80:
            return 'Excellent technical foundations. Fast, secure, well-compressed and properly cached.'
        if score >= 60:
            return 'Good technical setup with some optimisation opportunities.'
        if score >= 40:
            return 'Technical performance gaps are hurting Core Web Vitals and candidate experience.'
        return 'Significant technical issues. Poor performance is driving candidates away before they even land.'

    # -------------------------------------------------------------------------
    # Pillar: Conversion & Engagement
    # -------------------------------------------------------------------------

    async def _analyze_conversion(self) -> Dict:
        soup = self.soup
        checks = []
        score = 0
        max_score = 0
        page_text = soup.get_text().lower()
        html_lower = self.html.lower()

        # --- CTA Quality ---
        cta_re = re.compile(
            r'\b(apply now|apply today|search jobs|find jobs|browse jobs|get started|'
            r'upload (your |a )?cv|submit (your |a )?resume|register|sign up|join us|'
            r'view jobs|explore jobs|start (your )?job search|see (all )?jobs)\b', re.I
        )
        cta_els = soup.find_all(lambda t: t.name in ['a', 'button'] and cta_re.search(t.get_text()))
        if len(cta_els) >= 4:
            pts, note = 25, f'{len(cta_els)} strong CTAs detected ✓'
        elif len(cta_els) >= 2:
            pts, note = 18, f'{len(cta_els)} CTAs found — add more throughout'
        elif len(cta_els) == 1:
            pts, note = 10, '1 CTA found — not enough to guide candidates'
        else:
            pts, note = 0, 'No clear CTAs — candidates have no next step'
        checks.append({'name': 'Call-to-Action Strength', 'weight': 25, 'score': pts, 'max': 25,
                        'status': self._pts_status(pts, 25), 'detail': note})
        score += pts; max_score += 25

        # --- Job Alerts / Email Capture ---
        email_inputs = soup.find_all('input', type=re.compile(r'email', re.I))
        alert_sig    = bool(re.search(
            r'job alert|email alert|notify me|get notified|job match|saved search|subscribe', page_text, re.I))
        alert_pts = 0; alert_notes = []
        if email_inputs:
            alert_pts += 12; alert_notes.append(f'{len(email_inputs)} email capture(s) ✓')
        if alert_sig:
            alert_pts += 8; alert_notes.append('Job alert / subscription detected ✓')
        else:
            alert_notes.append('No job alert feature — critical for re-engaging passive candidates')
        alert_pts = min(alert_pts, 20)
        checks.append({'name': 'Job Alerts & Lead Capture', 'weight': 20, 'score': alert_pts, 'max': 20,
                        'status': self._pts_status(alert_pts, 20), 'detail': ' | '.join(alert_notes)})
        score += alert_pts; max_score += 20

        # --- Live Chat / Chatbot ---
        chat_sig = re.search(
            r'intercom|drift|crisp|tawk|zendesk|livechat|tidio|freshchat|hubspot|'
            r'chatbot|live.?chat|chat.?widget|widget.?chat|genesys|qualified', html_lower)
        chat_pts = 20 if chat_sig else 0
        chat_note = 'Live chat / chatbot detected ✓' if chat_sig else \
                    'No chat detected — chatbots reduce application abandonment by up to 40%'
        checks.append({'name': 'Live Chat & Chatbot', 'weight': 20, 'score': chat_pts, 'max': 20,
                        'status': self._pts_status(chat_pts, 20), 'detail': chat_note})
        score += chat_pts; max_score += 20

        # --- Job Search & Filter ---
        search_box = (soup.find('input', attrs={'type': 'search'}) or
                      soup.find('input', placeholder=re.compile(r'search|job|role|keyword', re.I)) or
                      soup.find('input', id=re.compile(r'search|job', re.I)))
        search_form = soup.find('form', id=re.compile(r'search', re.I))
        filter_els  = soup.find_all(class_=re.compile(r'filter|facet|refine', re.I))
        srch_pts = 0; srch_notes = []
        if search_box or search_form:
            srch_pts += 15; srch_notes.append('Job search detected ✓')
        else:
            srch_notes.append('No job search on page — candidates expect instant search')
        if filter_els:
            srch_pts += 5; srch_notes.append(f'{len(filter_els)} filter element(s) ✓')
        srch_pts = min(srch_pts, 20)
        checks.append({'name': 'Job Search & Filters', 'weight': 20, 'score': srch_pts, 'max': 20,
                        'status': self._pts_status(srch_pts, 20), 'detail': ' | '.join(srch_notes)})
        score += srch_pts; max_score += 20

        # --- Social Sharing ---
        share_els = soup.find_all(class_=re.compile(r'\bshare\b|social-share', re.I))
        share_sig = bool(re.search(r'share.*job|refer.*friend|share.*role', html_lower))
        share_pts = 15 if (share_els or share_sig) else 0
        share_note = 'Social sharing detected ✓' if (share_els or share_sig) else \
                     'No social sharing — candidates cannot easily refer friends to roles'
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

    def _conversion_summary(self, score):
        if score >= 80:
            return 'High-converting site. Strong CTAs, job alerts, and live search make applying effortless.'
        if score >= 60:
            return 'Good conversion features with notable gaps in lead capture and re-engagement.'
        if score >= 40:
            return 'Conversion rate is suffering from missing CTAs, no job alerts, or weak search UX.'
        return 'Extremely low conversion potential. Most visitors landing here will not take action.'

    # -------------------------------------------------------------------------
    # Recommendations & Shazamme Advantage
    # -------------------------------------------------------------------------

    def _generate_recommendations(self, pillars: Dict) -> List[Dict]:
        IMPACT = {
            'Title Tag': 'Directly controls click-through rate from search results',
            'Meta Description': 'Google uses this in SERPs — affects CTR by up to 30%',
            'Schema / Structured Data': 'Enables rich snippets — up to 3× higher CTR from search',
            'FAQ & Q&A Schema': 'Surfaces your answers directly in Google and AI search engines',
            'AI Crawler Access': 'Required for ChatGPT, Perplexity, and Claude to surface your jobs',
            'llms.txt File': 'Instructs AI models which content to use from your site',
            'llm-info File': 'Gives AI models structured data about your brand, sectors, and locations so they represent you accurately in generated answers',
            'Mobile Readiness': '60%+ of job searches happen on mobile — this is table stakes',
            'Apply Flow & Job Search': 'Every missing apply button = lost applications',
            'Job Alerts & Lead Capture': 'Most candidates are passive — alerts re-engage them automatically',
            'Live Chat & Chatbot': 'Reduces application abandonment by up to 40%',
            'Video Content': 'Increases application intent by 34% and time-on-page significantly',
            'EVP & Pay Transparency': '67% of candidates research compensation before applying',
            'Social Proof & Reviews': 'Candidates check reviews before deciding — it builds trust',
            'Server Response (TTFB)': 'Each second of delay reduces conversions by 7%',
            'HTTPS / SSL': 'Non-HTTPS sites show security warnings and rank lower in Google',
            'Open Graph / Social Tags': 'Controls how your site looks when shared on LinkedIn/WhatsApp',
            'H1 Heading': 'Primary page signal for Google — tells search what the page is about',
            'Content Compression': 'Reduces page weight — faster loads, better Core Web Vitals',
            'Job Search & Filters': 'Candidates expect instant, filterable search — no search = high bounce',
            'Call-to-Action Strength': 'Without clear CTAs candidates leave without converting',
            'DE&I Commitment': 'Growing factor in employer selection — affects talent pipeline diversity',
            'Content Depth': 'Thin pages rank poorly and give AI engines nothing to work with',
            'Entity & Authority': 'Helps AI engines reliably identify and cite your brand',
            'Industry & Sector Pages': 'Pages like "accounting jobs" and "IT recruitment" capture high-intent keyword searches — typically 60-80% of recruiter organic traffic',
            'Indexability': 'If noindex is set, your page will not appear in any search results',
            'Canonical URL': 'Prevents duplicate content from splitting your ranking signals',
        }
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
                        'impact': IMPACT.get(check['name'], 'Affects overall talent attraction performance'),
                    })
        recs.sort(key=lambda x: 0 if x['priority'] == 'critical' else 1)
        return recs[:15]

    def _generate_shazamme_advantage(self, pillars: Dict) -> List[Dict]:
        all_checks: Dict[str, str] = {}
        for pd in pillars.values():
            for check in pd.get('checks', []):
                all_checks[check['name']] = check['status']

        features = []

        if all_checks.get('Schema / Structured Data') != 'pass':
            features.append({
                'gap': 'Missing or weak schema markup',
                'feature': 'Auto Schema Engine',
                'description': 'Shazamme auto-generates JobPosting, Organization, FAQPage, and BreadcrumbList schema on every page — zero config, zero dev time.',
                'icon': 'label',
                'stat': '3× higher CTR from rich snippets',
            })

        if all_checks.get('Apply Flow & Job Search') != 'pass':
            features.append({
                'gap': 'No job search or weak apply flow',
                'feature': 'AI-Powered Job Search & One-Click Apply',
                'description': 'Instant AI-matched job search, smart filters by location/salary/type, and a streamlined mobile-first apply flow — all built in.',
                'icon': 'search',
                'stat': 'Up to 60% more completed applications',
            })

        if all_checks.get('Job Alerts & Lead Capture') != 'pass':
            features.append({
                'gap': 'No job alerts or lead capture',
                'feature': 'Intelligent Job Alert Engine',
                'description': 'Passive candidates subscribe to AI-matched alerts. Shazamme re-engages them the moment a relevant role is posted.',
                'icon': 'notifications',
                'stat': 'Re-engages 4× more passive candidates',
            })

        if all_checks.get('Live Chat & Chatbot') != 'pass':
            features.append({
                'gap': 'No chatbot or live engagement',
                'feature': 'AI Recruitment Chatbot',
                'description': "Shazamme's built-in AI chatbot qualifies candidates 24/7, answers FAQ questions, and guides applicants through the process without recruiter intervention.",
                'icon': 'smart_toy',
                'stat': 'Reduces drop-off by up to 40%',
            })

        if all_checks.get('AI Crawler Access') != 'pass' or not self.has_llms_txt or not self.llm_info_url:
            features.append({
                'gap': 'Invisible to AI search engines',
                'feature': 'GEO-Ready Out of the Box',
                'description': 'Every Shazamme site ships with llms.txt, llm-info, FAQPage schema, and AI-optimised content architecture — fully visible and accurately represented in ChatGPT, Perplexity, and Claude.',
                'icon': 'auto_awesome',
                'stat': 'Ranks in AI-generated job search answers',
            })

        if all_checks.get('Mobile Readiness') != 'pass':
            features.append({
                'gap': 'Poor mobile experience',
                'feature': 'Mobile-First Architecture',
                'description': 'Shazamme sites are built mobile-first with lightning-fast performance — scoring 90+ on Google PageSpeed without any configuration.',
                'icon': 'smartphone',
                'stat': '95+ Google Mobile Speed Score',
            })

        if all_checks.get('Video Content') != 'pass':
            features.append({
                'gap': 'No employer brand video',
                'feature': 'Employer Brand Content Studio',
                'description': 'Shazamme drag-and-drop content blocks make it trivial to add team videos, culture storytelling, and candidate testimonials.',
                'icon': 'videocam',
                'stat': '+34% application intent with video',
            })

        if all_checks.get('EVP & Pay Transparency') != 'pass':
            features.append({
                'gap': 'Weak EVP and pay transparency',
                'feature': 'EVP & Transparency Framework',
                'description': 'Pre-built templates for salary ranges, benefits highlights, flexibility options, and DE&I commitments — helping you attract higher-quality candidates faster.',
                'icon': 'workspace_premium',
                'stat': '2× candidate quality with transparent EVP',
            })

        if all_checks.get('Industry & Sector Pages') != 'pass':
            features.append({
                'gap': 'Missing or insufficient sector/industry pages',
                'feature': 'Sector Page Generator',
                'description': "Shazamme auto-generates SEO-optimised sector pages for every industry you recruit in — each one targeting '[sector] jobs' and '[sector] recruitment' keywords with live job feeds built in.",
                'icon': 'category',
                'stat': '60-80% of recruiter organic traffic from sector keywords',
            })

        if all_checks.get('Server Response (TTFB)') != 'pass':
            features.append({
                'gap': 'Slow server response times',
                'feature': 'Global Edge CDN',
                'description': 'Shazamme sites are delivered from a global edge network with automatic image optimisation, ensuring sub-second load times worldwide.',
                'icon': 'bolt',
                'stat': '<0.5s TTFB on Shazamme sites',
            })

        return features[:6]
