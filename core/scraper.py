# File: core/scraper.py
import re
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
    """Bộ bóc tách nội dung truyện chữ, link chuyển chương và ảnh bìa truyện tự động."""

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.google.com.vn/",
        "Upgrade-Insecure-Requests": "1",
    }

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

    _cover_cache = {}

    @classmethod
    def _download_html(cls, url: str, timeout: int = 20) -> str:
        """Tải HTML qua 4 lớp bảo vệ để tránh lỗi 403 Forbidden trên Render."""
        if cffi_requests is not None:
            try:
                resp = cffi_requests.get(
                    url, headers=cls.HEADERS, impersonate="chrome120", timeout=timeout
                )
                if resp.status_code == 200 and len(resp.text) > 500:
                    return resp.text
            except Exception:
                pass

        if cloudscraper is not None:
            try:
                scraper = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows", "mobile": False}
                )
                resp = scraper.get(url, headers=cls.HEADERS, timeout=timeout)
                if resp.status_code == 200 and len(resp.text) > 500:
                    resp.encoding = resp.apparent_encoding or "utf-8"
                    return resp.text
            except Exception:
                pass

        try:
            resp = requests.get(url, headers=cls.HEADERS, timeout=timeout)
            if resp.status_code == 200 and len(resp.text) > 500:
                resp.encoding = resp.apparent_encoding or "utf-8"
                return resp.text
        except Exception:
            pass

        proxy_urls = [
            f"https://api.allorigins.win/raw?url={quote(url, safe='')}",
            f"https://api.codetabs.com/v1/proxy?quest={quote(url, safe='')}",
        ]
        for p_url in proxy_urls:
            try:
                resp = requests.get(p_url, timeout=timeout)
                if resp.status_code == 200 and len(resp.text) > 500:
                    resp.encoding = "utf-8"
                    return resp.text
            except Exception:
                continue

        raise ValueError(f"Không thể truy cập trang web (Lỗi 403) tại URL: {url}")

    @classmethod
    def _extract_cover_from_soup(cls, soup: BeautifulSoup, base_url: str) -> str:
        """Tìm link ảnh bìa từ thẻ meta og:image hoặc các class ảnh bìa phổ biến."""
        # 1. Tìm trong các thẻ img chứa class/id bìa truyện
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
            if img:
                src = (img.get("data-src") or img.get("src") or "").strip()
                if src and not src.startswith("data:"):
                    return urljoin(base_url, src)

        # 2. Tìm trong thẻ Meta OpenGraph / Twitter Image
        for meta_sel in [
            'meta[property="og:image"]',
            'meta[name="og:image"]',
            'meta[name="twitter:image"]',
            'link[rel="image_src"]',
        ]:
            tag = soup.select_one(meta_sel)
            if tag:
                content = (tag.get("content") or tag.get("href") or "").strip()
                if content and not any(x in content.lower() for x in ["logo", "favicon", "default-share"]):
                    return urljoin(base_url, content)

        return ""

    @classmethod
    def extract_novel_cover(cls, chapter_soup: BeautifulSoup, chapter_url: str) -> str:
        """
        Trích xuất ảnh bìa truyện từ trang chương hoặc tự động lùi về trang chủ của bộ truyện
        để lấy ảnh bìa chính xác nhất (có bộ nhớ đệm để không phải tải lại nhiều lần).
        """
        # Suy luận URL gốc của bộ truyện (VD: .../gia-thien/chuong-1050 -> .../gia-thien)
        novel_base_url = re.sub(r"/(?:chuong|chapter)-\d+.*$", "", chapter_url, flags=re.I)
        if novel_base_url in cls._cover_cache:
            return cls._cover_cache[novel_base_url]

        # Thử lấy trực tiếp trên trang chương hiện tại
        cover_url = cls._extract_cover_from_soup(chapter_soup, chapter_url)

        # Nếu trang chương không có ảnh bìa, tự động kiểm tra trang thông tin gốc của truyện
        if not cover_url and novel_base_url != chapter_url:
            try:
                novel_html = cls._download_html(novel_base_url, timeout=8)
                novel_soup = BeautifulSoup(novel_html, "lxml")
                cover_url = cls._extract_cover_from_soup(novel_soup, novel_base_url)
            except Exception:
                pass

        if cover_url:
            cls._cover_cache[novel_base_url] = cover_url
        return cover_url

    @classmethod
    def fetch_chapter(cls, url: str, timeout: int = 20) -> Tuple[str, List[str], Optional[str], Optional[str], str]:
        """
        Tải trang web, trích xuất: (title, chunks, prev_url, next_url, cover_url)
        """
        html_text = cls._download_html(url, timeout=timeout)
        soup = BeautifulSoup(html_text, "lxml")

        # 1. Lấy link chuyển chương & ảnh bìa trước khi xóa các thẻ phụ
        prev_url, next_url = cls._extract_nav_links(soup, url)
        cover_url = cls.extract_novel_cover(soup, url)

        for tag in soup(["script", "style", "nav", "footer", "iframe", "noscript", "aside", "form"]):
            tag.decompose()

        title = cls._extract_chapter_title(soup, url)

        for tag in soup(["header", "button", "h1"]):
            tag.decompose()

        for junk_div in soup.find_all(
            attrs={
                "class": re.compile(r"(ads|advert|banner|nav|pagination|comment|social|breadcrumb)", re.I)
            }
        ):
            junk_div.decompose()

        for el in soup.find_all(["span", "div", "a", "li"]):
            el_text = el.get_text(strip=True)
            if re.match(r"(?i)^(bản\s*dịch|dịch\s*ai|convert|bản\s*dịch\s*dịch\s*ai\s*convert)$", el_text):
                el.decompose()

        content_node = None
        for selector in cls.CONTENT_SELECTORS:
            node = soup.select_one(selector)
            if node and len(node.get_text(strip=True)) > 200:
                content_node = node
                break

        if not content_node:
            best_div = None
            max_len = 0
            for div in soup.find_all(["div", "main", "section"]):
                text_len = len(div.get_text(strip=True))
                if text_len > max_len:
                    max_len = text_len
                    best_div = div
            content_node = best_div or soup.body

        if not content_node:
            raise ValueError("Không tìm thấy nội dung văn bản trên trang web này.")

        for br in content_node.find_all("br"):
            br.replace_with("\n")
        for block_tag in content_node.find_all(["p", "div", "h2", "h3", "h4", "li"]):
            block_tag.append("\n")

        raw_text = content_node.get_text()
        cleaned_text = cls._clean_text(raw_text)

        if len(cleaned_text) < 50:
            raise ValueError("Nội dung bóc tách được quá ngắn.")

        chunks = cls.split_into_chunks(cleaned_text, max_chars=450)

        if chunks:
            first_chunk_lower = chunks[0].lower().strip()
            title_lower = title.lower().strip()
            if not first_chunk_lower.startswith(title_lower):
                chunks.insert(0, f"{title}.")

        return title, chunks, prev_url, next_url, cover_url

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
            if node:
                text = re.sub(r"\s+", " ", node.get_text(strip=True))
                if text and len(text) > 1:
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
            if not tag:
                return None
            classes = " ".join(tag.get("class", [])).lower()
            if "disabled" in classes or "isdisabled" in classes or tag.has_attr("disabled"):
                return None
            href = (tag.get("href") or "").strip()
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
                title_attr = (a_tag.get("title") or "").lower()
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
        lines = text.splitlines()
        cleaned_lines = []

        for line in lines:
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
