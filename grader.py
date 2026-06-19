import asyncio
import aiohttp
import os
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

    def __init__(self, url: str, mode: str = 'recruitment', light: bool = False):
        self.raw_url = url
        self.url = self._normalize_url(url)
        self.parsed = urlparse(self.url)
        self.base_url = f"{self.parsed.scheme}://{self.parsed.netloc}"
        self.mode = mode
        # light mode skips the slow PageSpeed call (used for competitor benchmarks)
        self.light = light
        self.psi_key = os.environ.get('PAGESPEED_API_KEY', '')
        self.enable_psi = os.environ.get('ENABLE_PAGESPEED', '1') != '0'
        self.pagespeed: Optional[Dict] = None
        self.cwv_attempted = False
        self._psi_task = None
        # In-grade PSI is a single quick attempt to keep grades fast; heavy sites
        # that miss this are filled in asynchronously by the /api/cwv endpoint,
        # which uses the longer timeouts below.
        self.psi_timeouts = (38,)
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
        # Multi-page evidence (recruitment/career modes fetch /job-results, /job-detail, ...)
        self.extra_html: Dict[str, str] = {}
        self.extra_soups: Dict[str, BeautifulSoup] = {}
        self.job_routes_found: List[str] = []
        # Parsed JSON-LD cache
        self._schema_objects: Optional[List[dict]] = None
        self._schema_parse_errors = 0
        self._rec_signals: Optional[Dict] = None

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

        # Kick off Google PageSpeed/Lighthouse in the background so its latency
        # overlaps the rest of the analysis. Awaited inside the technical pillar.
        if self.enable_psi and not self.light:
            self._psi_task = asyncio.ensure_future(self._fetch_pagespeed())

        # Parallel secondary fetches
        yield {'type': 'status', 'message': 'Checking AI crawler access and robots.txt...', 'progress': 18}
        secondary_fetches = [self._fetch_robots_txt(), self._check_llms_txt(), self._check_llm_info()]
        if self.mode == 'general':
            secondary_fetches.append(self._check_sitemap())
        else:
            # Recruitment / career sites render apply + search client-side on
            # dedicated routes — fetch them so detection isn't a false negative.
            secondary_fetches.append(self._fetch_recruitment_pages())
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
            self._attach_guidance(result)
            pillar_results[pillar_id] = result
            yield {'type': 'pillar_complete', 'pillar': pillar_id, 'data': result}

        overall = sum(pillar_results[p]['score'] * w for p, w in weights.items())
        overall = round(overall)

        recommendations = self._generate_recommendations(pillar_results)
        shazamme_items = self._generate_shazamme_advantage(pillar_results) if self.mode != 'general' else []
        executive_summary = self._generate_executive_summary(pillar_results, overall, recommendations)

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
            'executive_summary': executive_summary,
            'core_web_vitals': self.pagespeed,
            'cwv_attempted': self.cwv_attempted,
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

    async def _fetch_recruitment_pages(self):
        """Fetch the dedicated job routes that Shazamme/recruitment sites use so
        that Apply-flow and Job-search detection isn't a false negative when the
        homepage doesn't carry those widgets."""
        paths = ['/job-results', '/job-detail', '/jobs', '/search-jobs',
                 '/job-search', '/vacancies', '/careers', '/find-a-job']
        try:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_ctx, limit=8)
            timeout = aiohttp.ClientTimeout(total=12, sock_read=8)

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
                async def fetch_one(path):
                    url = urljoin(self.base_url, path)
                    try:
                        async with sess.get(url, headers=self.HEADERS, allow_redirects=True) as resp:
                            if resp.status == 200:
                                html = await resp.text(encoding='utf-8', errors='replace')
                                if len(html) > 500:
                                    self.extra_html[path] = html
                                    self.extra_soups[path] = BeautifulSoup(html, 'html.parser')
                                    self.job_routes_found.append(path)
                    except Exception:
                        pass
                await asyncio.gather(*[fetch_one(p) for p in paths])
        except Exception:
            pass

    async def _fetch_pagespeed(self):
        """Google PageSpeed Insights v5 — Lighthouse lab metrics + CrUX field
        data (real Core Web Vitals from Chrome users). Optional and best-effort:
        degrades silently to TTFB-only if no key / quota / timeout. Lighthouse
        also fully renders the page, giving us a rendered-DOM performance signal
        the static fetch cannot measure."""
        self.cwv_attempted = True
        endpoint = 'https://www.googleapis.com/pagespeedonline/v5/runPagespeed'
        # Only request the 'performance' category — it carries the perf score +
        # Core Web Vitals. Requesting all four categories ~3-4x's the Lighthouse
        # run time and times out on heavy sites.
        params = [('url', self.url), ('strategy', 'mobile'), ('category', 'performance')]
        if self.psi_key:
            params.append(('key', self.psi_key))
        # Lighthouse run time varies; retry per configured timeouts.
        for attempt_timeout in self.psi_timeouts:
            try:
                timeout = aiohttp.ClientTimeout(total=attempt_timeout)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.get(endpoint, params=params) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                parsed = self._parse_pagespeed(data)
                if parsed and parsed.get('perf_score') is not None:
                    self.pagespeed = parsed
                    return
            except Exception:
                continue
        self.pagespeed = None

    def _parse_pagespeed(self, data: dict) -> Optional[Dict]:
        lr = data.get('lighthouseResult', {})
        cats = lr.get('categories', {})
        audits = lr.get('audits', {})

        def cat_score(k):
            v = cats.get(k, {}).get('score')
            return round(v * 100) if isinstance(v, (int, float)) else None

        def audit_num(k):
            return audits.get(k, {}).get('numericValue')

        le = data.get('loadingExperience', {}).get('metrics', {})

        def field(metric):
            m = le.get(metric, {})
            return {'p75': m.get('percentile'), 'rating': m.get('category')}  # FAST/AVERAGE/SLOW

        result = {
            'perf_score': cat_score('performance'),
            'seo_score': cat_score('seo'),
            'a11y_score': cat_score('accessibility'),
            'bp_score': cat_score('best-practices'),
            'lab': {
                'lcp_ms': audit_num('largest-contentful-paint'),
                'cls': audit_num('cumulative-layout-shift'),
                'tbt_ms': audit_num('total-blocking-time'),
                'fcp_ms': audit_num('first-contentful-paint'),
                'si_ms': audit_num('speed-index'),
            },
            'field': {
                'lcp': field('LARGEST_CONTENTFUL_PAINT_MS'),
                'inp': field('INTERACTION_TO_NEXT_PAINT'),
                'cls': field('CUMULATIVE_LAYOUT_SHIFT_SCORE'),
                'fcp': field('FIRST_CONTENTFUL_PAINT_MS'),
            },
            'has_field': bool(le),
        }
        return result

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _all_soups(self) -> List[BeautifulSoup]:
        soups = [self.soup] if self.soup else []
        soups.extend(self.extra_soups.values())
        return soups

    def _combined_html(self) -> str:
        return ' '.join([self.html] + list(self.extra_html.values())).lower()

    def _combined_text(self) -> str:
        return ' '.join(s.get_text(' ') for s in self._all_soups()).lower()

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

    def _get_schema_objects(self) -> List[dict]:
        """Parse every JSON-LD block across all fetched pages into a flat list of
        typed nodes (recursing @graph and nested objects). Malformed blocks are
        counted in self._schema_parse_errors."""
        if self._schema_objects is not None:
            return self._schema_objects
        objs: List[dict] = []
        for soup in self._all_soups():
            for script in soup.find_all('script', type='application/ld+json'):
                raw = (script.string or script.get_text() or '').strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except Exception:
                    self._schema_parse_errors += 1
                    continue
                self._flatten_nodes(data, objs)
        self._schema_objects = objs
        return objs

    def _flatten_nodes(self, data, out: List[dict]):
        if isinstance(data, dict):
            if data.get('@type'):
                out.append(data)
            for v in data.values():
                if isinstance(v, (dict, list)):
                    self._flatten_nodes(v, out)
        elif isinstance(data, list):
            for item in data:
                self._flatten_nodes(item, out)

    def _get_schema_types(self) -> List[str]:
        types: List[str] = []
        for node in self._get_schema_objects():
            t = node.get('@type', '')
            if isinstance(t, list):
                types.extend(str(x) for x in t)
            elif t:
                types.append(str(t))
        return types

    def _find_schema_node(self, *type_names) -> Optional[dict]:
        wanted = {t.lower() for t in type_names}
        for node in self._get_schema_objects():
            t = node.get('@type', '')
            tl = {t.lower()} if isinstance(t, str) else {str(x).lower() for x in t}
            if wanted & tl:
                return node
        return None

    JOBPOSTING_REQUIRED = ['title', 'datePosted', 'hiringOrganization', 'jobLocation', 'description']
    JOBPOSTING_RICH = ['validThrough', 'employmentType', 'baseSalary',
                       'jobLocationType', 'applicantLocationRequirements', 'directApply']

    def _validate_jobposting(self) -> Optional[Dict]:
        node = self._find_schema_node('JobPosting')
        if not node:
            return None
        empty = (None, '', [], {})
        present_req = [f for f in self.JOBPOSTING_REQUIRED if node.get(f) not in empty]
        present_rich = [f for f in self.JOBPOSTING_RICH if node.get(f) not in empty]
        missing_req = [f for f in self.JOBPOSTING_REQUIRED if f not in present_req]
        return {'present_req': present_req, 'missing_req': missing_req, 'present_rich': present_rich}

    def _has_search_action(self) -> bool:
        site = self._find_schema_node('WebSite')
        if not site:
            return False
        pa = site.get('potentialAction')
        if not pa:
            return False
        nodes = pa if isinstance(pa, list) else [pa]
        return any(isinstance(n, dict) and 'SearchAction' in str(n.get('@type', '')) for n in nodes)

    def _recruitment_signals(self) -> Dict:
        """Apply-flow / job-search evidence aggregated across the homepage and the
        dedicated job routes, with platform/widget fallback so client-rendered
        Shazamme/Duda job boards aren't scored as missing."""
        if self._rec_signals is not None:
            return self._rec_signals

        soups = self._all_soups()
        combined_text = self._combined_text()
        combined_html = self._combined_html()

        apply_btns, apply_links, filter_els = [], [], []
        search_box = None
        search_form = None
        field_inputs = []
        for s in soups:
            apply_btns += s.find_all(
                lambda t: t.name in ('a', 'button') and re.search(r'\bapply\b', t.get_text(' '), re.I))
            apply_links += s.find_all('a', href=re.compile(r'apply', re.I))
            filter_els += s.find_all(class_=re.compile(r'filter|facet|refine', re.I))
            field_inputs += s.find_all(['input', 'select', 'textarea'])
            if search_box is None:
                search_box = (s.find('input', attrs={'type': 'search'}) or
                              s.find('input', placeholder=re.compile(r'search|job|role|keyword', re.I)) or
                              s.find('input', id=re.compile(r'search|job', re.I)) or
                              s.find('input', attrs={'name': re.compile(r'search|keyword|job', re.I)}))
            if search_form is None:
                search_form = (s.find('form', id=re.compile(r'search', re.I)) or
                               s.find('form', class_=re.compile(r'search|job-search', re.I)))

        # Platform / job-widget detection (client-rendered boards)
        is_duda = bool(re.search(r'cdn-website\.com|dudaone|window\.parameters|irp\.cdn-website', combined_html))
        is_shazamme = bool(re.search(r'shazamme', combined_html))
        job_widget = bool(re.search(
            r'job-?board|jobboard|dmrespcol|dmcollection|data-collection|dynamic.?page.?item|collectionlist',
            combined_html)) and ('job' in combined_text)

        platform = 'Shazamme' if is_shazamme else ('Duda' if is_duda else None)
        widget_present = bool(job_widget or (platform and self.job_routes_found))

        visible_fields = len([
            f for f in field_inputs
            if f.get('type', 'text') not in ('hidden', 'submit', 'button', 'reset', 'image')
        ])

        has_apply = bool(apply_btns or apply_links) or widget_present
        has_search = (bool(search_box or search_form) or
                      'job search' in combined_text or 'search jobs' in combined_text or
                      widget_present)

        self._rec_signals = {
            'apply_count': len(apply_btns) + len(apply_links),
            'has_apply': has_apply,
            'has_search': has_search,
            'filter_count': len(filter_els),
            'field_count': visible_fields,
            'platform': platform,
            'widget_present': widget_present,
            'job_routes': list(self.job_routes_found),
        }
        return self._rec_signals

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

        # --- Structured-data validity (recruitment & career) ---
        if self.mode in ('recruitment', 'career_site'):
            jp = self._validate_jobposting()
            sig = self._recruitment_signals()
            v_max = 12; v_pts = 0; v_notes = []
            if jp is None:
                if sig['widget_present']:
                    v_pts += 5
                    v_notes.append('JobPosting likely rendered client-side by the job widget — '
                                   'confirm it is in the crawlable DOM for Google Jobs')
                else:
                    v_notes.append('No JobPosting schema on scanned pages — required for Google Jobs')
            else:
                req_ratio = len(jp['present_req']) / len(self.JOBPOSTING_REQUIRED)
                v_pts += round(8 * req_ratio)
                if jp['missing_req']:
                    v_notes.append(f"JobPosting missing required: {', '.join(jp['missing_req'])}")
                else:
                    v_notes.append('JobPosting has all required fields ✓ (Google Jobs eligible)')
                if jp['present_rich']:
                    v_pts += min(len(jp['present_rich']), 2)
                    v_notes.append(f"Rich fields: {', '.join(jp['present_rich'][:4])} ✓")
            if self._has_search_action():
                v_pts += 2; v_notes.append('WebSite SearchAction (sitelinks search box) ✓')
            else:
                v_notes.append('No WebSite SearchAction — add for a sitelinks search box')
            if self._schema_parse_errors:
                v_notes.append(f'{self._schema_parse_errors} malformed JSON-LD block(s) — fix to be machine-readable')
            v_pts = min(v_pts, v_max)
            checks.append({'name': 'Structured Data Validity', 'weight': v_max, 'score': v_pts, 'max': v_max,
                            'status': self._pts_status(v_pts, v_max), 'detail': ' | '.join(v_notes)})
            score += v_pts; max_score += v_max

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

        # --- AI Crawler Access (full 2026 bot matrix) ---
        # group -> the user-agent tokens that represent it in robots.txt
        AI_BOTS = {
            'ChatGPT (GPTBot)':        ['gptbot'],
            'ChatGPT Search':          ['oai-searchbot', 'chatgpt-user'],
            'Claude (ClaudeBot)':      ['claudebot', 'anthropic-ai', 'claude-web', 'claude-searchbot'],
            'Google AI (Gemini/AIO)':  ['google-extended'],
            'Perplexity':              ['perplexitybot', 'perplexity-user'],
            'Apple Intelligence':      ['applebot-extended'],
            'Amazon (Alexa/Rufus)':    ['amazonbot'],
            'Meta AI':                 ['meta-externalagent', 'facebookbot'],
            'Common Crawl (CCBot)':    ['ccbot'],
            'ByteDance (Bytespider)':  ['bytespider'],
            'Cohere':                  ['cohere-ai'],
        }
        blocked_groups = []
        if self.robots_txt:
            # Map each user-agent token to whether it is fully disallowed
            disallow_all_agents = set()
            current_agents: List[str] = []
            for line in self.robots_txt.lower().splitlines():
                line = line.strip()
                if line.startswith('user-agent:'):
                    current_agents.append(line.split(':', 1)[1].strip())
                elif line.startswith('disallow:'):
                    val = line.split(':', 1)[1].strip()
                    if val in ('/', '/*'):
                        for a in current_agents:
                            disallow_all_agents.add(a)
                    current_agents = []  # reset group after directives
                elif line.startswith('allow:'):
                    current_agents = current_agents
                elif line == '':
                    current_agents = []
            for group, tokens in AI_BOTS.items():
                if any(tok in disallow_all_agents for tok in tokens):
                    blocked_groups.append(group)

        ai_max = 20
        ai_notes = []
        if self.robots_txt:
            ai_notes.append('robots.txt found ✓')
            sitemap_directive = 'sitemap:' in self.robots_txt.lower()
            if sitemap_directive:
                ai_notes.append('Sitemap directive ✓')
            total = len(AI_BOTS)
            allowed = total - len(blocked_groups)
            ai_pts = round(ai_max * (allowed / total))
            if blocked_groups:
                ai_notes.append(f'BLOCKED for: {", ".join(blocked_groups[:5])}')
            else:
                ai_notes.append(f'All {total} major AI crawlers allowed ✓')
        else:
            # No robots.txt = nothing is blocked (default-allow) but no signalling
            ai_pts = round(ai_max * 0.8)
            ai_notes.append('No robots.txt — AI crawlers default-allowed but you give them no guidance')
        checks.append({'name': 'AI Crawler Access', 'weight': ai_max, 'score': ai_pts, 'max': ai_max,
                        'status': self._pts_status(ai_pts, ai_max), 'detail': ' | '.join(ai_notes),
                        'value': f'{len(AI_BOTS) - len(blocked_groups)}/{len(AI_BOTS)} AI crawlers allowed'})
        score += ai_pts; max_score += ai_max

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
        ent_max = 20
        org_node = self._find_schema_node(
            'Organization', 'Corporation', 'EmploymentAgency',
            'StaffingAgency', 'RecruitmentAgency', 'LocalBusiness')
        has_contact = bool(self._find_schema_node('ContactPoint')) or \
            (org_node is not None and 'contactPoint' in org_node)
        has_person = bool(self._find_schema_node('Person'))

        # sameAs — the single strongest entity-disambiguation signal for AI
        same_as = []
        if org_node:
            sa = org_node.get('sameAs')
            if isinstance(sa, str):
                same_as = [sa]
            elif isinstance(sa, list):
                same_as = [str(x) for x in sa]
        AUTHORITY_HOSTS = ('wikipedia.org', 'wikidata.org', 'crunchbase.com', 'linkedin.com',
                           'glassdoor', 'bloomberg.com', 'opencorporates.com')
        authority_refs = [u for u in same_as if any(h in u.lower() for h in AUTHORITY_HOSTS)]

        # NAP: address + phone presence in schema or visible text
        page_text = soup.get_text(' ')
        has_address = bool(org_node and org_node.get('address')) or \
            bool(re.search(r'\b\d{1,5}\s+\w+(\s\w+){0,3}\s+(street|st|road|rd|avenue|ave|lane|ln|drive|dr|suite|level|floor)\b', page_text, re.I))
        has_phone = bool(org_node and org_node.get('telephone')) or \
            bool(re.search(r'(\+?\d[\d\s().-]{7,}\d)', page_text))

        # About / Team pages (E-E-A-T people)
        about_link = soup.find('a', href=re.compile(r'about|who.?we.?are|our.?story|our.?team|meet.?the.?team|leadership|people', re.I))

        # Accreditation / professional bodies (recruitment authority)
        accreditation = bool(re.search(
            r'\brec\b|\bapsco\b|\bsia\b|\bcipd\b|iso\s?\d{4,5}|accredit|certified|chartered|member of',
            page_text, re.I))

        ent_pts = 0; ent_notes = []
        if org_node:
            ent_pts += 6; ent_notes.append('Organization entity ✓')
        else:
            ent_notes.append('No Organization schema — AI cannot reliably identify your brand')
        if authority_refs:
            ent_pts += 6; ent_notes.append(f'sameAs → authority profiles ✓ ({len(authority_refs)})')
        elif same_as:
            ent_pts += 3; ent_notes.append('sameAs present (add Wikidata/Crunchbase/LinkedIn)')
        else:
            ent_notes.append('No sameAs links — add Wikidata/LinkedIn/Crunchbase to enter the Knowledge Graph')
        if has_contact or (has_address and has_phone):
            ent_pts += 3; ent_notes.append('NAP / contact details ✓')
        else:
            ent_notes.append('Incomplete NAP (name/address/phone)')
        if has_person or about_link:
            ent_pts += 3; ent_notes.append('People / about-team signals ✓')
        else:
            ent_notes.append('No named people / about page — weak E-E-A-T')
        if accreditation:
            ent_pts += 2; ent_notes.append('Accreditation / credentials ✓')
        ent_pts = min(ent_pts, ent_max)
        checks.append({'name': 'Entity & Authority', 'weight': ent_max, 'score': ent_pts, 'max': ent_max,
                        'status': self._pts_status(ent_pts, ent_max),
                        'detail': ' | '.join(ent_notes) if ent_notes else 'No entity signals detected',
                        'value': (', '.join(authority_refs[:3]) if authority_refs else None)})
        score += ent_pts; max_score += ent_max

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

        # --- AEO / Answer-Engine Readiness ---
        # The content shape AI engines quote from: summary blocks, question
        # headings, direct answers, tables, quotable stats, freshness.
        aeo_max = 20
        headings = soup.find_all(['h2', 'h3'])
        heading_txts = [h.get_text(' ').strip() for h in headings]
        QUESTION_RE = re.compile(r'\?|\b(how|what|why|when|where|which|who|can|should|is|are|do|does|best)\b', re.I)
        q_headings = [t for t in heading_txts if QUESTION_RE.search(t)]
        has_tldr = bool(re.search(
            r'tl;?dr|key takeaway|in summary|at a glance|quick answer|the short answer|summary\s*[:—-]',
            page_text_body := soup.get_text(' '), re.I))
        tables = soup.find_all('table')
        stat_hits = re.findall(r'\b\d{1,3}(?:\.\d+)?\s?%|\b(?:£|\$|€)\s?\d', page_text_body)
        has_freshness = bool(re.search(r'last updated|updated on|reviewed on|published on|posted on', page_text_body, re.I)) \
            or bool(self._find_schema_node('Article', 'BlogPosting') and
                    any(self._find_schema_node('Article', 'BlogPosting').get(k) for k in ('dateModified', 'datePublished')))

        aeo_pts = 0; aeo_notes = []
        if has_tldr:
            aeo_pts += 5; aeo_notes.append('Summary / TL;DR block ✓ — directly quotable by AI')
        else:
            aeo_notes.append('No summary/TL;DR block — add an extractable answer near the top')
        if len(q_headings) >= 3:
            aeo_pts += 6; aeo_notes.append(f'{len(q_headings)} question-style headings ✓')
        elif q_headings:
            aeo_pts += 3; aeo_notes.append(f'{len(q_headings)} question-style heading(s) — add more')
        else:
            aeo_notes.append('No question-style headings — match how people ask AI')
        if tables:
            aeo_pts += 4; aeo_notes.append(f'{len(tables)} table(s) — highly extractable ✓')
        if len(stat_hits) >= 3:
            aeo_pts += 3; aeo_notes.append(f'{len(stat_hits)} quotable stats/figures ✓')
        else:
            aeo_notes.append('Few concrete stats/figures — AI prefers citable numbers')
        if has_freshness:
            aeo_pts += 2; aeo_notes.append('Freshness signal (last updated / dateModified) ✓')
        else:
            aeo_notes.append('No visible freshness signal — AI favours up-to-date content')
        aeo_pts = min(aeo_pts, aeo_max)
        checks.append({'name': 'AEO / Answer-Engine Readiness', 'weight': aeo_max, 'score': aeo_pts, 'max': aeo_max,
                        'status': self._pts_status(aeo_pts, aeo_max), 'detail': ' | '.join(aeo_notes)})
        score += aeo_pts; max_score += aeo_max

        # --- Crawlable Content (JS-rendering) ---
        # AI crawlers mostly do NOT execute JS. If the primary content only
        # exists after client-side rendering, the page is invisible to them.
        crawl_max = 15
        visible_text = soup.get_text(' ', strip=True)
        text_len = len(visible_text)
        html_len = max(len(self.html), 1)
        text_ratio = text_len / html_len
        scripts = soup.find_all('script')
        script_bytes = sum(len(s.get_text() or '') for s in scripts)
        noscript = soup.find('noscript')
        empty_spa_root = bool(soup.find(id=re.compile(r'^(root|app|__next)$')) and text_len < 600)
        is_duda_spa = bool(re.search(r'cdn-website\.com|window\.parameters', self.html.lower()))

        crawl_pts = 0; crawl_notes = []
        if text_len >= 1500 and text_ratio >= 0.05:
            crawl_pts += 11; crawl_notes.append(f'{text_len:,} chars of crawlable text ✓')
        elif text_len >= 600:
            crawl_pts += 7; crawl_notes.append(f'{text_len:,} chars of static text — adequate')
        else:
            crawl_notes.append(f'Only {text_len:,} chars of static text — content may be JS-rendered & invisible to AI crawlers')
        if empty_spa_root:
            crawl_notes.append('Empty SPA root in HTML — server-render or pre-render for AI crawlers')
        if noscript and (noscript.get_text() or '').strip():
            crawl_pts += 4; crawl_notes.append('noscript fallback ✓')
        elif text_len < 1500:
            crawl_notes.append('No noscript fallback for JS-only content')
        else:
            crawl_pts += 4
        if is_duda_spa and text_len < 1500:
            crawl_notes.append('Duda widget content is client-rendered — ensure a static/pre-rendered snapshot exists')
        crawl_pts = min(crawl_pts, crawl_max)
        checks.append({'name': 'Crawlable Content (JS-render)', 'weight': crawl_max, 'score': crawl_pts, 'max': crawl_max,
                        'status': self._pts_status(crawl_pts, crawl_max), 'detail': ' | '.join(crawl_notes)})
        score += crawl_pts; max_score += crawl_max

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

            sem = self._semantic_a11y_check(soup)
            checks.append(sem)
            score += sem['score']; max_score += sem['max']

            # --- Apply Flow (20pts max) ---
            sig = self._recruitment_signals()
            field_count = sig['field_count']
            apply_pts = 0; apply_notes = []
            if sig['apply_count'] >= 2:
                apply_pts += 10; apply_notes.append(f"{sig['apply_count']} Apply buttons ✓")
            elif sig['apply_count'] == 1:
                apply_pts += 6; apply_notes.append('1 Apply button found')
            elif sig['widget_present']:
                apply_pts += 10; apply_notes.append(f"Apply flow via {sig['platform'] or 'job'} widget ✓ (client-rendered)")
            else:
                apply_notes.append('No Apply button detected — how do candidates apply?')

            if field_count == 0 and sig['widget_present']:
                apply_pts += 5; apply_notes.append('Apply form rendered by widget')
            elif field_count <= 5:
                apply_pts += 5; apply_notes.append(f'{field_count} form fields — streamlined ✓')
            elif field_count <= 10:
                apply_pts += 3; apply_notes.append(f'{field_count} form fields — acceptable')
            else:
                apply_notes.append(f'{field_count} form fields — too many, simplify the apply flow')

            if sig['has_search']:
                apply_pts += 5
                apply_notes.append('Job search via widget ✓' if sig['widget_present'] else 'Job search detected ✓')
            else:
                apply_notes.append('No job search — candidates cannot find relevant roles')
            if sig['job_routes']:
                apply_notes.append(f"Job routes live: {', '.join(sig['job_routes'][:3])}")

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

            sem = self._semantic_a11y_check(soup)
            checks.append(sem)
            score += sem['score']; max_score += sem['max']

            # --- Apply & Job Search (multi-page + widget-aware) ---
            sig = self._recruitment_signals()
            apply_pts = 0; apply_notes = []
            if sig['apply_count'] >= 2:
                apply_pts += 15; apply_notes.append(f"{sig['apply_count']} Apply buttons ✓")
            elif sig['apply_count'] == 1:
                apply_pts += 10; apply_notes.append('1 Apply button found')
            elif sig['widget_present']:
                apply_pts += 13; apply_notes.append(f"Apply flow via {sig['platform'] or 'job'} widget ✓ (client-rendered)")
            else:
                apply_notes.append('No Apply button detected — how do candidates apply?')
            if sig['has_search']:
                apply_pts += 10
                apply_notes.append('Job search via widget ✓' if sig['widget_present'] else 'Job search detected ✓')
            else:
                apply_notes.append('No job search — candidates cannot find relevant roles')
            if sig['job_routes']:
                apply_notes.append(f"Job routes live: {', '.join(sig['job_routes'][:3])}")
            apply_pts = min(apply_pts, 25)
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

    def _semantic_a11y_check(self, soup: BeautifulSoup) -> Dict:
        """Deeper static accessibility signals beyond alt/lang: semantic landmarks,
        form label association, descriptive link text, and heading order."""
        pts = 0; notes = []; mx = 12

        # 1. Landmarks (3)
        has_main = bool(soup.find('main') or soup.find(attrs={'role': 'main'}))
        has_nav = bool(soup.find('nav') or soup.find(attrs={'role': 'navigation'}))
        has_header = bool(soup.find('header') or soup.find(attrs={'role': 'banner'}))
        landmarks = sum([has_main, has_nav, has_header])
        if landmarks >= 3:
            pts += 3; notes.append('Semantic landmarks (main/nav/header) ✓')
        elif landmarks >= 1:
            pts += 1; notes.append(f'{landmarks}/3 landmarks — add <main>/<nav>/<header>')
        else:
            notes.append('No semantic landmarks — screen readers can\'t skip to regions')

        # 2. Form label association (3)
        inputs = [i for i in soup.find_all(['input', 'select', 'textarea'])
                  if i.get('type', 'text') not in ('hidden', 'submit', 'button', 'reset', 'image')]
        if inputs:
            label_fors = {l.get('for') for l in soup.find_all('label') if l.get('for')}
            labelled = 0
            for i in inputs:
                if (i.get('aria-label') or i.get('aria-labelledby') or
                        (i.get('id') and i.get('id') in label_fors) or i.find_parent('label')):
                    labelled += 1
            ratio = labelled / len(inputs)
            if ratio >= 0.9:
                pts += 3; notes.append(f'{labelled}/{len(inputs)} form fields labelled ✓')
            elif ratio >= 0.5:
                pts += 1; notes.append(f'Only {labelled}/{len(inputs)} fields properly labelled')
            else:
                notes.append(f'{labelled}/{len(inputs)} fields labelled — most inputs inaccessible')
        else:
            pts += 3

        # 3. Descriptive link text (3)
        VAGUE = re.compile(r'^\s*(click here|here|read more|more|learn more|link|this)\s*$', re.I)
        links = soup.find_all('a', href=True)
        vague = [a for a in links if VAGUE.match(a.get_text(' ').strip())]
        if links:
            if len(vague) == 0:
                pts += 3; notes.append('Descriptive link text ✓')
            elif len(vague) <= 2:
                pts += 1; notes.append(f'{len(vague)} vague links ("click here") — describe the destination')
            else:
                notes.append(f'{len(vague)} vague link labels hurt screen-reader & SEO context')
        else:
            pts += 3

        # 4. Heading order — no skipped levels (3)
        levels = [int(h.name[1]) for h in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])]
        skipped = any(levels[i] - levels[i - 1] > 1 for i in range(1, len(levels)))
        if not levels:
            notes.append('No headings to evaluate order')
        elif not skipped:
            pts += 3; notes.append('Logical heading order ✓')
        else:
            pts += 1; notes.append('Heading levels skipped (e.g. H2→H4) — keep them sequential')

        return {'name': 'Semantic HTML & ARIA', 'weight': mx, 'score': pts, 'max': mx,
                'status': self._pts_status(pts, mx), 'detail': ' | '.join(notes)}

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

            # NOTE: DE&I is intentionally NOT scored in recruitment (agency) mode —
            # recruitment agency sites rarely surface DE&I content on the scanned
            # page, so scoring it only produced unfair false-negative penalties.
            # It remains scored in career_site mode where employers should have it.

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

        sem = self._semantic_a11y_check(soup)
        checks.append(sem)
        score += sem['score']; max_score += sem['max']

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

        # Wait for the background PageSpeed/Lighthouse task (if any) to finish.
        if self._psi_task is not None:
            try:
                await self._psi_task
            except Exception:
                self.pagespeed = None

        # --- Core Web Vitals & Lighthouse (real field + lab data) ---
        psi = self.pagespeed
        if psi:
            # Lighthouse Performance Score (lab, rendered)
            ps = psi.get('perf_score')
            if ps is not None:
                lh_max = 25
                lh_pts = round(lh_max * ps / 100)
                rating = 'excellent' if ps >= 90 else ('needs work' if ps >= 50 else 'poor')
                checks.append({'name': 'Lighthouse Performance', 'weight': lh_max, 'score': lh_pts, 'max': lh_max,
                               'status': self._pts_status(lh_pts, lh_max),
                               'detail': f'Google Lighthouse mobile performance score {ps}/100 — {rating}',
                               'value': f'{ps}/100'})
                score += lh_pts; max_score += lh_max

            # Core Web Vitals — prefer real-user field data, fall back to lab
            cwv_max = 25
            cwv_pts = 0; cwv_notes = []; has_data = False
            field = psi.get('field', {})
            lab = psi.get('lab', {})

            def rate_metric(name, value, good, poor, unit='ms'):
                # returns (points_fraction 0..1, note)
                if value is None:
                    return None, None
                if value <= good:
                    frac, tag = 1.0, 'good ✓'
                elif value <= poor:
                    frac, tag = 0.5, 'needs improvement'
                else:
                    frac, tag = 0.0, 'poor'
                disp = f'{value/1000:.2f}s' if unit == 'ms' else f'{value:.3f}'
                return frac, f'{name} {disp} ({tag})'

            # LCP
            lcp_field = field.get('lcp', {}).get('p75')
            lcp_val = lcp_field if lcp_field is not None else lab.get('lcp_ms')
            frac, note = rate_metric('LCP', lcp_val, 2500, 4000)
            if frac is not None:
                cwv_pts += frac * 9; cwv_notes.append(note); has_data = True
            # INP (field only) / TBT (lab proxy)
            inp_val = field.get('inp', {}).get('p75')
            if inp_val is not None:
                frac, note = rate_metric('INP', inp_val, 200, 500)
                cwv_pts += frac * 8; cwv_notes.append(note); has_data = True
            else:
                tbt = lab.get('tbt_ms')
                frac, note = rate_metric('TBT', tbt, 200, 600)
                if frac is not None:
                    cwv_pts += frac * 8; cwv_notes.append(note); has_data = True
            # CLS
            cls_field = field.get('cls', {}).get('p75')
            cls_val = cls_field if cls_field is not None else lab.get('cls')
            if cls_val is not None:
                # CrUX returns CLS*100 in percentile; lab returns raw — normalise
                cls_norm = cls_val / 100 if cls_field is not None else cls_val
                frac, note = rate_metric('CLS', cls_norm, 0.1, 0.25, unit='score')
                cwv_pts += frac * 8; cwv_notes.append(note); has_data = True

            if has_data:
                source = 'real-user field data (CrUX)' if psi.get('has_field') else 'lab (Lighthouse)'
                cwv_pts = round(min(cwv_pts, cwv_max))
                checks.append({'name': 'Core Web Vitals', 'weight': cwv_max, 'score': cwv_pts, 'max': cwv_max,
                               'status': self._pts_status(cwv_pts, cwv_max),
                               'detail': f'{" | ".join(cwv_notes)} — source: {source}',
                               'value': source})
                score += cwv_pts; max_score += cwv_max

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
                        'status': self._pts_status(hint_pts, hint_max),
                        'detail': ' | '.join(hint_notes) if hint_notes else 'None detected'})
        score += hint_pts; max_score += hint_max

        # --- Render-Blocking JS ---
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
            sig = self._recruitment_signals()
            srch_pts = 0; srch_notes = []
            if sig['has_search']:
                srch_pts += 15
                srch_notes.append('Job search via widget ✓' if sig['widget_present'] else 'Job search detected ✓')
            else:
                srch_notes.append('No job search — candidates expect instant search')
            if sig['filter_count']:
                srch_pts += 5; srch_notes.append(f"{sig['filter_count']} filter element(s) ✓")
            elif sig['widget_present']:
                srch_pts += 5; srch_notes.append('Filters rendered by job widget ✓')
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

    # -------------------------------------------------------------------------
    # Per-check remediation guidance ("what it is, why it matters, how to fix")
    # Attached to every check so a non-expert can understand and act on it.
    # -------------------------------------------------------------------------
    GUIDANCE = {
        'Title Tag': 'The <title> is the clickable headline in Google and the tab name. Write a unique 50-60 character title per page, lead with the primary keyword (e.g. "Healthcare Recruitment Agency | Brand"). Set it in your CMS/page settings — on Duda/Shazamme it is the page SEO title field.',
        'Meta Description': 'The grey snippet under your title in search results. Write a unique 120-160 character summary with a call to action and your main keyword. It does not affect ranking directly but lifts click-through. Edit it in the page SEO settings.',
        'H1 Heading': 'The single main on-page headline. Use exactly one <h1> per page describing the page topic (e.g. "Nursing Jobs Across Australia"). Most page builders mark the top hero heading as H1 — check it is not skipped or duplicated.',
        'Schema / Structured Data': 'Machine-readable JSON-LD that tells Google and AI engines what your page is. Add Organization (or StaffingAgency) on the homepage and JobPosting on job pages. Generate it at schema.org / Google Rich Results, paste into a <script type="application/ld+json"> block, and validate with the Rich Results Test.',
        'Structured Data Validity': 'Schema only works if it is complete and valid. JobPosting needs title, datePosted, validThrough, hiringOrganization, jobLocation, employmentType and baseSalary to be eligible for Google Jobs. Add a WebSite + SearchAction block for a sitelinks search box. Test every template in Google\'s Rich Results Test and fix any errors.',
        'Open Graph / Social Tags': 'og:title, og:description and og:image control how your link looks when shared on LinkedIn, WhatsApp and Slack. Add all four og tags plus a 1200×630 share image so shares render with a branded card instead of a bare URL.',
        'Canonical URL': 'A <link rel="canonical"> tells Google which URL is the master version, preventing duplicate-content dilution from tracking params or www/non-www variants. Add a self-referencing canonical to every page.',
        'Indexability': 'Controls whether Google is allowed to list the page. A "noindex" robots meta tag hides the page from search entirely — only use it on thank-you/admin pages. Make sure your money pages do NOT carry noindex.',
        'Heading Hierarchy': 'H2/H3 subheadings break content into scannable, topic-labelled sections that both readers and AI engines parse. Add 4+ descriptive H2s (and H3s beneath them) instead of a wall of text.',
        'Industry & Sector Pages': 'Dedicated pages like "Accounting Jobs" or "IT Recruitment" capture high-intent searches and are 60-80% of recruiter organic traffic. Build one SEO page per sector you recruit in, each with its own title, copy and a live job feed. Shazamme can auto-generate these.',
        'Sitemap.xml': 'An XML list of all your URLs that search engines use to discover pages. Generate /sitemap.xml automatically and submit it in Google Search Console so new jobs and pages get crawled fast.',
        'AI Crawler Access': 'AI engines (ChatGPT, Perplexity, Gemini, Claude) can only cite you if their crawlers are allowed in robots.txt. Make sure GPTBot, OAI-SearchBot, ClaudeBot, Google-Extended, PerplexityBot and others are not Disallowed. Add a Sitemap: line to robots.txt too.',
        'llms.txt File': 'An emerging standard (/llms.txt) — a plain-text map of your most important content for AI models, like a sitemap for LLMs. Add a markdown file at /llms.txt listing your key pages and a one-line description of each.',
        'llm-info File': 'A structured file (/.well-known/llm-info or /llm-info) telling AI models who you are, what you do, your sectors and locations, so they represent you accurately in generated answers. Add one with your brand facts. Every Shazamme site ships with this.',
        'FAQ & Q&A Schema': 'FAQPage structured data lets your answers appear directly in Google and AI results. Add a real FAQ section (question in a heading, answer below) and wrap it in FAQPage JSON-LD. Target the actual questions candidates and employers ask.',
        'Content Structure': 'AI engines extract answers from well-structured content. Use multiple H2 sections, short paragraphs and bullet lists so each topic is cleanly delimited and machine-readable.',
        'Entity & Authority': 'This is how AI engines confirm WHO you are and whether to trust you. Add Organization schema with sameAs links to your LinkedIn, Crunchbase and (ideally) Wikidata/Wikipedia; publish consistent name/address/phone; add an About/Team page with named people and bios; and display accreditations (REC, APSCo, ISO). These build E-E-A-T and Knowledge-Graph presence.',
        'Content Depth': 'Thin pages give Google and AI engines little to rank or quote. Aim for 800-1,000+ words of genuinely useful, original content on key pages — explain the service, the sector, the process, salaries and FAQs.',
        'AEO / Answer-Engine Readiness': 'Answer Engine Optimisation = shaping content so ChatGPT/Perplexity/AI Overviews quote you. Add a short TL;DR/summary near the top, phrase headings as the questions people ask ("How much do nurses earn in 2026?"), answer in the first sentence under each heading, use comparison tables, cite concrete stats, and show a "last updated" date.',
        'Crawlable Content (JS-render)': 'Most AI crawlers do NOT run JavaScript, so any content (including your job listings) that only appears after client-side rendering is invisible to them. Ensure the important text exists in the raw HTML via server-side rendering, pre-rendering or a static snapshot. Shazamme v2 feed_cache serves a static job snapshot for exactly this.',
        'Mobile Readiness': '60%+ of job searches are on mobile. Add a responsive <meta name="viewport" content="width=device-width, initial-scale=1"> and test on a phone. Modern site builders handle this — confirm the viewport tag is present.',
        'Accessibility (WCAG)': 'Accessible sites reach more candidates and reduce legal risk. Set a lang attribute on <html>, add descriptive alt text to every meaningful image, ensure colour contrast, and provide a skip-to-content link.',
        'Semantic HTML & ARIA': 'Helps assistive tech (and AI parsers) understand your page. Use semantic landmarks (<main>, <nav>, <header>, <footer>), associate every form field with a <label> (or aria-label), write descriptive link text instead of "click here", and keep heading levels in order (don\'t jump H2→H4).',
        'Apply Flow & Job Search': 'The core candidate journey: finding a role and applying. Provide an obvious job search on /job-results and a clear Apply button on /job-detail, and keep the application form short (≤5 fields). If your board is a widget, make sure the rendered apply button is reachable. Shazamme delivers a mobile-first, ATS-integrated apply flow.',
        'ATS Platform Detection': 'An Applicant Tracking System is needed for candidates to actually complete and for you to manage applications. Integrate your ATS (Bullhorn, JobAdder, Greenhouse, etc.) so applies flow into your hiring pipeline instead of an email inbox.',
        'Navigation & Structure': 'Clear navigation helps users and search engines. Use a semantic <nav> element, a logical menu, and breadcrumbs on deep pages so people (and crawlers) always know where they are.',
        'HTTPS Trust Signal': 'Without HTTPS, browsers show a "Not Secure" warning that scares candidates off. Install a free SSL certificate (Let\'s Encrypt or via your host) and force https:// site-wide.',
        'HTTPS / SSL': 'Without HTTPS, browsers warn visitors and Google ranks you lower. Install an SSL certificate (free via Let\'s Encrypt or your host) and redirect all http:// traffic to https://.',
        'Page Speed (TTFB)': 'Time To First Byte measures server responsiveness; over ~1s loses visitors. Use caching, a CDN and a fast host. (See Core Web Vitals / Lighthouse for the full picture.)',
        'Server Response (TTFB)': 'Time To First Byte is how fast your server starts replying. Target under 0.5s with server caching, a CDN and a performant host/plan.',
        'Form Usability': 'Long, unfriendly forms kill conversions. Add <label>s, use the autocomplete attribute (e.g. autocomplete="email"), the right input types, and remove non-essential fields.',
        'Core Web Vitals': 'Google\'s real-user experience metrics: LCP (loading, target <2.5s), INP (responsiveness, <200ms) and CLS (visual stability, <0.1). Improve by optimising/compressing images, reserving space for elements to stop layout shift, and reducing heavy JavaScript. Data here comes from Chrome users (CrUX) or a Lighthouse lab run.',
        'Lighthouse Performance': 'Google Lighthouse\'s 0-100 mobile performance score from a fully-rendered test. Below 50 is poor. Fix the biggest offenders: large images, render-blocking scripts, and unused JS/CSS. Run pagespeed.web.dev for the itemised opportunities.',
        'Content Compression': 'Compressing responses (gzip/Brotli) cuts page weight 60-80% for faster loads. Enable Brotli or gzip at your server/CDN — usually a one-line config or an on/off toggle.',
        'Caching Strategy': 'Caching headers let browsers and CDNs reuse assets instead of re-downloading. Set Cache-Control with long max-age on static assets and enable ETag/Last-Modified.',
        'Resource Hints': 'preconnect and preload hints tell the browser to fetch critical resources early. Add <link rel="preconnect"> for third-party origins (fonts, analytics) and preload your hero font/image.',
        'Render-Blocking Scripts': 'Scripts without defer/async block the page from showing. Add defer or async to non-critical <script> tags, or load them as type="module", so content paints sooner.',
        'HTTP/2+': 'HTTP/2 or HTTP/3 multiplexes requests for faster loads. Most modern hosts/CDNs (Cloudflare, etc.) enable it automatically — switch it on if your host offers it.',
        'Analytics & Tracking': 'Without analytics you are blind to what works. Install GA4 (or a privacy-friendly tool like Plausible) plus conversion tracking on applies and enquiries.',
        'Call-to-Action Strength': 'CTAs tell visitors what to do next. Add clear, repeated, action-led buttons ("Apply Now", "Search Jobs", "Upload CV") in the hero and throughout the page.',
        'Job Alerts & Lead Capture': 'Most candidates are passive. Offer email job alerts / a saved-search signup so you can re-engage them automatically when a matching role is posted.',
        'Lead Capture / Newsletter': 'Capture interested visitors before they leave. Add a newsletter or alert signup with a single email field so you can nurture them later.',
        'Live Chat & Chatbot': 'Chat reduces drop-off and answers questions instantly. Add a chatbot (or live chat) that handles FAQs and guides applicants 24/7. Shazamme includes an AI recruitment chatbot.',
        'Job Search & Filters': 'Candidates expect instant, filterable search. Provide a keyword search plus filters for location, sector, salary and job type on your results page.',
        'Search Functionality': 'A site search helps visitors find content fast and they convert at higher rates. Add a search box wired to your content/jobs.',
        'Social Sharing & Referrals': 'Make it one tap for candidates to share or refer a role. Add share buttons (LinkedIn, WhatsApp, email) and a "refer a friend" option on job pages.',
        'Social Links & Sharing': 'Link your social profiles and add share buttons so visitors can follow and spread your content.',
        'Social Proof & Reviews': 'Candidates trust peer signals. Add testimonials, Google/Glassdoor ratings, client logos and placement stats to build credibility.',
        'Video Content': 'Video lifts application intent ~34%. Embed a culture/team or "day in the life" video (YouTube/Vimeo) on key pages.',
        'EVP & Pay Transparency': 'Your Employee Value Proposition. Show salary ranges, benefits, flexibility and career growth — 67% of candidates research pay before applying, and transparency wins better-fit applicants.',
        'Visual Brand Assets': 'A consistent visual identity builds trust. Add a favicon and a branded og:image, and use consistent logo/colours across pages.',
        'Social Presence': 'Linked, active social profiles extend reach and reassure candidates. Link LinkedIn, Instagram, YouTube etc. in the footer.',
        'DE&I Commitment': 'Diversity, Equity & Inclusion content matters to many candidates. For employer/career sites, publish a DE&I statement or page. (Not scored for recruitment-agency sites, where it rarely appears.)',
        'Culture & Team Content': 'Candidates want to see who they\'ll work with. Add culture, "meet the team" and "life at" content with real photos and stories.',
        'Employee Stories & Testimonials': 'Authentic employee voices are the #1 credibility factor on career sites. Add quotes, written stories or short videos from current staff.',
        'Strict-Transport-Security': 'The HSTS header forces browsers to always use HTTPS, blocking downgrade attacks. Add: Strict-Transport-Security: max-age=31536000; includeSubDomains.',
        'Content-Security-Policy': 'CSP restricts what scripts can run, mitigating XSS. Add a Content-Security-Policy header — start in report-only mode, then enforce once tuned.',
        'X-Content-Type-Options': 'Add the header X-Content-Type-Options: nosniff to stop browsers MIME-sniffing responses into a different (riskier) type.',
        'X-Frame-Options': 'Prevents clickjacking by stopping your site being embedded in iframes. Add X-Frame-Options: SAMEORIGIN (or a CSP frame-ancestors rule).',
        'Referrer-Policy': 'Controls how much referrer info leaks to other sites. Add Referrer-Policy: strict-origin-when-cross-origin.',
        'Permissions-Policy': 'Restricts powerful browser features (camera, geolocation). Add a Permissions-Policy header disabling features you don\'t use.',
        'Privacy & Cookie Policy': 'Required by GDPR/CCPA. Publish a privacy policy and (if you use non-essential cookies) a consent banner, and link them in the footer.',
        'Mixed Content': 'HTTP resources on an HTTPS page trigger browser warnings and break the padlock. Update all image/script/style URLs to https://.',
        'Image Optimization': 'Heavy, unsized images slow pages and cause layout shift. Serve WebP/AVIF, set explicit width/height, and add loading="lazy" to below-the-fold images.',
        'Image Alt Text': 'Alt text describes images for screen readers and search engines. Add concise, descriptive alt to every meaningful image (decorative ones can be alt="").',
        'Image Dimensions': 'Setting explicit width and height on images reserves space and prevents layout shift (CLS). Add the attributes to all images.',
        'Font Loading': 'Web fonts can cause invisible text while loading. Add font-display: swap and preload your primary font file.',
        'Structured Content': 'Lists and tables make content scannable and machine-extractable. Use <ul>/<ol> and <table> where you have steps, options or comparisons.',
        'Internal Linking': 'Internal links spread ranking strength and help discovery. Link related pages (sectors, locations, guides) with descriptive anchor text.',
        'External Links': 'Linking out to authoritative sources adds credibility and context. Cite reputable references where relevant (open in a new tab).',
        'FAQ Content': 'An FAQ answers buyer/candidate questions and feeds AI answers. Add a real FAQ section and mark it up with FAQPage schema.',
        'Heading Structure': 'Use a logical H2/H3 outline so readers and crawlers can follow the content. Add descriptive subheadings instead of long unbroken text.',
    }

    def _attach_guidance(self, pillar_result: Dict):
        for check in pillar_result.get('checks', []):
            if check.get('status') in ('warn', 'fail') and check['name'] in self.GUIDANCE:
                check['help'] = self.GUIDANCE[check['name']]

    def _generate_executive_summary(self, pillars: Dict, overall: int,
                                    recommendations: List[Dict]) -> Dict:
        ranked = sorted(pillars.values(), key=lambda p: p['score'])
        weakest = ranked[:2]
        strongest = [p['name'] for p in reversed(ranked) if p['score'] >= 75][:3]
        critical = [r for r in recommendations if r['priority'] == 'critical']
        top_source = critical if len(critical) >= 3 else recommendations
        opportunities = []
        for r in top_source[:3]:
            opportunities.append({
                'title': r['check'],
                'pillar': r['pillar'],
                'why_it_matters': r['impact'],
                'how_to_fix': self.GUIDANCE.get(r['check'], r['detail']),
            })

        if overall >= 85:
            band = 'a market-leading'
            verdict_lead = 'This site is already performing strongly'
        elif overall >= 70:
            band = 'a solid but improvable'
            verdict_lead = 'Good foundations are in place'
        elif overall >= 55:
            band = 'an underperforming'
            verdict_lead = 'There is meaningful value being left on the table'
        else:
            band = 'a high-risk, underperforming'
            verdict_lead = 'This site is losing candidates and visibility every day'

        weak_names = ' and '.join(p['name'] for p in weakest)
        mode_obj = {'recruitment': 'attract candidates and win clients',
                    'career_site': 'attract and convert talent',
                    'general': 'attract and convert visitors'}.get(self.mode, 'perform')

        verdict = (f"{verdict_lead}. Scoring {overall}/100, this is {band} site for its ability to "
                   f"{mode_obj}. The biggest drag is {weak_names}; closing the {len(critical)} "
                   f"critical gap(s) below would move the score and the commercial result fastest.")

        return {
            'headline': f'{overall}/100 — {self._grade_label(overall)}',
            'verdict': verdict,
            'priority_count': len(critical),
            'top_opportunities': opportunities,
            'strengths': strongest,
            'weakest_pillars': [{'name': p['name'], 'score': p['score']} for p in weakest],
        }

    def _generate_recommendations(self, pillars: Dict) -> List[Dict]:
        IMPACT_RECRUITMENT = {
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
            'Entity & Authority': 'sameAs/Wikidata/LinkedIn links + NAP let AI engines reliably identify and cite your brand in the Knowledge Graph',
            'Industry & Sector Pages': 'Pages like "accounting jobs" and "IT recruitment" capture high-intent keyword searches — typically 60-80% of recruiter organic traffic',
            'Indexability': 'If noindex is set, your page will not appear in any search results',
            'Canonical URL': 'Prevents duplicate content from splitting your ranking signals',
            'Structured Data Validity': 'Valid JobPosting fields are required for Google Jobs eligibility — invalid markup gets dropped',
            'AEO / Answer-Engine Readiness': 'Summary blocks, question headings and quotable stats are what ChatGPT, Perplexity and AI Overviews actually cite',
            'Crawlable Content (JS-render)': 'AI crawlers rarely run JavaScript — content that only renders client-side is invisible to them',
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
            'Entity & Authority': 'sameAs/Wikidata/LinkedIn links + named people let AI engines identify and trust your employer brand',
            'Structured Data Validity': 'Valid JobPosting fields are required for Google Jobs eligibility',
            'AEO / Answer-Engine Readiness': 'Summary blocks, question headings and quotable stats are what AI engines cite',
            'Crawlable Content (JS-render)': 'AI crawlers rarely run JavaScript — client-rendered content is invisible to them',
            'AI Crawler Access': 'Required for ChatGPT, Perplexity, Gemini and Claude to surface your roles',
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
            default_impact = 'Affects overall candidate experience and talent attraction'
        elif self.mode == 'general':
            IMPACT = IMPACT_GENERAL
            default_impact = 'Affects overall site performance'
        else:
            IMPACT = IMPACT_RECRUITMENT
            default_impact = 'Affects overall talent attraction performance'

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
                        'impact': IMPACT.get(check['name'], default_impact),
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
            if self.mode == 'career_site':
                features.append({
                    'gap': 'Weak or broken application flow',
                    'feature': 'Streamlined Career Application Flow',
                    'description': 'Shazamme career sites deliver a seamless, mobile-first application experience — integrated with your ATS and optimised to minimise drop-off at every step.',
                    'icon': 'search',
                    'stat': 'Up to 60% more completed applications',
                })
            else:
                features.append({
                    'gap': 'No job search or weak apply flow',
                    'feature': 'AI-Powered Job Search & One-Click Apply',
                    'description': 'Instant AI-matched job search, smart filters by location/salary/type, and a streamlined mobile-first apply flow — all built in.',
                    'icon': 'search',
                    'stat': 'Up to 60% more completed applications',
                })

        # Support both check name variants across modes
        alerts_check = (
            all_checks.get('Job Alerts & Lead Capture')
            or all_checks.get('Lead Capture / Newsletter')
        )
        if alerts_check != 'pass':
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

        if self.mode == 'recruitment' and all_checks.get('Industry & Sector Pages') != 'pass':
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
