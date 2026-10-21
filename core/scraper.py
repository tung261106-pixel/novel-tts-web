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
    """
    Bộ bóc tách văn bản truyện chữ:
    1. Ưu tiên tải trực tiếp từ URL gốc của người dùng (truyendich.space).
    2. Khi các chương mới (> 1188) trên truyendich.space bật khóa 403 / 'Kiểm tra linh căn' / Chữ tượng hình,
       tự động xuyên qua khóa bảo mật để lấy đúng nội dung Tiếng Việt của chương đó.
    """

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.google.com.vn/",
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

    # Các dòng rác giao diện hoặc thông báo chặn Bot ("Kiểm tra linh căn") cần loại bỏ hoàn toàn
    JUNK_PATTERNS = [
        r"(?i)^\s*(chương\s+trước|chương\s+sau|chương\s+tiếp|mục\s+lục|báo\s+lỗi\s+chương|bình\s+luận)\s*$",
        r"(?i)^\s*(bản\s*dịch|dịch\s*ai|convert|\s|/|\|)+$",
        r"(?i).*xác\s+thực\s+bảo\s+mật.*",
        r"(?i).*đang\s+kiểm\s+tra\s+linh\s+căn.*",
        r"(?i).*quá\s+trình\s+này\s+diễn\s+ra\s+tự\s+động.*",
        r"(?i).*quản\s+lý\s+tên\s+riêng.*",
        r"(?i).*bôi\s+đen\s+chữ\s+trong\s+chương\s+truyện.*",
        r"(?i).*không\s+tìm\s+thấy\s+chương\s+phù\s+hợp.*",
        r"(?i)hãy\s+bấm\s+vào\s+đây\s+để\s+báo\s+lỗi.*",
        r"(?i)nguồn\s*:\s*http.*",
        r"(?i)truyện\s+được\s+đăng\s+tại.*",
        r"(?i)quảng\s+cáo.*",
    ]

    # Nhận diện nguyên âm Tiếng Việt có dấu để phân biệt chương truyện thật với trang lỗi/tượng hình
    VN_ACCENT_REGEX = re.compile(
        r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ"
        r"ÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ]"
    )

    # Chỉ giữ lại chữ cái Tiếng Việt, số và dấu câu chuẩn (xóa sạch chữ tượng hình Ai Cập & dấu '_')
    VALID_CHARS_REGEX = re.compile(
        r"[^a-zA-Z0-9\s.,!?;:'\"()\[\]\-–—…“”‘’/\\+%&\u00C0-\u024F\u1E00-\u1EFF]"
    )

    QUICK_FIXES = [
        (r"\bngoan\s+ngoản\b", "ngoan ngoãn"),
        (r"\blăn\s+tan\b", "lăn tăn"),
        (r"\bvô\s+cũng\b", "vô cùng"),
        (r"\bnhư\s+vây\b", "như vậy"),
        (r"\bko\b", "không"),
        (r"\bđc\b", "được"),
    ]

    # Một chương truyện thật bắt buộc phải có ít nhất 1000 ký tự và 180 chữ cái Tiếng Việt có dấu
    MIN_CHAPTER_CHARS = 1000
    MIN_VN_ACCENTS = 180

    @classmethod
    def _count_vn_accents(cls, text: str) -> int:
        if not text:
            return 0
        return len(cls.VN_ACCENT_REGEX.findall(text))

    @classmethod
    def _is_valid_chapter_text(cls, cleaned_text: str) -> bool:
        """Kiểm tra văn bản sau khi lọc có thực sự là nội dung chương truyện Tiếng Việt đầy đủ không."""
        if not cleaned_text or len(cleaned_text) < cls.MIN_CHAPTER_CHARS:
            return False
        if cls._count_vn_accents(cleaned_text) < cls.MIN_VN_ACCENTS:
            return False
        lower_txt = cleaned_text[:500].lower()
        if "đang kiểm tra linh căn" in lower_txt or "không tìm thấy chương phù hợp" in lower_txt:
            return False
        return True

    @classmethod
    def _raw_get_html(cls, url: str, timeout: int = 15) -> str:
        """Tải HTML từ một đường dẫn qua 4 lớp kết nối."""
        # 1. curl_cffi (Tạo phiên mới trên mỗi lần gọi để không bị lưu Cookie lỗi 403)
        if cffi_requests is not None:
            for browser_fp in ["chrome120", "safari17_0", "chrome124"]:
                try:
                    resp = cffi_requests.get(
                        url,
                        headers=cls.HEADERS,
                        impersonate=browser_fp,
                        timeout=timeout,
                    )
                    if resp.status_code == 200 and len(resp.text) > 600:
                        return resp.text
                except Exception:
                    continue

        # 2. Cloudscraper
        if cloudscraper is not None:
            try:
                scraper = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows", "mobile": False}
                )
                resp = scraper.get(url, headers=cls.HEADERS, timeout=timeout)
                if resp.status_code == 200 and len(resp.text) > 600:
                    resp.encoding = resp.apparent_encoding or "utf-8"
                    return resp.text
            except Exception:
                pass

        # 3. Requests chuẩn
        try:
            resp = requests.get(url, headers=cls.HEADERS, timeout=timeout)
            if resp.status_code == 200 and len(resp.text) > 600:
                resp.encoding = resp.apparent_encoding or "utf-8"
                return resp.text
        except Exception:
            pass

        # 4. Cổng Proxy dự phòng
        encoded_url = quote(url, safe="")
        for p_url in [
            f"https://api.allorigins.win/raw?url={encoded_url}",
            f"https://api.codetabs.com/v1/proxy?quest={encoded_url}",
        ]:
            try:
                resp = requests.get(p_url, timeout=timeout)
                if resp.status_code == 200 and len(resp.text) > 600:
                    resp.encoding = "utf-8"
                    return resp.text
            except Exception:
                continue

        return ""

    @classmethod
    def _parse_html_to_chapter(
        cls, html_text: str, base_url: str
    ) -> Optional[Tuple[str, str, Optional[str], Optional[str]]]:
        """
        Bóc tách HTML thành (title, cleaned_text, prev_url, next_url).
        Trả về None nếu trang HTML chỉ chứa chữ tượng hình hoặc màn hình 'Kiểm tra linh căn'.
        """
        if not html_text or len(html_text) < 600:
            return None

        soup = BeautifulSoup(html_text, "lxml")
        prev_url, next_url = cls._extract_nav_links(soup, base_url)

        for tag in soup(["script", "style", "nav", "footer", "iframe", "noscript", "aside", "form", "svg"]):
            tag.decompose()

        title = cls._extract_chapter_title(soup, base_url)

        for tag in soup(["header", "button", "h1"]):
            tag.decompose()

        for junk_div in soup.find_all(
            attrs={
                "class": re.compile(r"(ads|advert|banner|nav|pagination|comment|social|breadcrumb)", re.I)
            }
        ):
            junk_div.decompose()

        # Tìm khung chứa nhiều văn bản Tiếng Việt thật nhất
        content_node = None
        for selector in cls.CONTENT_SELECTORS:
            node = soup.select_one(selector)
            if node:
                test_clean = cls._clean_text(node.get_text("\n"))
                if cls._is_valid_chapter_text(test_clean):
                    content_node = node
                    break

        if not content_node:
            best_div = None
            max_vn_score = 0
            for div in soup.find_all(["div", "main", "section", "article"]):
                test_clean = cls._clean_text(div.get_text("\n"))
                vn_score = cls._count_vn_accents(test_clean)
                if cls._is_valid_chapter_text(test_clean) and vn_score > max_vn_score:
                    max_vn_score = vn_score
                    best_div = div
            content_node = best_div

        if not content_node:
            return None

        for br in content_node.find_all("br"):
            br.replace_with("\n")
        for block_tag in content_node.find_all(["p", "div", "h2", "h3", "h4", "li"]):
            block_tag.append("\n")

        cleaned_text = cls._clean_text(content_node.get_text())
        if not cls._is_valid_chapter_text(cleaned_text):
            return None

        return title, cleaned_text, prev_url, next_url

    @classmethod
    def _fetch_unlocked_fallback(cls, original_url: str) -> Optional[Tuple[str, str]]:
        """
        Khi truyendich.space khóa các chương > 1188 bằng lỗi 403 / 'Kiểm tra linh căn' / Chữ tượng hình,
        tự động lấy nội dung chương đó qua Trình duyệt ảo Jina hoặc nguồn văn bản sạch cùng chương.
        """
        # 1. Thử chạy trình duyệt ảo Jina chờ qua màn hình JavaScript trên chính URL gốc
        try:
            jina_resp = requests.get(
                f"https://r.jina.ai/{original_url}",
                headers={
                    "Accept": "text/plain",
                    "X-Wait-For-Selector": "article, #chapter-c, .chapter-c",
                    "X-Timeout": "15",
                },
                timeout=18,
            )
            if jina_resp.status_code == 200 and len(jina_resp.text) > 1000:
                raw_jina = jina_resp.text
                if "Markdown Content:" in raw_jina:
                    raw_jina = raw_jina.split("Markdown Content:", 1)[1]
                cleaned_jina = cls._clean_text(raw_jina)
                if cls._is_valid_chapter_text(cleaned_jina):
                    m_url = re.search(r"(?:chuong|chapter)-(\d+)", original_url, re.I)
                    j_title = f"Chương {m_url.group(1)}" if m_url else "Chương truyện"
                    return j_title, cleaned_jina
        except Exception:
            pass

        # 2. Lấy nội dung chương tương ứng từ nguồn không khóa (khi truyendich.space chặn IP máy chủ)
        m = re.search(
            r"/(?:doc-truyen/|truyen/)?([a-zA-Z0-9-]+)/(?:chuong|chapter)-(\d+)",
            original_url,
            re.I,
        )
        if not m:
            return None

        novel_slug = m.group(1).lower().strip()
        chap_num = m.group(2).strip()

        backup_urls = [
            f"https://truyenfull.vision/{novel_slug}/chuong-{chap_num}/",
            f"https://truyenfull.io/{novel_slug}/chuong-{chap_num}/",
            f"https://truyenfull.vn/{novel_slug}/chuong-{chap_num}/",
            f"https://truyenfull.bio/{novel_slug}/chuong-{chap_num}/",
            f"https://sstruyen.vn/{novel_slug}/chuong-{chap_num}/",
        ]

        for b_url in backup_urls:
            html = cls._raw_get_html(b_url, timeout=12)
            if not html:
                continue
            parsed = cls._parse_html_to_chapter(html, b_url)
            if parsed is not None:
                b_title, b_cleaned_text, _, _ = parsed
                return b_title, b_cleaned_text

        return None

    @classmethod
    def fetch_chapter(cls, url: str, timeout: int = 20) -> Tuple[str, List[str], Optional[str], Optional[str]]:
        """
        Tải và bóc tách chương truyện:
        - Bước 1: Tải trực tiếp từ URL người dùng dán (hoạt động ngay với các chương không khóa như 1159).
        - Bước 2: Nếu chương bị khóa bảo mật (như các chương > 1188 trên truyendich.space),
                  tự động lấy nội dung đã mở khóa và giữ nguyên link điều hướng gốc.
        """
        default_prev, default_next = cls._compute_url_nav_links(url)

        # Bước 1: Thử tải trực tiếp từ link gốc
        html_text = cls._raw_get_html(url, timeout=12)
        if html_text:
            parsed = cls._parse_html_to_chapter(html_text, url)
            if parsed is not None:
                title, cleaned_text, prev_url, next_url = parsed
                chunks = cls._finalize_chunks(title, cleaned_text)
                return title, chunks, (prev_url or default_prev), (next_url or default_next)

        # Bước 2: Nếu link gốc bị khóa 403 / 'Kiểm tra linh căn' / Chữ tượng hình (các chương > 1188)
        unlocked = cls._fetch_unlocked_fallback(url)
        if unlocked is not None:
            title, cleaned_text = unlocked
            chunks = cls._finalize_chunks(title, cleaned_text)
            return title, chunks, default_prev, default_next

        raise ValueError(f"Không thể kết nối tới trang truyện (Lỗi 403/Timeout): {url}")

    @classmethod
    def _finalize_chunks(cls, title: str, cleaned_text: str) -> List[str]:
        chunks = cls.split_into_chunks(cleaned_text, max_chars=420)
        if chunks:
            first_chunk_lower = chunks[0].lower().strip()
            title_lower = title.lower().strip()
            if not first_chunk_lower.startswith(title_lower):
                chunks.insert(0, f"{title}.")
        return chunks

    @classmethod
    def _compute_url_nav_links(cls, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Tính link Chương Trước & Chương Tiếp trên chính tên miền gốc của người dùng."""
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
            if node:
                text = re.sub(r"\s+", " ", node.get_text(strip=True))
                if text and len(text) > 1 and "xác thực" not in text.lower():
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

        if not next_url or not prev_url:
            fallback_prev, fallback_next = cls._compute_url_nav_links(base_url)
            prev_url = prev_url or fallback_prev
            next_url = next_url or fallback_next

        return prev_url, next_url

    @classmethod
    def _clean_text(cls, text: str) -> str:
        lines = text.splitlines()
        cleaned_lines = []

        for raw_line in lines:
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            if raw_line.count("_") >= 3:
                continue

            line = cls.VALID_CHARS_REGEX.sub("", raw_line)
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
            if is_junk:
                continue

            line = re.sub(r",\s*,+", ",", line)
            line = re.sub(r"\.\s*\.(?!\.)", ".", line)
            for pattern, replacement in cls.QUICK_FIXES:
                line = re.sub(pattern, replacement, line, flags=re.IGNORECASE)

            cleaned_lines.append(line)

        return "\n".join(cleaned_lines)

    @classmethod
    def split_into_chunks(cls, text: str, max_chars: int = 420) -> List[str]:
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
