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
    - Chặn đứng màn hình tường lửa 'Đang kiểm tra linh căn đạo hữu' & bẫy chữ tượng hình.
    - Tự động chuyển hướng sang hệ thống Kho Truyện Dự Phòng (TruyenFull / SSTruyen / Jina)
      để lấy toàn bộ nội dung chương truyện Tiếng Việt chuẩn xác.
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
        ".vung-doc",
        "#chapt-content",
        ".content.container1",
        ".box-chap",
        "#chapter-content",
        ".chapter-content",
        "#content",
        ".entry-content",
        "article",
    ]

    # Các mẫu câu rác giao diện và màn hình chặn Bot của truyendich.space
    JUNK_PATTERNS = [
        r"(?i)^\s*(chương\s+trước|chương\s+sau|chương\s+tiếp|mục\s+lục|báo\s+lỗi\s+chương|bình\s+luận)\s*$",
        r"(?i)^\s*(bản\s*dịch|dịch\s*ai|convert|\s|/|\|)+$",
        r"(?i).*xác\s+thực\s+bảo\s+mật.*",
        r"(?i).*đang\s+kiểm\s+tra\s+linh\s+căn.*",
        r"(?i).*quá\s+trình\s+này\s+diễn\s+ra\s+tự\s+động.*",
        r"(?i).*quản\s+lý\s+tên\s+riêng.*",
        r"(?i).*bôi\s+đen\s+chữ\s+trong\s+chương\s+truyện.*",
        r"(?i).*không\s+tìm\s+thấy\s+chương\s+phù\s+hợp.*",
        r"(?i).*chưa\s+có\s+tên\s+nào.*",
        r"(?i)hãy\s+bấm\s+vào\s+đây\s+để\s+báo\s+lỗi.*",
        r"(?i)nguồn\s*:\s*http.*",
        r"(?i)truyện\s+được\s+đăng\s+tại.*",
        r"(?i)quảng\s+cáo.*",
    ]

    # Các dấu hiệu nhận biết trang đang bị khóa bởi màn hình kiểm tra bảo mật
    ANTIBOT_CHALLENGE_MARKERS = [
        "đang kiểm tra linh căn",
        "xác thực bảo mật",
        "không tìm thấy chương phù hợp",
        "bôi đen chữ trong chương truyện",
        "<title>just a moment...</title>",
        "cf-browser-verification",
        "challenge-platform",
        "enable javascript and cookies to continue",
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

    # Ngưỡng tối thiểu của một chương truyện thật (chống lấy nhầm thông báo ngắn 200-300 chữ)
    MIN_VALID_CHAPTER_CHARS = 1000
    MIN_VALID_VN_ACCENTS = 180

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
        """Kiểm tra xem một dòng có phải là bẫy chữ tượng hình, chuỗi '_' hoặc thông báo chặn Bot."""
        if not line or not isinstance(line, str):
            return True

        lower_line = line.lower()
        if any(marker in lower_line for marker in cls.ANTIBOT_CHALLENGE_MARKERS):
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
        """
        Kiểm tra mã HTML tải về thực sự có nội dung chương truyện dài (>= 1000 ký tự, >= 180 dấu Tiếng Việt)
        và KHÔNG bị dính màn hình 'Đang kiểm tra linh căn đạo hữu'.
        """
        if not html or not isinstance(html, str) or len(html) < 1500:
            return False

        text_only = re.sub(r"(?is)<(script|style|head|nav|footer).*?>.*?</\1>", "\n", html)
        text_only = re.sub(r"(?s)<[^>]+>", "\n", text_only)

        cleaned = cls._clean_text(text_only)
        if len(cleaned) >= cls.MIN_VALID_CHAPTER_CHARS and cls._count_vn_accents(cleaned) >= cls.MIN_VALID_VN_ACCENTS:
            return True

        return False

    @classmethod
    def _raw_http_get(cls, url: str, timeout: int = 14) -> str:
        """Tải HTML từ 1 URL qua curl_cffi, cloudscraper, requests hoặc cổng Proxy dự phòng."""
        headers = cls._get_headers(url)

        # 1. Thử bằng curl_cffi giả lập trình duyệt thật
        if cffi_requests is not None:
            if cls._cffi_session is None:
                cls._cffi_session = cffi_requests.Session()
            for browser_fp in ["chrome124", "safari17_0", "chrome120"]:
                try:
                    resp = cls._cffi_session.get(
                        url,
                        headers=headers,
                        impersonate=browser_fp,
                        timeout=timeout,
                    )
                    if resp.status_code == 200 and resp.text and len(resp.text) > 1000:
                        return resp.text
                except Exception:
                    continue

        # 2. Thử bằng Cloudscraper
        if cloudscraper is not None:
            try:
                scraper = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows", "mobile": False}
                )
                resp = scraper.get(url, headers=headers, timeout=timeout)
                if resp.status_code == 200 and resp.text and len(resp.text) > 1000:
                    resp.encoding = resp.apparent_encoding or "utf-8"
                    return resp.text
            except Exception:
                pass

        # 3. Thử bằng Requests chuẩn
        try:
            if cls._req_session is None:
                cls._req_session = requests.Session()
            resp = cls._req_session.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200 and resp.text and len(resp.text) > 1000:
                resp.encoding = resp.apparent_encoding or "utf-8"
                return resp.text
        except Exception:
            pass

        # 4. Thử qua cổng Proxy trung gian nếu IP máy chủ bị chặn
        encoded_url = quote(url, safe="")
        proxy_urls = [
            f"https://api.allorigins.win/raw?url={encoded_url}",
            f"https://api.codetabs.com/v1/proxy?quest={encoded_url}",
            f"https://corsproxy.io/?{encoded_url}",
        ]
        for p_url in proxy_urls:
            try:
                resp = requests.get(p_url, headers=headers, timeout=timeout)
                if resp.status_code == 200 and resp.text and len(resp.text) > 1000:
                    resp.encoding = "utf-8"
                    return resp.text
            except Exception:
                continue

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

        mirror_list = [
            f"https://truyenfull.vision/{novel_slug}/chuong-{chap_num}/",
            f"https://truyenfull.io/{novel_slug}/chuong-{chap_num}/",
            f"https://truyenfull.vn/{novel_slug}/chuong-{chap_num}/",
            f"https://truyenfull.bio/{novel_slug}/chuong-{chap_num}/",
            f"https://sstruyen.vn/{novel_slug}/chuong-{chap_num}/",
            f"https://truyenchu.vn/{novel_slug}/chuong-{chap_num}",
        ]

        preferred_domain = cls._working_mirror_cache.get(novel_slug)
        if preferred_domain:
            preferred_url = f"{preferred_domain}/{novel_slug}/chuong-{chap_num}/"
            if preferred_url in mirror_list:
                mirror_list.remove(preferred_url)
            mirror_list.insert(0, preferred_url)

        return novel_slug, mirror_list

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
        """Sử dụng trình duyệt ảo Jina Headless Chrome (chỉ chấp nhận nếu vượt qua được màn hình bảo mật)."""
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

            if "Markdown Content:" in raw_text:
                raw_text = raw_text.split("Markdown Content:", 1)[1]

            cleaned = cls._clean_text(raw_text)
            if (
                len(cleaned) >= cls.MIN_VALID_CHAPTER_CHARS
                and cls._count_vn_accents(cleaned) >= cls.MIN_VALID_VN_ACCENTS
            ):
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
    def _extract_dom_chapter(
        cls, html_text: str, url: str
    ) -> Optional[Tuple[str, List[str], Optional[str], Optional[str], str]]:
        """
        Phân tích HTML thành (title, chunks, prev_url, next_url, cover_url).
        Nếu phát hiện trang 'Đang kiểm tra linh căn đạo hữu' hoặc nội dung ngắn < 1000 ký tự,
        trả về None ngay lập tức để chuyển sang Kho Truyện Dự Phòng.
        """
        if not html_text or len(html_text) < 1000:
            return None

        soup = BeautifulSoup(html_text, "lxml")
        prev_url, next_url = cls._extract_nav_links(soup, url)
        cover_url = cls.extract_novel_cover(soup, url)

        for tag in list(
            soup(["script", "style", "nav", "footer", "iframe", "noscript", "aside", "form", "svg", "canvas"])
        ):
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

        # Tìm khung chứa nội dung chương truyện thật (phải có ít nhất 180 chữ Tiếng Việt có dấu)
        content_node = None
        for selector in cls.CONTENT_SELECTORS:
            node = soup.select_one(selector)
            if cls._is_alive_tag(node):
                cleaned_candidate = cls._clean_text(node.get_text("\n"))
                if (
                    len(cleaned_candidate) >= cls.MIN_VALID_CHAPTER_CHARS
                    and cls._count_vn_accents(cleaned_candidate) >= cls.MIN_VALID_VN_ACCENTS
                ):
                    content_node = node
                    break

        if not content_node:
            candidates = []
            for div in soup.find_all(["article", "main", "section", "div"]):
                if not cls._is_alive_tag(div):
                    continue
                cleaned_candidate = cls._clean_text(div.get_text("\n"))
                vn_score = cls._count_vn_accents(cleaned_candidate)
                if (
                    len(cleaned_candidate) >= cls.MIN_VALID_CHAPTER_CHARS
                    and vn_score >= cls.MIN_VALID_VN_ACCENTS
                ):
                    candidates.append((vn_score, len(cleaned_candidate), div))

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
        if (
            len(cleaned_text) < cls.MIN_VALID_CHAPTER_CHARS
            or cls._count_vn_accents(cleaned_text) < cls.MIN_VALID_VN_ACCENTS
        ):
            return None

        chunks = cls.split_into_chunks(cleaned_text, max_chars=450)
        if chunks:
            first_chunk_lower = chunks[0].lower().strip()
            title_lower = title.lower().strip()
            if not first_chunk_lower.startswith(title_lower):
                chunks.insert(0, f"{title}.")

        return title, chunks, prev_url, next_url, cover_url

    @classmethod
    def _fetch_from_mirrors_or_jina(
        cls, original_url: str, fallback_title: str = "", fallback_cover: str = ""
    ) -> Tuple[str, List[str], Optional[str], Optional[str], str]:
        """
        Tự động lấy nội dung chương từ hệ thống Kho Truyện Dự Phòng (TruyenFull / SSTruyen / TruyenChu)
        khi truyendich.space bật màn hình 'Đang kiểm tra linh căn đạo hữu'.
        """
        fallback_prev, fallback_next = cls._compute_fallback_nav_links(original_url)
        cover_url = fallback_cover or cls.extract_novel_cover(None, original_url)

        novel_slug, mirror_urls = cls._build_mirror_urls(original_url)
        for m_url in mirror_urls:
            html = cls._raw_http_get(m_url, timeout=12)
            if not html:
                continue

            parsed = cls._extract_dom_chapter(html, m_url)
            if parsed is not None:
                m_title, m_chunks, m_prev, m_next, m_cover = parsed
                domain_match = re.match(r"^(https?://[^/]+)", m_url)
                if domain_match and novel_slug:
                    cls._working_mirror_cache[novel_slug] = domain_match.group(1)

                final_title = (
                    fallback_title
                    if (fallback_title and fallback_title != "Chương truyện")
                    else m_title
                )
                final_cover = cover_url or m_cover
                if final_cover:
                    novel_base = re.sub(r"/(?:chuong|chapter)-\d+.*$", "", original_url, flags=re.I)
                    cls._cover_cache[novel_base] = final_cover

                # Đồng bộ lại tiêu đề ở câu đầu tiên nếu cần
                if m_chunks and final_title:
                    m_chunks[0] = re.sub(r"^Chương\s+\d+[^.]*\.", f"{final_title}.", m_chunks[0], count=1)

                return (
                    final_title,
                    m_chunks,
                    fallback_prev or m_prev,
                    fallback_next or m_next,
                    final_cover,
                )

        # Dự phòng cuối qua Jina Headless Chrome
        jina_res = cls._fetch_via_jina_headless(original_url, timeout=18)
        if jina_res is not None:
            j_title, j_chunks = jina_res
            return (
                fallback_title or j_title,
                j_chunks,
                fallback_prev,
                fallback_next,
                cover_url,
            )

        raise ValueError(
            f"Không thể tải chương truyện từ cả trang gốc lẫn kho dự phòng cho URL: {original_url}"
        )

    @classmethod
    def parse_chapter_html(
        cls, html_text: str, url: str
    ) -> Tuple[str, List[str], Optional[str], Optional[str], str]:
        """
        Phân tích mã HTML do trình duyệt gửi lên. Nếu HTML là màn hình 'Đang kiểm tra linh căn',
        lập tức lấy tiêu đề + ảnh bìa rồi tự động tải nội dung thật từ Kho Truyện Dự Phòng.
        """
        fallback_title = ""
        fallback_cover = ""
        if html_text:
            try:
                temp_soup = BeautifulSoup(html_text, "lxml")
                fallback_title = cls._extract_chapter_title(temp_soup, url)
                fallback_cover = cls.extract_novel_cover(temp_soup, url)
            except Exception:
                pass

        parsed = cls._extract_dom_chapter(html_text, url)
        if parsed is not None:
            return parsed

        return cls._fetch_from_mirrors_or_jina(url, fallback_title=fallback_title, fallback_cover=fallback_cover)

    @classmethod
    def fetch_chapter(
        cls, url: str, timeout: int = 20
    ) -> Tuple[str, List[str], Optional[str], Optional[str], str]:
        """
        Tải trang web và bóc tách nội dung chương truyện.
        Tự động vượt qua màn hình 'Đang kiểm tra linh căn đạo hữu' bằng Kho Truyện Dự Phòng.
        """
        novel_slug, _ = cls._build_mirror_urls(url)

        # Nếu bộ truyện này đang bị trang gốc khóa bằng màn hình bảo mật -> Lấy thẳng từ kho dự phòng
        if novel_slug and novel_slug in cls._working_mirror_cache:
            try:
                return cls._fetch_from_mirrors_or_jina(url)
            except Exception:
                pass

        fallback_title = ""
        fallback_cover = ""
        html_text = cls._raw_http_get(url, timeout=8)
        if html_text:
            try:
                temp_soup = BeautifulSoup(html_text, "lxml")
                fallback_title = cls._extract_chapter_title(temp_soup, url)
                fallback_cover = cls.extract_novel_cover(temp_soup, url)
            except Exception:
                pass

            if cls._has_valid_vietnamese_chapter(html_text):
                parsed = cls._extract_dom_chapter(html_text, url)
                if parsed is not None:
                    return parsed

        return cls._fetch_from_mirrors_or_jina(
            url, fallback_title=fallback_title, fallback_cover=fallback_cover
        )

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
                if (
                    text
                    and len(text) > 1
                    and not cls._is_trap_or_garbled_line(text)
                    and "xác thực bảo mật" not in text.lower()
                ):
                    chap_name = text
                    break

        if chap_name:
            if re.search(r"(?i)^chương\s+\d+", chap_name):
                return chap_name
            elif chap_num:
                return f"{chap_num}: {chap_name}"
            else:
                return chap_name

        if soup.title and soup.title.get_text(strip=True):
            raw_title = soup.title.get_text(strip=True)
            clean_t = re.split(r"\s*[-|]\s*", raw_title)[0].strip()
            if clean_t and "xác thực" not in clean_t.lower() and "just a moment" not in clean_t.lower():
                return clean_t

        if chap_num:
            return chap_num

        return "Chương truyện"

    @classmethod
    def _extract_nav_links(
        cls, soup: BeautifulSoup, base_url: str
    ) -> Tuple[Optional[str], Optional[str]]:
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

        if not next_url or not prev_url:
            fallback_prev, fallback_next = cls._compute_fallback_nav_links(base_url)
            prev_url = prev_url or fallback_prev
            next_url = next_url or fallback_next

        return prev_url, next_url

    @classmethod
    def _clean_text(cls, text: str) -> str:
        """Làm sạch từng dòng văn bản, xóa sạch thông báo chặn Bot và chữ tượng hình."""
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
