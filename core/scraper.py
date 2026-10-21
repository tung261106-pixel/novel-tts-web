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
    Bộ bóc tách nội dung truyện chữ chống chặn 403, chống bẫy chữ tượng hình
    và kiểm tra an toàn thuộc tính thẻ HTML (chống lỗi NoneType).
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

    # Chỉ giữ lại chữ cái Tiếng Việt, chữ số và dấu câu chuẩn (loại bỏ hoàn toàn chữ tượng hình & dấu '_')
    DISALLOWED_CHARS_REGEX = re.compile(
        r"[^a-zA-Z0-9\s.,!?;:'\"()\[\]\-–—…“”‘’/\\+%&\u00C0-\u024F\u1E00-\u1EFF]"
    )

    _cover_cache = {}
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
        if not html or not isinstance(html, str) or len(html) < 800:
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

        if cls._count_vn_accents(html) >= 180:
            return True

        return False

    @classmethod
    def _download_html(cls, url: str, timeout: int = 20) -> str:
        """Tải HTML qua 7 lớp bảo vệ và loại bỏ các phản hồi bị dính bẫy chữ tượng hình."""
        headers = cls._get_headers(url)

        # Lớp 1: Dùng curl_cffi giả lập trình duyệt thật
        if cffi_requests is not None:
            if cls._cffi_session is None:
                cls._cffi_session = cffi_requests.Session()
            for browser_fp in ["chrome124", "safari17_0", "chrome120", "edge101"]:
                try:
                    resp = cls._cffi_session.get(
                        url,
                        headers=headers,
                        impersonate=browser_fp,
                        timeout=timeout,
                    )
                    if resp.status_code == 200 and cls._has_valid_vietnamese_chapter(resp.text):
                        return resp.text
                except Exception:
                    continue

        # Lớp 2: Dùng Cloudscraper
        if cloudscraper is not None:
            for mobile_mode in [False, True]:
                try:
                    scraper = cloudscraper.create_scraper(
                        browser={
                            "browser": "chrome",
                            "platform": "ios" if mobile_mode else "windows",
                            "mobile": mobile_mode,
                        }
                    )
                    resp = scraper.get(url, headers=headers, timeout=timeout)
                    if resp.status_code == 200 and resp.text:
                        resp.encoding = resp.apparent_encoding or "utf-8"
                        if cls._has_valid_vietnamese_chapter(resp.text):
                            return resp.text
                except Exception:
                    continue

        # Lớp 3: Dùng Requests chuẩn
        try:
            if cls._req_session is None:
                cls._req_session = requests.Session()
            resp = cls._req_session.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200 and resp.text:
                resp.encoding = resp.apparent_encoding or "utf-8"
                if cls._has_valid_vietnamese_chapter(resp.text):
                    return resp.text
        except Exception:
            pass

        # Lớp 4: Cổng AllOrigins JSON Mode
        try:
            ao_json_url = f"https://api.allorigins.win/get?url={quote(url, safe='')}&disableCache=true"
            resp = requests.get(ao_json_url, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                contents = (data.get("contents") or "") if isinstance(data, dict) else ""
                if cls._has_valid_vietnamese_chapter(contents):
                    return contents
        except Exception:
            pass

        # Lớp 5, 6, 7: Hệ thống Proxy dự phòng
        encoded_url = quote(url, safe="")
        proxy_urls = [
            f"https://api.allorigins.win/raw?url={encoded_url}",
            f"https://api.codetabs.com/v1/proxy?quest={encoded_url}",
            f"https://corsproxy.io/?{encoded_url}",
            f"https://thingproxy.freeboard.io/fetch/{url}",
        ]
        for p_url in proxy_urls:
            try:
                resp = requests.get(p_url, headers=headers, timeout=timeout)
                if resp.status_code == 200 and resp.text:
                    resp.encoding = "utf-8"
                    if cls._has_valid_vietnamese_chapter(resp.text):
                        return resp.text
            except Exception:
                continue

        raise ValueError(f"Không thể truy cập trang web (Lỗi 403) tại URL: {url}")

    @classmethod
    def _extract_nextjs_script_text(cls, soup: BeautifulSoup) -> str:
        """Trích xuất dự phòng nội dung chương Tiếng Việt nằm trong các thẻ <script> của Next.js."""
        candidates = []
        try:
            for script in soup.find_all("script"):
                if not cls._is_alive_tag(script):
                    continue
                raw = script.string or script.get_text() or ""
                if not raw or len(raw) < 300:
                    continue

                matches = re.findall(r'"((?:[^"\\]|\\.)*)"', raw)
                for m in matches:
                    if len(m) < 200:
                        continue
                    try:
                        decoded = json.loads(f'"{m}"')
                    except Exception:
                        decoded = m.replace("\\n", "\n").replace('\\"', '"')

                    if isinstance(decoded, str) and cls._count_vn_accents(decoded) >= 60:
                        if not cls._is_trap_or_garbled_line(decoded[:200]):
                            candidates.append(decoded)
        except Exception:
            pass

        if not candidates:
            return ""
        candidates.sort(key=lambda s: cls._count_vn_accents(s), reverse=True)
        return "\n\n".join(candidates[:2])

    @classmethod
    def _remove_hidden_and_trap_elements(cls, soup: BeautifulSoup) -> None:
        """
        Xóa toàn bộ các thẻ ẩn CSS và các thẻ chứa chữ tượng hình bẫy bot.
        Kiểm tra _is_alive_tag nghiêm ngặt để không bao giờ xảy ra lỗi NoneType.
        """
        try:
            to_decompose = []
            for el in soup.find_all(True):
                if not cls._is_alive_tag(el):
                    continue
                if el.name in ("html", "body", "main", "article", "section", "head"):
                    continue

                attrs = el.attrs
                if "hidden" in attrs or str(attrs.get("aria-hidden") or "").lower() == "true":
                    to_decompose.append(el)
                    continue

                style = str(attrs.get("style") or "").lower().replace(" ", "")
                if style and any(
                    rule in style
                    for rule in (
                        "display:none",
                        "visibility:hidden",
                        "opacity:0",
                        "font-size:0",
                        "height:0",
                        "max-height:0",
                        "left:-9999",
                    )
                ):
                    to_decompose.append(el)
                    continue

                raw_cls = attrs.get("class") or []
                if isinstance(raw_cls, (list, tuple)):
                    cls_list = [str(c).lower() for c in raw_cls]
                elif isinstance(raw_cls, str):
                    cls_list = raw_cls.lower().split()
                else:
                    cls_list = []

                if any(c in cls_list for c in ("hidden", "sr-only", "invisible", "opacity-0", "text-[0px]")):
                    if cls._count_vn_accents(el.get_text()) < 100:
                        to_decompose.append(el)

            for el in to_decompose:
                if cls._is_alive_tag(el):
                    el.decompose()

            # Quét các thẻ đoạn ngắn chứa chữ tượng hình hoặc dòng bẫy '_'
            trap_nodes = []
            for node in soup.find_all(["p", "span", "div"]):
                if not cls._is_alive_tag(node):
                    continue
                if node.find(["p", "div", "article", "section"]):
                    continue
                txt = node.get_text(strip=True)
                if txt and cls._is_trap_or_garbled_line(txt):
                    trap_nodes.append(node)

            for node in trap_nodes:
                if cls._is_alive_tag(node):
                    node.decompose()
        except Exception:
            pass

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
    def extract_novel_cover(cls, chapter_soup: BeautifulSoup, chapter_url: str) -> str:
        """Trích xuất ảnh bìa và lưu đệm theo tên truyện."""
        novel_base_url = re.sub(r"/(?:chuong|chapter)-\d+.*$", "", chapter_url, flags=re.I)
        if novel_base_url in cls._cover_cache:
            return cls._cover_cache[novel_base_url]

        cover_url = cls._extract_cover_from_soup(chapter_soup, chapter_url)

        if not cover_url and novel_base_url != chapter_url:
            try:
                novel_html = cls._download_html(novel_base_url, timeout=6)
                novel_soup = BeautifulSoup(novel_html, "lxml")
                cover_url = cls._extract_cover_from_soup(novel_soup, novel_base_url)
            except Exception:
                pass

        cls._cover_cache[novel_base_url] = cover_url
        return cover_url

    @classmethod
    def parse_chapter_html(cls, html_text: str, url: str) -> Tuple[str, List[str], Optional[str], Optional[str], str]:
        """Phân tích mã HTML thành (title, chunks, prev_url, next_url, cover_url) an toàn tuyệt đối."""
        soup = BeautifulSoup(html_text or "", "lxml")

        prev_url, next_url = cls._extract_nav_links(soup, url)
        cover_url = cls.extract_novel_cover(soup, url)

        nextjs_backup_text = cls._extract_nextjs_script_text(soup)

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

        cls._remove_hidden_and_trap_elements(soup)

        # Chọn khung chứa nhiều chữ Tiếng Việt có dấu nhất (loại bỏ hoàn toàn khung chữ tượng hình)
        content_node = None
        for selector in cls.CONTENT_SELECTORS:
            node = soup.select_one(selector)
            if cls._is_alive_tag(node) and cls._count_vn_accents(node.get_text(strip=True)) >= 60:
                content_node = node
                break

        if not content_node:
            candidates = []
            for div in soup.find_all(["article", "main", "section", "div"]):
                if not cls._is_alive_tag(div):
                    continue
                txt = div.get_text(" ", strip=True)
                vn_score = cls._count_vn_accents(txt)
                if vn_score >= 60:
                    candidates.append((vn_score, len(txt), div))

            if candidates:
                max_vn_score = max(c[0] for c in candidates)
                best_candidates = [c for c in candidates if c[0] >= max_vn_score * 0.85]
                best_candidates.sort(key=lambda x: x[1])
                content_node = best_candidates[0][2]
            else:
                content_node = soup.body

        cleaned_text = ""
        if content_node and cls._is_alive_tag(content_node):
            for br in list(content_node.find_all("br")):
                if cls._is_alive_tag(br):
                    br.replace_with("\n")
            for block_tag in list(content_node.find_all(["p", "div", "h2", "h3", "h4", "li"])):
                if cls._is_alive_tag(block_tag):
                    block_tag.append("\n")

            raw_text = content_node.get_text()
            cleaned_text = cls._clean_text(raw_text)

        if cls._count_vn_accents(cleaned_text) < 50 and cls._count_vn_accents(nextjs_backup_text) >= 50:
            cleaned_text = cls._clean_text(nextjs_backup_text)

        if len(cleaned_text) < 50 or cls._count_vn_accents(cleaned_text) < 30:
            raise ValueError(
                "Không tìm thấy văn bản Tiếng Việt hợp lệ (trang nguồn đang bật chế độ chống bot). Vui lòng thử tải lại!"
            )

        chunks = cls.split_into_chunks(cleaned_text, max_chars=450)

        if chunks:
            first_chunk_lower = chunks[0].lower().strip()
            title_lower = title.lower().strip()
            if not first_chunk_lower.startswith(title_lower):
                chunks.insert(0, f"{title}.")

        return title, chunks, prev_url, next_url, cover_url

    @classmethod
    def fetch_chapter(cls, url: str, timeout: int = 20) -> Tuple[str, List[str], Optional[str], Optional[str], str]:
        """Tải trang web và bóc tách nội dung chương truyện."""
        html_text = cls._download_html(url, timeout=timeout)
        return cls.parse_chapter_html(html_text, url)

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
            match = re.search(r"(chuong-|chapter-)(\d+)([^/\d]*)$", base_url, re.I)
            if match:
                prefix, num_str, suffix = match.groups()
                chap_num = int(num_str)
                base_prefix = base_url[: match.start()]
                if not next_url:
                    next_url = f"{base_prefix}{prefix}{chap_num + 1}{suffix}"
                if not prev_url and chap_num > 1:
                    prev_url = f"{base_prefix}{prefix}{chap_num - 1}{suffix}"

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
