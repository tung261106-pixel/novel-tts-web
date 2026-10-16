# File: core/scraper.py
import re
from typing import Tuple, List, Optional
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup


class NovelScraper:
    """Bộ bóc tách nội dung truyện chữ, tìm link chuyển chương và chia nhỏ văn bản."""

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    CONTENT_SELECTORS = [
        "#chapter-c",            # TruyenFull
        ".chapter-c",
        "#vungdoc",              # TangThuVien / TruyenYY
        ".box-chap",             # TangThuVien
        "#chapter-content",      # Metruyenchu / Wordpress Novel
        ".chapter-content",
        "#content",
        ".entry-content",
        "article",
    ]

    # Các mẫu dòng rác cần loại bỏ hoàn toàn
    JUNK_PATTERNS = [
        r"(?i)^\s*(chương\s+trước|chương\s+sau|chương\s+tiếp|mục\s+lục|báo\s+lỗi\s+chương|bình\s+luận)\s*$",
        r"(?i)^\s*(bản\s*dịch|dịch\s*ai|convert|\s|/|\|)+$",
        r"(?i)hãy\s+bấm\s+vào\s+đây\s+để\s+báo\s+lỗi.*",
        r"(?i)nguồn\s*:\s*http.*",
        r"(?i)truyện\s+được\s+đăng\s+tại.*",
        r"(?i)quảng\s+cáo.*",
    ]

    @classmethod
    def fetch_chapter(cls, url: str, timeout: int = 15) -> Tuple[str, List[str], Optional[str], Optional[str]]:
        """
        Tải trang web từ URL, trích xuất tiêu đề (Số chương + Tên chương),
        các đoạn văn bản đã lọc sạch và link Chương trước / Chương tiếp.
        """
        response = requests.get(url, headers=cls.HEADERS, timeout=timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"

        soup = BeautifulSoup(response.text, "lxml")

        # 1. Trích xuất link Chương trước & Chương sau TRƯỚC KHI xóa thẻ điều hướng
        prev_url, next_url = cls._extract_nav_links(soup, url)

        # 2. Xóa các thẻ script, menu, footer trước (giữ lại <header> để lấy Tên chương)
        for tag in soup(["script", "style", "nav", "footer", "iframe", "noscript", "aside", "form"]):
            tag.decompose()

        # 3. Trích xuất đầy đủ Số chương và Tên chương (VD: "Chương 1050: Dương Đông Kích Tây")
        title = cls._extract_chapter_title(soup, url)

        # 4. Xóa tiếp thẻ <header>, <button>, <h1> và các nút chuyển đổi "Bản Dịch / Dịch AI / Convert"
        for tag in soup(["header", "button", "h1"]):
            tag.decompose()

        for junk_div in soup.find_all(
            attrs={
                "class": re.compile(r"(ads|advert|banner|nav|pagination|comment|social|breadcrumb)", re.I)
            }
        ):
            junk_div.decompose()

        # Tìm và xóa trực tiếp các thẻ chứa chữ Bản Dịch / Dịch AI / Convert
        for el in soup.find_all(["span", "div", "a", "li"]):
            el_text = el.get_text(strip=True)
            if re.match(r"(?i)^(bản\s*dịch|dịch\s*ai|convert|bản\s*dịch\s*dịch\s*ai\s*convert)$", el_text):
                el.decompose()

        # 5. Tìm vùng chứa nội dung chính của truyện
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

        # Chèn ký tự xuống dòng sau các thẻ khối để các chữ không bị dính liền vào nhau
        for br in content_node.find_all("br"):
            br.replace_with("\n")
        for block_tag in content_node.find_all(["p", "div", "h2", "h3", "h4", "li"]):
            block_tag.append("\n")

        raw_text = content_node.get_text()
        cleaned_text = cls._clean_text(raw_text)

        if len(cleaned_text) < 50:
            raise ValueError("Nội dung bóc tách được quá ngắn hoặc trang web yêu cầu JavaScript (SPA).")

        chunks = cls.split_into_chunks(cleaned_text, max_chars=300)

        # 6. Chèn Số chương & Tên chương vào làm đoạn đầu tiên (chunk 0) để phần mềm đọc to ở đầu mỗi chap
        if chunks:
            first_chunk_lower = chunks[0].lower().strip()
            title_lower = title.lower().strip()
            if not first_chunk_lower.startswith(title_lower):
                chunks.insert(0, f"{title}.")

        return title, chunks, prev_url, next_url

    @classmethod
    def _extract_chapter_title(cls, soup: BeautifulSoup, url: str) -> str:
        """
        Trích xuất kết hợp cả Số chương và Tên chương.
        Ví dụ: 'Chương 1050: Dương Đông Kích Tây'
        """
        # 1. Tìm số chương (VD: "Chương 1050")
        chap_num = ""
        # Tìm trên các thẻ hiển thị số chương hoặc trong <title>
        if soup.title and soup.title.get_text(strip=True):
            m = re.search(r"(?i)(chương\s+\d+)", soup.title.get_text(strip=True))
            if m:
                chap_num = m.group(1).title()

        if not chap_num:
            m_url = re.search(r"(?:chuong|chapter)-(\d+)", url, re.I)
            if m_url:
                chap_num = f"Chương {m_url.group(1)}"

        # 2. Tìm tên chương từ thẻ <h1> hoặc class tiêu đề chương
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

        # 3. Ghép Số chương và Tên chương
        if chap_name:
            # Nếu trong chap_name đã có sẵn chữ "Chương X..."
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
        """Tìm đường dẫn Chương trước và Chương tiếp theo trên trang web."""
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
        """Làm sạch văn bản: xóa dòng thừa, cụm 'Bản DịchDịch AIConvert' và các nút điều hướng."""
        lines = text.splitlines()
        cleaned_lines = []

        for line in lines:
            line = re.sub(r"\s+", " ", line).strip()
            if not line:
                continue

            # Cắt bỏ cụm "Bản DịchDịch AIConvert" nếu bị dính ở đầu dòng
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
    def split_into_chunks(cls, text: str, max_chars: int = 300) -> List[str]:
        """Chia văn bản thành các câu/đoạn nhỏ (~300 ký tự) dựa trên dấu kết thúc câu."""
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
