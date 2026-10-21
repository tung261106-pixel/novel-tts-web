# File: core/scraper.py
import re
import json
import random
from typing import Tuple, List, Optional
from urllib.parse import urljoin, quote
import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

try:
    import cloudscraper
except ImportError:
    cloudscraper = None


class NovelScraper:
    """
    Bộ bóc tách nội dung truyện chữ đa tầng:
    - Chống lỗi 403 & lọc sạch bẫy chữ tượng hình (Honeypot).
    - Tự động chuyển đổi sang Kho Truyện Dự Phòng (Mirror Sites) hoặc Headless Chrome (Jina)
      khi trang nguồn bật chế độ tráo chữ tượng hình chống bot.
    """

    USER_AGENTS = [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
        ),
    ]

    CONTENT_SELECTORS = [
        "#chapter-c",
        ".chapter-c",
        "#vungdoc",
        ".box-chap",
        "#chapter-content",
        ".chapter-content",
        "#content",
        ".entry-content",
        "article",
    ]

    JUNK_PATTERNS = [
        r"(?i)^\s*(chương\s+trước|chương\s+sau|chương\s+tiếp|mục\s+lục|báo\s+lỗi\s+chương|bình\s+luận)\s*$",
        r"(?i)^\s*(bản\s*dịch|dịch\s*ai|convert|\s|/|\|)+$",
        r"(?i)hãy\s+bấm\s+vào\s+đây\s+để\s+báo\s+lỗi.*",
        r"(?i)nguồn\s*:\s*http.*",
        r"(?i)truyện\s+được\s+đăng\s+tại.*",
        r"(?i)quảng\s+cáo.*",
    ]

    # Nhận diện nguyên âm có dấu đặc trưng của Tiếng Việt
    VN_ACCENT_REGEX = re.compile(
        r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ"
        r"ÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ]"
    )

    # Chỉ giữ lại chữ cái Tiếng Việt, chữ số và dấu câu văn học hợp lệ
    DISALLOWED_CHARS_REGEX = re.compile(
        r"[^a-zA-Z0-9\s.,!?;:'\"()\[\]\-–—…“”‘’/\\+%&\u00C0-\u024F\u1E00-\u1EFF]"
    )

    _cover_cache = {}
    _working_mirror_cache = {}
    _cffi_session = None
    _req_session = None

    @staticmethod
    def _is_alive_tag(el) -> bool:
        """Kiểm tra thẻ BeautifulSoup còn tồn tại và chưa bị decompose (tránh lỗi NoneType)."""
        return (
            el is not None
            and not getattr(el, "decomposed", False)
            and isinstance(getattr(el, "attrs", None), dict)
        )

    @classmethod
    def _count_vn_accents(cls, text: str) -> int:
        """Đếm số lượng ký tự Tiếng Việt có dấu trong chuỗi."""
        if not text or not isinstance(text, str):
            return 0
        return len(cls.VN_ACCENT_REGEX.findall(text))

    @classmethod
    def _is_trap_or_garbled_line(cls, line: str) -> bool:
        """Kiểm tra xem một dòng có phải là bẫy chữ tượng hình hoặc chuỗi ký tự rác hay không."""
        if not line or not isinstance(line, str):
            return True

        if line.count("_") >= 3:
            return True

        cleaned = cls.DISALLOWED_CHARS_REGEX.sub("", line)
        if len(line) > 10 and (len(line) - len(cleaned)) / len(line) > 0.20:
            return True

        if len(cleaned) > 45 and cls._count_vn_accents(cleaned) < 2:
            return True

        return False

    @classmethod
    def _get_headers(cls, url: str = "") -> dict:
        headers = {
            "User-Agent": random.choice(cls.USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        }
        if url:
            m = re.match(r"^(https?://[^/]+)", url)
            headers["Referer"] = m.group(1) + "/" if m else "https://www.google.com.vn/"
        return headers

    @classmethod
    def _has_valid_vietnamese_chapter(cls, html: str) -> bool:
        """Kiểm tra mã HTML tải về thực sự có nội dung chương truyện Tiếng Việt."""
        if not html or not isinstance(html, str) or len(html) < 600:
            return False

        lower_html = html[:4000].lower()
        cloudflare_markers = [
            "<title>just a moment...</title>",
            "<title>attention required! | cloudflare</title>",
            "cf-browser-verification",
            "challenge-platform",
            "enable javascript and cookies to continue",
            "access denied |",
        ]
        if any(marker in lower_html for marker in cloudflare_markers):
            return False

        text_only = re.sub(r"(?is)<(script|style|head|nav|footer).*?>.*?</\1>", " ", html)
        text_only = re.sub(r"(?s)<[^>]+>", " ", text_only)
        text_only = cls.DISALLOWED_CHARS_REGEX.sub("", text_only)

        if cls._count_vn_accents(text_only) >= 60:
            return True

        return False

    @classmethod
    def _raw_http_get(cls, url: str, timeout: int = 15) -> str:
        """Tải HTML từ 1 URL qua curl_cffi, cloudscraper hoặc requests."""
        headers = cls._get_headers(url)

        if cffi_requests is not None:
            if cls._cffi_session is None:
                cls._cffi_session = cffi_requests.Session()
            for browser_fp in ["chrome120", "safari17_0", "edge101"]:
                try:
                    resp = cls._cffi_session.get(
                        url,
                        headers=headers,
                        impersonate=browser_fp,
                        timeout=timeout,
                    )
                    if resp.status_code == 200 and resp.text and len(resp.text) > 600:
                        return resp.text
                except Exception:
                    continue

        if cloudscraper is not None:
            try:
                scraper = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows", "mobile": False}
                )
                resp = scraper.get(url, headers=headers, timeout=timeout)
                if resp.status_code == 200 and resp.text and len(resp.text) > 600:
                    resp.encoding = resp.apparent_encoding or "utf-8"
                    return resp.text
            except Exception:
                pass

        try:
            if cls._req_session is None:
                cls._req_session = requests.Session()
            resp = cls._req_session.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200 and resp.text and len(resp.text) > 600:
                resp.encoding = resp.apparent_encoding or "utf-8"
                return resp.text
        except Exception:
            pass

        return ""

    @classmethod
    def _build_mirror_urls(cls, original_url: str) -> Tuple[str, List[str]]:
        """
        Tách tên truyện (slug) và số chương từ URL gốc để tạo danh sách link
        Kho Truyện Dự Phòng (truyenfull.vision, truyenfull.io, truyenfull.vn, sstruyen.vn...).
        """
        m = re.search(
            r"/(?:doc-truyen/|truyen/)?([a-zA-Z0-9-]+)/(?:chuong|chapter)-(\d+)",
            original_url,
            re.I,
        )
        if not m:
            return "", []

        novel_slug = m.group(1).lower().strip()
        chap_num = m.group(2).strip()

        mirror_Auto_List = [
            f"https://truyenfull.vision/{novel_slug}/chuong-{chap_num}/",
            f"https://truyenfull.io/{novel_slug}/chuong-{chap_num}/",
            f"https://truyenfull.vn/{novel_slug}/chuong-{chap_num}/",
            f"https://truyenfull.bio/{novel_slug}/chuong-{chap_num}/",
            f"https://sstruyen.vn/{novel_slug}/chuong-{chap_num}/",
        ]

        # Nếu trước đó đã tìm được kho dự phòng nhanh nhất cho truyện này thì đưa lên đầu danh sách
        preferred_domain = cls._working_mirror_cache.get(novel_slug)
        if preferred_domain:
            preferred_url = f"{preferred_domain}/{novel_slug}/chuong-{chap_num}/"
            if preferred_url in mirror_Auto_List:
                mirror_Auto_List.remove(preferred_url)
                mirror_Auto_List.insert(0, preferred_url)

        return novel_slug, mirror_Auto_List

    @classmethod
    def _compute_fallback_nav_links(cls, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Tính toán chính xác link Chương Trước & Chương Tiếp theo cấu trúc URL gốc của người dùng."""
        prev_url: Optional[str] = None
        next_url: Optional[str] = None
        match = re.search(r"(chuong-|chapter-)(\d+)([^/\d]*)$", url, re.I)
        if match:
            prefix, num_str, suffix = match.groups()
            chap_num = int(num_str)
            base_prefix = url[: match.start()]
            next_url = f"{base_prefix}{prefix}{chap_num + 1}{suffix}"
            if chap_num > 1:
                prev_url = f"{base_prefix}{prefix}{chap_num - 1}{suffix}"
        return prev_url, next_url

    @classmethod
    def _fetch_via_jina_headless(cls, url: str, timeout: int = 18) -> Optional[Tuple[str, List[str]]]:
        """Sử dụng trình duyệt ảo Jina Headless Chrome để lấy văn bản đã giải mã JavaScript."""
        jina_url = f"https://r.jina.ai/{url}"
        try:
            headers = {
                "User-Agent": random.choice(cls.USER_AGENTS),
                "Accept": "text/plain",
                "X-No-Cache": "true",
            }
            resp = requests.get(jina_url, headers=headers, timeout=timeout)
            if resp.status_code != 200 or not resp.text:
                return None

            raw_text = resp.text
            title = "Chương truyện"
            m_title = re.search(r"^Title:\s*(.+)$", raw_text, re.M)
            if m_title:
                title = re.split(r"\s*[-|]\s*", m_title.group(1).strip())[0].strip()

            # Cắt bỏ phần header của Jina
            if "Markdown Content:" in raw_text:
                raw_text = raw_text.split("Markdown Content:", 1)[1]

            cleaned = cls._clean_text(raw_text)
            if cls._count_vn_accents(cleaned) >= 60:
                chunks = cls.split_into_chunks(cleaned, max_chars=450)
                if chunks:
                    if not chunks[0].lower().startswith(title.lower()):
                        chunks.insert(0, f"{title}.")
                    return title, chunks
        except Exception:
            pass
        return None

    @classmethod
    def _extract_cover_from_soup(cls, soup: BeautifulSoup, base_url: str) -> str:
        """Tìm link ảnh bìa từ thẻ meta og:image hoặc các class ảnh bìa phổ biến."""
        try:
            cover_selectors = [
                ".book-img img",
                ".books .book img",
                ".info-holder .books img",
                ".book-cover img",
                ".novel-cover img",
                ".thumb img",
                "img.cover",
                "img[alt*='bìa']",
            ]
            for sel in cover_selectors:
                img = soup.select_one(sel)
                if cls._is_alive_tag(img):
                    src = str(img.attrs.get("data-src") or img.attrs.get("src") or "").strip()
                    if src and not src.startswith("data:"):
                        return urljoin(base_url, src)

            for meta_sel in [
                'meta[property="og:image"]',
                'meta[name="og:image"]',
                'meta[name="twitter:image"]',
                'link[rel="image_src"]',
            ]:
                tag = soup.select_one(meta_sel)
                if cls._is_alive_tag(tag):
                    content = str(tag.attrs.get("content") or tag.attrs.get("href") or "").strip()
                    if content and not any(x in content.lower() for x in ("logo", "favicon", "default-share")):
                        return urljoin(base_url, content)
        except Exception:
            pass
        return ""

    @classmethod
    def extract_novel_cover(cls, chapter_soup: Optional[BeautifulSoup], chapter_url: str) -> str:
        """Trích xuất ảnh bìa và lưu đệm theo tên truyện."""
        novel_base_url = re.sub(r"/(?:chuong|chapter)-\d+.*$", "", chapter_url, flags=re.I)
        if novel_base_url in cls._cover_cache and cls._cover_cache[novel_base_url]:
            return cls._cover_cache[novel_base_url]

        cover_url = ""
        if chapter_soup is not None:
            cover_url = cls._extract_cover_from_soup(chapter_soup, chapter_url)

        if not cover_url and novel_base_url != chapter_url:
            try:
                novel_html = cls._raw_http_get(novel_base_url, timeout=6)
                if novel_html:
                    novel_soup = BeautifulSoup(novel_html, "lxml")
                    cover_url = cls._extract_cover_from_soup(novel_soup, novel_base_url)
            except Exception:
                pass

        if cover_url:
            cls._cover_cache[novel_base_url] = cover_url
        return cover_url

    @classmethod
    def _extract_dom_chapter(cls, html_text: str, url: str) -> Optional[Tuple[str, List[str], Optional[str], Optional[str], str]]:
        """
        Thử phân tích HTML thành (title, chunks, prev_url, next_url, cover_url).
        Trả về None nếu trang HTML bị dính bẫy chữ tượng hình (để kích hoạt kho truyện dự phòng).
        """
        if not html_text or len(html_text) < 500:
            return None

        soup = BeautifulSoup(html_text, "lxml")
        prev_url, next_url = cls._extract_nav_links(soup, url)
        cover_url = cls.extract_novel_cover(soup, url)

        for tag in list(soup(["script", "style", "nav", "footer", "iframe", "noscript", "aside", "form", "svg", "canvas"])):
            if cls._is_alive_tag(tag):
                tag.decompose()

        title = cls._extract_chapter_title(soup, url)

        for tag in list(soup(["header", "button", "h1"])):
            if cls._is_alive_tag(tag):
                tag.decompose()

        for junk_div in list(
            soup.find_all(
                attrs={
                    "class": re.compile(r"(ads|advert|banner|nav|pagination|comment|social|breadcrumb)", re.I)
                }
            )
        ):
            if cls._is_alive_tag(junk_div):
                junk_div.decompose()

        for el in list(soup.find_all(["span", "div", "a", "li"])):
            if not cls._is_alive_tag(el):
                continue
            el_text = el.get_text(strip=True)
            if re.match(r"(?i)^(bản\s*dịch|dịch\s*ai|convert|bản\s*dịch\s*dịch\s*ai\s*convert)$", el_text):
                el.decompose()

        # Tìm khung chứa nội dung chương có mật độ chữ Tiếng Việt hợp lệ
        content_node = None
        for selector in cls.CONTENT_SELECTORS:
            node = soup.select_one(selector)
            if cls._is_alive_tag(node):
                raw_node_txt = cls.DISALLOWED_CHARS_REGEX.sub("", node.get_text(" ", strip=True))
                if cls._count_vn_accents(raw_node_txt) >= 60:
                    content_node = node
                    break

        if not content_node:
            candidates = []
            for div in soup.find_all(["article", "main", "section", "div"]):
                if not cls._is_alive_tag(div):
                    continue
                txt = cls.DISALLOWED_CHARS_REGEX.sub("", div.get_text(" ", strip=True))
                vn_score = cls._count_vn_accents(txt)
                if vn_score >= 60:
                    candidates.append((vn_score, len(txt), div))

            if candidates:
                max_vn_score = max(c[0] for c in candidates)
                best_candidates = [c for c in candidates if c[0] >= max_vn_score * 0.85]
                best_candidates.sort(key=lambda x: x[1])
                content_node = best_candidates[0][2]

        if not content_node or not cls._is_alive_tag(content_node):
            return None

        for br in list(content_node.find_all("br")):
            if cls._is_alive_tag(br):
                br.replace_with("\n")
        for block_tag in list(content_node.find_all(["p", "div", "h2", "h3", "h4", "li"])):
            if cls._is_alive_tag(block_tag):
                block_tag.append("\n")

        cleaned_text = cls._clean_text(content_node.get_text())
        if len(cleaned_text) < 50 or cls._count_vn_accents(cleaned_text) < 50:
            return None

        chunks = cls.split_into_chunks(cleaned_text, max_chars=450)
        if chunks:
            first_chunk_lower = chunks[0].lower().strip()
            title_lower = title.lower().strip()
            if not first_chunk_lower.startswith(title_lower):
                chunks.insert(0, f"{title}.")

        return title, chunks, prev_url, next_url, cover_url

    @classmethod
    def _fetch_from_mirrors_or_jina(cls, original_url: str) -> Tuple[str, List[str], Optional[str], Optional[str], str]:
        """
        Khi trang nguồn (như truyendich.space) bật bẫy chữ tượng hình hoặc chặn 403,
        tự động lấy nội dung chương đó từ Kho Truyện Dự Phòng (TruyenFull / SSTruyen) hoặc Jina Chrome.
        """
        fallback_prev, fallback_next = cls._compute_fallback_nav_links(original_url)
        cover_url = cls.extract_novel_cover(None, original_url)

        # 1. Tự động lấy từ các Kho Truyện Dự Phòng theo đúng tên truyện và số chương
        novel_slug, mirror_urls = cls._build_mirror_urls(original_url)
        for m_url in mirror_urls:
            html = cls._raw_http_get(m_url, timeout=12)
            if not html or not cls._has_valid_vietnamese_chapter(html):
                continue

            parsed = cls._extract_dom_chapter(html, m_url)
            if parsed is not None:
                m_title, m_chunks, m_prev, m_next, m_cover = parsed
                # Lưu lại tên miền kho dự phòng tốt nhất cho bộ truyện này
                domain_match = re.match(r"^(https?://[^/]+)", m_url)
                if domain_match and novel_slug:
                    cls._working_mirror_cache[novel_slug] = domain_match.group(1)

                final_cover = cover_url or m_cover
                if final_cover and novel_slug:
                    novel_base = re.sub(r"/(?:chuong|chapter)-\d+.*$", "", original_url, flags=re.I)
                    cls._cover_cache[novel_base] = final_cover

                return (
                    m_title,
                    m_chunks,
                    fallback_prev or m_prev,
                    fallback_next or m_next,
                    final_cover,
                )

        # 2. Thử qua trình duyệt ảo Jina Headless Chrome
        jina_res = cls._fetch_via_jina_headless(original_url, timeout=18)
        if jina_res is not None:
            j_title, j_chunks = jina_res
            return j_title, j_chunks, fallback_prev, fallback_next, cover_url

        raise ValueError(
            f"Không thể tải chương truyện từ cả trang gốc lẫn kho dự phòng cho URL: {original_url}"
        )

    @classmethod
    def parse_chapter_html(cls, html_text: str, url: str) -> Tuple[str, List[str], Optional[str], Optional[str], str]:
        """
        Phân tích mã HTML do trình duyệt gửi lên. Nếu mã HTML bị dính bẫy chữ tượng hình,
        tự động chuyển sang lấy từ Kho Truyện Dự Phòng mà không báo lỗi.
        """
        parsed = cls._extract_dom_chapter(html_text, url)
        if parsed is not None:
            return parsed
        return cls._fetch_from_mirrors_or_jina(url)

    @classmethod
    def fetch_chapter(cls, url: str, timeout: int = 20) -> Tuple[str, List[str], Optional[str], Optional[str], str]:
        """
        Tải trang web và bóc tách nội dung chương truyện.
        Nếu trang gốc đã từng bật bẫy chữ tượng hình cho truyện này, ưu tiên kiểm tra kho nhanh nhất.
        """
        novel_slug, _ = cls._build_mirror_urls(url)

        # Nếu bộ truyện này đang bị trang gốc chặn/tráo chữ tượng hình -> Lấy thẳng từ kho dự phòng đã lưu
        if novel_slug and novel_slug in cls._working_mirror_cache:
            try:
                return cls._fetch_from_mirrors_or_jina(url)
            except Exception:
                pass

        # Thử tải trực tiếp từ URL gốc
        html_text = cls._raw_http_get(url, timeout=12)
        if html_text and cls._has_valid_vietnamese_chapter(html_text):
            parsed = cls._extract_dom_chapter(html_text, url)
            if parsed is not None:
                return parsed

        # Tự động chuyển sang Kho Truyện Dự Phòng (TruyenFull / SSTruyen / Jina)
        return cls._fetch_from_mirrors_or_jina(url)

    @classmethod
    def _extract_chapter_title(cls, soup: BeautifulSoup, url: str) -> str:
        chap_num = ""
        if soup.title and soup.title.get_text(strip=True):
            m = re.search(r"(?i)(chương\s+\d+)", soup.title.get_text(strip=True))
            if m:
                chap_num = m.group(1).title()

        if not chap_num:
            m_url = re.search(r"(?:chuong|chapter)-(\d+)", url, re.I)
            if m_url:
                chap_num = f"Chương {m_url.group(1)}"

        chap_name = ""
        # Ưu tiên lấy .chapter-title trước h1 để tránh lấy nhầm tên bộ truyện trên các trang Mirror
        title_selectors = [
            ".chapter-title",
            "#chapter-title",
            ".chap-title",
            "h2.chapter-title",
            "article h1",
            "main h1",
            "header h1",
            "h1",
        ]
        for sel in title_selectors:
            node = soup.select_one(sel)
            if cls._is_alive_tag(node):
                text = re.sub(r"\s+", " ", node.get_text(strip=True))
                if text and len(text) > 1 and not cls._is_trap_or_garbled_line(text):
                    chap_name = text
                    break

        if chap_name:
            if re.search(r"(?i)^chương\s+\d+", chap_name):
                return chap_name
            elif chap_num:
                return f"{chap_num}: {chap_name}"
            else:
                return chap_name

        if chap_num:
            return chap_num

        if soup.title and soup.title.get_text(strip=True):
            raw_title = soup.title.get_text(strip=True)
            return re.split(r"\s*[|]\s*", raw_title)[0].strip()

        return "Chương truyện"

    @classmethod
    def _extract_nav_links(cls, soup: BeautifulSoup, base_url: str) -> Tuple[Optional[str], Optional[str]]:
        prev_url: Optional[str] = None
        next_url: Optional[str] = None

        def _is_valid_href(tag) -> Optional[str]:
            if not cls._is_alive_tag(tag):
                return None
            attrs = tag.attrs
            raw_class = attrs.get("class") or []
            if isinstance(raw_class, (list, tuple)):
                classes = " ".join(str(c) for c in raw_class).lower()
            elif isinstance(raw_class, str):
                classes = raw_class.lower()
            else:
                classes = ""

            if "disabled" in classes or "isdisabled" in classes or "disabled" in attrs:
                return None
            href = str(attrs.get("href") or "").strip()
            if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                return None
            return urljoin(base_url, href)

        prev_selectors = ["#prev_chap", "a.prev-chap", "a.btn-prev", "a[rel='prev']"]
        next_selectors = ["#next_chap", "a.next-chap", "a.btn-next", "a[rel='next']"]

        for sel in prev_selectors:
            link = _is_valid_href(soup.select_one(sel))
            if link:
                prev_url = link
                break

        for sel in next_selectors:
            link = _is_valid_href(soup.select_one(sel))
            if link:
                next_url = link
                break

        if not prev_url or not next_url:
            for a_tag in soup.find_all("a", href=True):
                valid_link = _is_valid_href(a_tag)
                if not valid_link:
                    continue

                text = a_tag.get_text(" ", strip=True).lower()
                title_attr = str(a_tag.attrs.get("title") or "").lower()
                combined = f"{text} {title_attr}"

                if not next_url and re.search(r"(chương\s+tiếp|chương\s+sau|tiếp\s+theo|next\s+chapter)", combined):
                    next_url = valid_link
                elif not prev_url and re.search(r"(chương\s+trước|trước\s+đó|prev\s+chapter)", combined):
                    prev_url = valid_link

        if not next_url or not prev_url:
            fallback_prev, fallback_next = cls._compute_fallback_nav_links(base_url)
            prev_url = prev_url or fallback_prev
            next_url = next_url or fallback_next

        return prev_url, next_url

    @classmethod
    def _clean_text(cls, text: str) -> str:
        """Làm sạch từng dòng văn bản, loại bỏ mọi dòng chữ tượng hình hoặc ký tự lạ."""
        if not text or not isinstance(text, str):
            return ""

        lines = text.splitlines()
        cleaned_lines = []

        for raw_line in lines:
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            if cls._is_trap_or_garbled_line(raw_line):
                continue

            line = cls.DISALLOWED_CHARS_REGEX.sub("", raw_line)
            line = re.sub(r"\s+", " ", line).strip()
            if not line:
                continue

            line = re.sub(
                r"(?i)^(?:bản\s*dịch\s*dịch\s*ai\s*convert|bản\s*dịch\s+dịch\s*ai|dịch\s*ai\s*convert)\s*",
                "",
                line,
            ).strip()

            if not line:
                continue

            is_junk = any(re.search(pattern, line) for pattern in cls.JUNK_PATTERNS)
            if not is_junk:
                cleaned_lines.append(line)

        return "\n".join(cleaned_lines)

    @classmethod
    def split_into_chunks(cls, text: str, max_chars: int = 450) -> List[str]:
        sentences = re.split(r"(?<=[.!?…])\s+|\n+", text)
        chunks: List[str] = []
        current_chunk = ""

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            if len(sentence) > max_chars:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                sub_parts = re.split(r"(?<=[,;:])\s+", sentence)
                for part in sub_parts:
                    if len(current_chunk) + len(part) + 1 <= max_chars:
                        current_chunk = f"{current_chunk} {part}".strip()
                    else:
                        if current_chunk:
                            chunks.append(current_chunk.strip())
                        current_chunk = part
                continue

            if len(current_chunk) + len(sentence) + 1 <= max_chars:
                current_chunk = f"{current_chunk} {sentence}".strip()
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = sentence

        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks
