# File: core/scraper.py
import re
import time
from typing import Tuple, List, Optional
from urllib.parse import urljoin, quote, urlparse
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
    Bộ bóc tách nội dung truyện chữ CHỈ LẤY TỪ ĐÚNG URL NGƯỜI DÙNG DÁN VÀO:
    - Đồng bộ 100% chữ ký TLS/HTTP2 nguyên bản của curl_cffi (không ghi đè User-Agent gây xung đột).
    - Tự động vượt màn hình 'Đang kiểm tra linh căn đạo hữu' và lọc sạch bẫy chữ tượng hình.
    """

    STANDARD_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Upgrade-Insecure-Requests": "1",
    }

    CONTENT_SELECTORS = [
        "#chapter-c",
        ".chapter-c",
        "#vungdoc",
        ".vung-doc",
        "#chapt-content",
        ".box-chap",
        "#chapter-content",
        ".chapter-content",
        "#content",
        ".entry-content",
        "article",
        "main",
    ]

    # Các cụm từ giao diện / màn hình chặn Bot của truyendich.space cần loại bỏ
    INLINE_UI_JUNK = [
        r"(?i)xác\s+thực\s+bảo\s+mật",
        r"(?i)đang\s+kiểm\s+tra\s+linh\s+căn\s+đạo\s+hữu\.?\.?",
        r"(?i)quá\s+trình\s+này\s+diễn\s+ra\s+tự\s+động",
        r"(?i)nghe\s+truyện\s*\(tts\)",
        r"(?i)quản\s+lý\s+tên\s+riêng",
        r"(?i)cài\s+đặt\s+giao\s+diện",
        r"(?i)chưa\s+có\s+tên\s+nào",
        r"(?i)bôi\s+đen\s+chữ\s+trong\s+chương\s+truyện\s+rồi\s+chọn\s*[\"“]?thêm\s+tên[\"”]?\s*để\s+thêm\s+nhanh\.?",
        r"(?i)mục\s+lục\s+\d+\s+chương",
        r"(?i)không\s+tìm\s+thấy\s+chương\s+phù\s+hợp\.?",
    ]

    JUNK_LINE_PATTERNS = [
        r"(?i)^\s*(chương\s+trước|chương\s+sau|chương\s+tiếp|mục\s+lục|danh\s+sách\s+chương|báo\s+lỗi\s+chương|bình\s+luận)\s*$",
        r"(?i)^\s*(bản\s*dịch|dịch\s*ai|convert|\d+\s+từ|\s|/|\|)+$",
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

    # Chỉ giữ lại chữ cái Tiếng Việt, chữ số và dấu câu văn học hợp lệ (loại bỏ chữ tượng hình & dấu '_')
    DISALLOWED_CHARS_REGEX = re.compile(
        r"[^a-zA-Z0-9\s.,!?;:'\"()\[\]\-–—…“”‘’/\\+%&\u00C0-\u024F\u1E00-\u1EFF]"
    )

    # Một chương truyện thật (sau khi đã xóa chữ tượng hình & thông báo bảo mật) phải có ít nhất:
    MIN_VALID_CHAPTER_CHARS = 600
    MIN_VALID_VN_ACCENTS = 120

    _cover_cache = {}
    _cffi_sessions = {}

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
        if not text or not isinstance(text, str):
            return 0
        return len(cls.VN_ACCENT_REGEX.findall(text))

    @classmethod
    def _is_trap_or_garbled_line(cls, line: str) -> bool:
        """Kiểm tra dòng bẫy chữ tượng hình Ai Cập, chuỗi '_' hoặc dòng thông báo chặn Bot."""
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
    def _build_google_translate_proxy_url(cls, url: str) -> str:
        """
        Tạo link đi qua Google Translate Proxy cho đúng URL gốc.
        Đặt ngôn ngữ nguồn là 'es' (Tây Ban Nha) và đích là 'vi' (Tiếng Việt) để máy chủ Google
        tải trang bằng IP của Google nhưng giữ nguyên 100% câu chữ Tiếng Việt gốc.
        """
        parsed = urlparse(url)
        if not parsed.netloc:
            return ""
        dashed_host = parsed.netloc.replace("-", "--").replace(".", "-")
        path_part = parsed.path or "/"
        query_part = f"&{parsed.query}" if parsed.query else ""
        return (
            f"https://{dashed_host}.translate.goog{path_part}"
            f"?_x_tr_sl=es&_x_tr_tl=vi&_x_tr_hl=vi&_x_tr_pto=wapp{query_part}"
        )

    @classmethod
    def _extract_cover_from_soup(cls, soup: BeautifulSoup, base_url: str) -> str:
        """Lấy ảnh bìa truyện trực tiếp từ trang hiện tại (không gửi thêm request phụ gây chặn IP)."""
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
                    if src and not src.startswith("data:") and "translate.goog" not in src:
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
    def _extract_valid_chapter_from_html(
        cls, html_text: str, url: str
    ) -> Optional[Tuple[str, List[str], Optional[str], Optional[str], str]]:
        """
        Phân tích mã HTML của trang và kiểm tra nghiêm ngặt:
        - Nếu trang đang dừng ở màn hình 'Đang kiểm tra linh căn đạo hữu' hoặc 'Chữ tượng hình',
          hàm sẽ trả về None để hệ thống tự động chuyển sang lớp tải tiếp theo.
        """
        if not html_text or not isinstance(html_text, str) or len(html_text) < 800:
            return None

        soup = BeautifulSoup(html_text, "lxml")

        prev_url, next_url = cls._extract_nav_links(soup, url)

        novel_base_url = re.sub(r"/(?:chuong|chapter)-\d+.*$", "", url, flags=re.I)
        cover_url = cls._extract_cover_from_soup(soup, url)
        if cover_url:
            cls._cover_cache[novel_base_url] = cover_url
        else:
            cover_url = cls._cover_cache.get(novel_base_url, "")

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
                    "class": re.compile(
                        r"(ads|advert|banner|nav|pagination|comment|social|breadcrumb|modal|dialog|drawer)",
                        re.I,
                    )
                }
            )
        ):
            if cls._is_alive_tag(junk_div):
                # Chỉ xóa nếu thẻ đó không chứa toàn bộ chương truyện
                if cls._count_vn_accents(junk_div.get_text()) < 250:
                    junk_div.decompose()

        # Tìm khung chứa nội dung chương truyện thật
        content_node = None
        for selector in cls.CONTENT_SELECTORS:
            node = soup.select_one(selector)
            if cls._is_alive_tag(node):
                candidate_text = cls._clean_text(node.get_text("\n"))
                if (
                    len(candidate_text) >= cls.MIN_VALID_CHAPTER_CHARS
                    and cls._count_vn_accents(candidate_text) >= cls.MIN_VALID_VN_ACCENTS
                ):
                    content_node = node
                    break

        if not content_node:
            candidates = []
            for div in soup.find_all(["article", "main", "section", "div"]):
                if not cls._is_alive_tag(div):
                    continue
                candidate_text = cls._clean_text(div.get_text("\n"))
                vn_score = cls._count_vn_accents(candidate_text)
                if (
                    len(candidate_text) >= cls.MIN_VALID_CHAPTER_CHARS
                    and vn_score >= cls.MIN_VALID_VN_ACCENTS
                ):
                    candidates.append((vn_score, len(candidate_text), div))

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
    def _fetch_via_jina_same_url(
        cls, url: str, timeout: int = 22
    ) -> Optional[Tuple[str, List[str], Optional[str], Optional[str], str]]:
        """
        Dùng trình duyệt Chrome ảo (Jina Headless Browser) mở ĐÚNG đường dẫn URL của người dùng,
        chờ JavaScript chạy qua màn hình 'Quá trình này diễn ra tự động' rồi lấy nội dung.
        """
        jina_url = f"https://r.jina.ai/{url}"

        # Thử chế độ trả về HTML đã render xong JavaScript
        for return_format in ["html", "text"]:
            try:
                headers = {
                    "Accept": "text/plain",
                    "X-Return-Format": return_format,
                    "X-Timeout": "20",
                    "X-No-Cache": "true",
                }
                resp = requests.get(jina_url, headers=headers, timeout=timeout)
                if resp.status_code != 200 or not resp.text:
                    continue

                if return_format == "html":
                    parsed = cls._extract_valid_chapter_from_html(resp.text, url)
                    if parsed is not None:
                        return parsed
                else:
                    raw_text = resp.text
                    title = ""
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
                        prev_url, next_url = cls._compute_fallback_nav_links(url)
                        novel_base_url = re.sub(r"/(?:chuong|chapter)-\d+.*$", "", url, flags=re.I)
                        cover_url = cls._cover_cache.get(novel_base_url, "")
                        if not title or "xác thực" in title.lower():
                            m_url = re.search(r"(?:chuong|chapter)-(\d+)", url, re.I)
                            title = f"Chương {m_url.group(1)}" if m_url else "Chương truyện"

                        chunks = cls.split_into_chunks(cleaned, max_chars=450)
                        if chunks and not chunks[0].lower().startswith(title.lower()):
                            chunks.insert(0, f"{title}.")
                        return title, chunks, prev_url, next_url, cover_url
            except Exception:
                continue

        return None

    @classmethod
    def fetch_chapter(
        cls, url: str, timeout: int = 20
    ) -> Tuple[str, List[str], Optional[str], Optional[str], str]:
        """
        Tải và bóc tách chương truyện CHỈ TỪ ĐÚNG URL ĐƯỢC CUNG CẤP.
        Tự động đi qua 5 lớp kết nối khác nhau trên cùng URL đó cho đến khi lấy được
        nội dung Tiếng Việt thật (bỏ qua màn hình kiểm tra linh căn & chữ tượng hình).
        """
        parsed_url = urlparse(url)
        origin_referer = f"{parsed_url.scheme}://{parsed_url.netloc}/" if parsed_url.netloc else "https://www.google.com.vn/"

        # LỚP 1: curl_cffi với chữ ký gốc 100% (KHÔNG ghi đè User-Agent để tránh lệch Fingerprint)
        if cffi_requests is not None:
            native_Headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer": origin_referer,
                "Upgrade-Insecure-Requests": "1",
            }
            for browser_fp in ["chrome120", "chrome124", "safari17_0", "edge101"]:
                try:
                    if browser_fp not in cls._cffi_sessions:
                        cls._cffi_sessions[browser_fp] = cffi_requests.Session()

                    resp = cls._cffi_sessions[browser_fp].get(
                        url,
                        headers=native_Headers,
                        impersonate=browser_fp,
                        timeout=14,
                    )
                    if resp.status_code == 200 and resp.text:
                        parsed = cls._extract_valid_chapter_from_html(resp.text, url)
                        if parsed is not None:
                            return parsed
                    # Nếu phiên hiện tại bị dính màn hình kiểm tra linh căn hoặc 403 -> Tạo phiên mới sạch sẽ
                    cls._cffi_sessions[browser_fp] = cffi_requests.Session()
                except Exception:
                    cls._cffi_sessions.pop(browser_fp, None)
                    continue

        # LỚP 2: Cloudscraper (Giả lập Chrome trên Windows & iOS)
        if cloudscraper is not None:
            for platform_cfg in [
                {"browser": "chrome", "platform": "windows", "mobile": False},
                {"browser": "chrome", "platform": "ios", "mobile": True},
            ]:
                try:
                    scraper = cloudscraper.create_scraper(browser=platform_cfg)
                    resp = scraper.get(
                        url,
                        headers={"Accept-Language": "vi-VN,vi;q=0.9", "Referer": origin_referer},
                        timeout=14,
                    )
                    if resp.status_code == 200 and resp.text:
                        resp.encoding = resp.apparent_encoding or "utf-8"
                        parsed = cls._extract_valid_chapter_from_html(resp.text, url)
                        if parsed is not None:
                            return parsed
                except Exception:
                    continue

        # LỚP 3: Google Translate Website Proxy cho ĐÚNG URL GỐC (Đi qua dải IP của Google)
        gtrans_url = cls._build_google_translate_proxy_url(url)
        if gtrans_url:
            try:
                if cffi_requests is not None:
                    resp = cffi_requests.get(gtrans_url, impersonate="chrome120", timeout=15)
                else:
                    resp = requests.get(gtrans_url, headers=cls.STANDARD_HEADERS, timeout=15)

                if resp.status_code == 200 and resp.text:
                    parsed = cls._extract_valid_chapter_from_html(resp.text, url)
                    if parsed is not None:
                        return parsed
            except Exception:
                pass

        # LỚP 4: Cổng Proxy AllOrigins / CodeTabs / CorsProxy cho ĐÚNG URL GỐC
        encoded_url = quote(url, safe="")
        proxy_gateways = [
            f"https://api.allorigins.win/raw?url={encoded_url}",
            f"https://api.codetabs.com/v1/proxy?quest={encoded_url}",
            f"https://corsproxy.io/?{encoded_url}",
        ]
        for p_url in proxy_gateways:
            try:
                resp = requests.get(p_url, headers=cls.STANDARD_HEADERS, timeout=14)
                if resp.status_code == 200 and resp.text:
                    resp.encoding = "utf-8"
                    parsed = cls._extract_valid_chapter_from_html(resp.text, url)
                    if parsed is not None:
                        return parsed
            except Exception:
                continue

        # LỚP 5: Trình duyệt Chrome ảo Jina Headless Browser chạy JavaScript trên ĐÚNG URL GỐC
        jina_parsed = cls._fetch_via_jina_same_url(url, timeout=22)
        if jina_parsed is not None:
            return jina_parsed

        raise ValueError(
            f"Trang web tại {url} đang tạm khóa kết nối tự động (Kiểm tra linh căn / 403). "
            f"Vui lòng đợi khoảng 5-10 giây rồi bấm Tải chương lại!"
        )

    @classmethod
    def parse_chapter_html(
        cls, html_text: str, url: str
    ) -> Tuple[str, List[str], Optional[str], Optional[str], str]:
        """
        Kiểm tra mã HTML do trình duyệt gửi lên.
        Nếu HTML đó bị dính màn hình 'Đang kiểm tra linh căn đạo hữu' hoặc chữ tượng hình,
        tự động gọi fetch_chapter(url) để tải lại từ chính URL gốc đó bằng 5 lớp bảo vệ.
        """
        parsed = cls._extract_valid_chapter_from_html(html_text, url)
        if parsed is not None:
            return parsed
        return cls.fetch_chapter(url)

    @classmethod
    def _compute_fallback_nav_links(cls, url: str) -> Tuple[Optional[str], Optional[str]]:
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
            "article h1",
            "main h1",
            "header h1",
            ".chapter-title",
            "#chapter-title",
            ".chap-title",
            "h1",
            "h2.chapter-title",
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
                    and "kiểm tra linh căn" not in text.lower()
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
            clean_t = re.split(r"\s*[|-]\s*", raw_title)[0].strip()
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
            # Không lấy link bị biến đổi sang translate.goog làm link chuyển chương
            if "translate.goog" in href:
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

                if not next_url and re.search(
                    r"(chương\s+tiếp|chương\s+sau|tiếp\s+theo|next\s+chapter)", combined
                ):
                    next_url = valid_link
                elif not prev_url and re.search(
                    r"(chương\s+trước|trước\s+đó|prev\s+chapter)", combined
                ):
                    prev_url = valid_link

        if not next_url or not prev_url:
            fallback_prev, fallback_next = cls._compute_fallback_nav_links(base_url)
            prev_url = prev_url or fallback_prev
            next_url = next_url or fallback_next

        return prev_url, next_url

    @classmethod
    def _clean_text(cls, text: str) -> str:
        """
        Làm sạch văn bản:
        - Xóa mọi cụm chữ thông báo 'Xác thực bảo mật / Đang kiểm tra linh căn đạo hữu'
          (kể cả khi bị gộp chung trên 1 dòng).
        - Xóa sạch toàn bộ chữ tượng hình Ai Cập và ký tự rác.
        """
        if not text or not isinstance(text, str):
            return ""

        lines = text.splitlines()
        cleaned_lines = []

        for raw_line in lines:
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            # Xóa trực tiếp các cụm từ giao diện / bảo mật nếu bị dính trên cùng dòng
            for ui_pattern in cls.INLINE_UI_JUNK:
                raw_line = re.sub(ui_pattern, " ", raw_line)

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

            is_junk = any(re.search(pattern, line) for pattern in cls.JUNK_LINE_PATTERNS)
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
