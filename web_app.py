# File: web_app.py
import asyncio
import io
import json
import os
import re
import unicodedata
import requests
from flask import Flask, render_template, request, jsonify, send_file, Response, stream_with_context
import edge_tts

from core.scraper import NovelScraper
from utils.storage import HistoryManager

app = Flask(__name__)


def _speed_to_rate_str(speed: float) -> str:
    """Chuyển hệ số tốc độ (0.5 - 2.0) thành chuỗi phần trăm cho edge-tts (VD: +25%)."""
    percent = int(round((float(speed) - 1.0) * 100))
    return f"+{percent}%" if percent >= 0 else f"{percent}%"


def _slugify_filename(text: str) -> str:
    """Chuyển tiêu đề chương thành tên file MP3 an toàn không dấu."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip()
    text = re.sub(r"[-\s]+", "-", text)
    return text[:80] or "chuong-truyen"


class BGMStreamer:
    """
    Hệ thống tìm kiếm và phát luồng nhạc nền MP3 thật (Cổ Cầm, Đàn Tranh, Tiêu Sáo,
    Huyền Huyễn, Mưa Đêm, Lofi) từ kho lưu trữ âm nhạc trực tuyến Internet Archive.
    """

    GENRE_QUERIES = {
        "tienhiep": '(title:(guqin OR guzheng OR "chinese zither" OR "chinese classical") OR subject:(guqin OR guzheng)) AND mediatype:audio',
        "tieusao": '(title:(dizi OR xiao OR "chinese flute" OR "bamboo flute" OR erhu) OR subject:("chinese flute" OR erhu)) AND mediatype:audio',
        "huyenhuyen": '(title:("dark ambient" OR "space ambient" OR "dungeon synth" OR "ambient drone") OR subject:("dark ambient" OR "space ambient")) AND mediatype:audio',
        "muadem": '(title:("rain sound" OR "gentle rain" OR "night rain" OR "rainstorm") AND NOT title:music) AND mediatype:audio',
        "lofi": '(title:("lofi chill" OR "chillhop" OR "relaxing piano" OR "ambient piano")) AND mediatype:audio',
    }

    FALLBACK_TRACKS = {
        "tienhiep": [
            "https://archive.org/download/chinese-classical-music-guzheng/Chinese%20Classical%20Music%20-%20Guzheng.mp3",
            "https://archive.org/download/guqin-music/01.%20Gao%20Shan%20Liu%20Shui%20%28High%20Mountains%20and%20Flowing%20Water%29.mp3",
        ],
        "tieusao": [
            "https://archive.org/download/chinese-bamboo-flute-music/Chinese%20Bamboo%20Flute%20Music.mp3",
        ],
        "huyenhuyen": [
            "https://archive.org/download/AmbientMusicCollection/Space%20Ambient.mp3",
        ],
        "muadem": [
            "https://archive.org/download/relaxing-rain-sounds/Relaxing%20Rain%20Sounds.mp3",
        ],
        "lofi": [
            "https://archive.org/download/lofi-chill-beats/Lofi%20Chill%20Beats.mp3",
        ],
    }

    _resolved_cache = {}

    @classmethod
    def get_tracks_for_genre(cls, genre: str) -> list:
        """Lấy danh sách các link file MP3 thật cho thể loại được chọn."""
        if genre in cls._resolved_cache and cls._resolved_cache[genre]:
            return cls._resolved_cache[genre]

        query = cls.GENRE_QUERIES.get(genre)
        found_urls = []

        if query:
            try:
                search_url = "https://archive.org/advancedsearch.php"
                params = {
                    "q": query,
                    "fl[]": ["identifier"],
                    "rows": 12,
                    "page": 1,
                    "output": "json",
                }
                resp = requests.get(search_url, params=params, timeout=10)
                if resp.status_code == 200:
                    docs = resp.json().get("response", {}).get("docs", [])
                    for doc in docs:
                        ident = doc.get("identifier")
                        if not ident:
                            continue
                        meta_resp = requests.get(f"https://archive.org/metadata/{ident}", timeout=6)
                        if meta_resp.status_code != 200:
                            continue
                        files = meta_resp.json().get("files", [])
                        for f in files:
                            fname = f.get("name", "")
                            size = int(f.get("size") or 0)
                            if fname.lower().endswith(".mp3") and 800_000 <= size <= 15_000_000:
                                mp3_url = f"https://archive.org/download/{ident}/{requests.utils.quote(fname)}"
                                found_urls.append(mp3_url)
                                break
                        if len(found_urls) >= 6:
                            break
            except Exception:
                pass

        if not found_urls:
            found_urls = cls.FALLBACK_TRACKS.get(genre, [])

        cls._resolved_cache[genre] = found_urls
        return found_urls


class ContextualTextPolisher:
    """
    Tầng 1: Bộ lọc Ngữ cảnh Tiếng Việt & Tiên Hiệp tức thì.
    Sử dụng biểu thức chính quy có kiểm tra từ đứng trước/sau để sửa lỗi từ láy,
    lỗi mất dấu, thiếu chữ mà không sửa nhầm ngữ cảnh.
    """

    CONTEXT_PHRASE_FIXES = [
        (r"\bngoan\s+ngoản\b", "ngoan ngoãn"),
        (r"\blăn\s+tan\b", "lăn tăn"),
        (r"\bvô\s+cũng\b", "vô cùng"),
        (r"\bnhư\s+vây\b", "như vậy"),
        (r"\bvì\s+vây\b", "vì vậy"),
        (r"\bđúng\s+vây\b", "đúng vậy"),
        (r"\bthế\s+vây\b", "thế vậy"),
        (r"\b(con|lũ|bầy|đám)\s+khi\s+(kia|này|đó|ấy|thôi|nhỏ|vàng|đen|trắng)\b", r"\1 khỉ \2"),
        (r"\b(con|một\s+con)\s+khi\b(?=\s+(?:kia|thôi|mà|này|đó|lợi\s+hại|biến\s+thái))", r"\1 khỉ"),
        (r"\b(chó|gà|heo|lợn|ngựa|trâu|bò)\s*,\s*khi\b", r"\1, khỉ"),
        (
            r"\b(đánh\s+nứt(?:\s+cả)?|đánh\s+vỡ|tế\s+ra|khống\s+chế|một\s+kiện|hai\s+kiện|mảnh\s+vỡ|uy\s+năng|luyện\s+chế|cổ|truyền\s+thế|cấp|chuẩn\s+đế|vũ\s+khí)\s+thán\s+khí\b",
            r"\1 Thánh khí",
        ),
        (
            r"\bthán\s+khí\b(?=\s+(?:cổ\s+xưa|của\s+hắn|của\s+mình|bị\s+đánh|vỡ\s+nát|phát\s+sáng|rung\s+động|bay\s+ra|trấn\s+áp))",
            "Thánh khí",
        ),
        (r"\b(cực\s+đạo\s+)?đế\s+bin\b", r"\1Đế binh"),
        (r"\bthánh\s+bin\b", "Thánh binh"),
        (r"\bthần\s+thưc\b", "thần thức"),
        (r"\bthần\s+thuc\b", "thần thức"),
        (r"\bcảnh\s+giơi\b", "cảnh giới"),
        (r"\btruyền\s+thưa\b", "truyền thừa"),
        (r"\bthiên\s+kiêp\b", "thiên kiếp"),
        (r"\bdị\s+tương\b", "dị tượng"),
        (r"\bbất\s+tư\s+(thiên\s+hoàng|thần\s+dược|dược|thể|thần|kinh)\b", r"Bất Tử \1"),
        (r"\bxẩy\s+ra\b", "xảy ra"),
        (r"\blàm\s+cái\s+gi\b", "làm cái gì"),
        (r"\bkhông\s+biết\s+la\b", "không biết là"),
        (r"\bcũng\s+la\b", "cũng là"),
        (r"\bchính\s+la\b", "chính là"),
        (r"\bnhư\s+thế\s+nao\b", "như thế nào"),
        (r"\blàm\s+sao\s+bây\s+giơ\b", "làm sao bây giờ"),
        (r"\bmột\s+chut\b", "một chút"),
        (r"\bngươi\s+nghỉ\s+sao\b", "ngươi nghĩ sao"),
        (r"\bchẳn\s+lẽ\b", "chẳng lẽ"),
        (r"\bkhổng\s+lổ\b", "khổng lồ"),
        (r"\brỏ\s+ràng\b", "rõ ràng"),
        (r"\bdể\s+dàng\b", "dễ dàng"),
        (r"\bvội\s+vàn\b", "vội vàng"),
        (r"\blỏng\s+lẽo\b", "lỏng lẻo"),
    ]

    @classmethod
    def polish_chunk(cls, text: str, custom_dict: dict = None, enable_context_punct: bool = True) -> str:
        if not text:
            return ""

        result = text

        if enable_context_punct:
            result = re.sub(r",\s*,+", ",", result)
            result = re.sub(r";\s*;+", ";", result)
            result = re.sub(r"\.\s*\.(?!\.)", ".", result)

            result = re.sub(r"([,;:!?])([^\s\d\"'”’)])", r"\1 \2", result)
            result = re.sub(
                r"(?<![\d])\.([A-ZÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ])",
                r". \1",
                result,
            )
            result = re.sub(r"\s+([,.;:!?])", r"\1", result)

            for pattern, replacement in cls.CONTEXT_PHRASE_FIXES:
                result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        if custom_dict and isinstance(custom_dict, dict):
            for wrong_word, right_word in custom_dict.items():
                w = wrong_word.strip()
                r = right_word.strip()
                if w and r:
                    pattern = rf"(?<!\w){re.escape(w)}(?!\w)"
                    result = re.sub(pattern, r, result, flags=re.IGNORECASE)

        return re.sub(r"\s+", " ", result).strip()


async def _synthesize_audio_bytes(text: str, voice: str, rate_str: str) -> bytes:
    """Chuyển văn bản thành luồng dữ liệu âm thanh MP3 trực tiếp trên RAM."""
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate_str)
    audio_buffer = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_buffer.write(chunk["data"])
    return audio_buffer.getvalue()


@app.route("/")
def index():
    """Trang chủ hiển thị giao diện đọc truyện."""
    return render_template("index.html")


@app.route("/api/bgm/stream", methods=["GET"])
def api_bgm_stream():
    """
    API phát luồng nhạc nền MP3 thật (Proxy Stream) giúp vượt lỗi CORS và cho phép
    điều chỉnh âm lượng mượt mà trên cả iPhone (iOS Safari) lẫn Máy tính.
    """
    genre = request.args.get("genre", "tienhiep")
    track_idx = int(request.args.get("track", 0))
    custom_url = (request.args.get("custom_url") or "").strip()

    target_url = custom_url
    if not target_url:
        tracks = BGMStreamer.get_tracks_for_genre(genre)
        if not tracks:
            return jsonify({"error": "Không tìm thấy bản nhạc nền"}), 404
        target_url = tracks[track_idx % len(tracks)]

    try:
        upstream = requests.get(
            target_url,
            stream=True,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        upstream.raise_for_status()

        headers = {
            "Content-Type": upstream.headers.get("Content-Type", "audio/mpeg"),
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        }
        if "Content-Length" in upstream.headers:
            headers["Content-Length"] = upstream.headers["Content-Length"]

        return Response(
            stream_with_context(upstream.iter_content(chunk_size=16384)),
            headers=headers,
        )
    except Exception as e:
        return jsonify({"error": f"Lỗi tải nhạc nền: {e}"}), 500


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    """API nhận URL chương truyện, bóc tách văn bản sạch, ảnh bìa và lưu lịch sử."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    custom_dict = data.get("custom_dict") or {}
    enable_punct = data.get("enable_punct", True)

    if not url:
        return jsonify({"error": "Vui lòng nhập đường dẫn (URL) của chương truyện!"}), 400

    try:
        # Tương thích an toàn với cả bản scraper.py trả về 4 hoặc 5 giá trị
        scrape_result = NovelScraper.fetch_chapter(url)
        if len(scrape_result) >= 5:
            title, raw_chunks, prev_url, next_url, cover_url = scrape_result[:5]
        else:
            title, raw_chunks, prev_url, next_url = scrape_result[:4]
            cover_url = ""

        chunks = [
            ContextualTextPolisher.polish_chunk(c, custom_dict, enable_punct)
            for c in raw_chunks
        ]

        # Tương thích an toàn với cả bản storage.py cũ hoặc mới
        try:
            history = HistoryManager.add_entry(
                title, url, total_chunks=len(chunks), cover_url=cover_url
            )
        except TypeError:
            history = HistoryManager.add_entry(title, url, total_chunks=len(chunks))

        return jsonify({
            "title": title,
            "url": url,
            "cover_url": cover_url,
            "chunks": chunks,
            "prev_url": prev_url,
            "next_url": next_url,
            "history": history,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai-polish", methods=["POST"])
def api_ai_polish():
    """
    Tầng 2: API dùng Gemini AI đọc hiểu ngữ cảnh sâu để sửa từ bị thiếu chữ, sai dấu thanh,
    sai từ láy và chuẩn hóa dấu câu (Hỗ trợ tự động xoay vòng nhiều API Key).
    """
    data = request.get_json(silent=True) or {}
    chunks = data.get("chunks") or []
    start_key_index = int(data.get("start_key_index") or 0)

    raw_keys = data.get("api_keys") or []
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]
    api_keys = [k.strip() for k in raw_keys if isinstance(k, str) and k.strip()]

    env_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if env_key and env_key not in api_keys:
        api_keys.append(env_key)

    if not chunks:
        return jsonify({"error": "Chưa có nội dung để xử lý."}), 400

    if not api_keys:
        return jsonify({"error": "Chưa có API Key!"}), 400

    indexed_input = {str(i): text for i, text in enumerate(chunks)}

    prompt = (
        "Bạn là chuyên gia hiệu đính văn học và biên tập viên tiểu thuyết Tiên Hiệp / Huyền Huyễn tiếng Việt (như Già Thiên, Thế Giới Hoàn Mỹ).\n"
        "NHIỆM VỤ: Đọc hiểu kỹ ngữ cảnh của từng câu trong JSON đầu vào và sửa toàn bộ các lỗi sau:\n"
        "1. LỖI TỪ LÁY & DẤU THANH TIẾNG VIỆT: Sửa các từ bị sai dấu hỏi/ngã/sắc/huyền/nặng hoặc sai vần. "
        "Ví dụ: 'ngoan ngoản' -> 'ngoan ngoãn', 'lăn tan' -> 'lăn tăn', 'hối hận vô cũng' -> 'hối hận vô cùng', 'như vây' -> 'như vậy'.\n"
        "2. LỖI MẤT DẤU LÀM BIẾN ĐỔI TỪ THEO NGỮ CẢNH: Phải đọc hiểu câu để khôi phục từ đúng. "
        "Ví dụ: 'con khi kia đánh nứt', 'trẻ con, chó, khi mà lợi hại' -> chữ 'khi' ở đây là con vật nên phải sửa thành 'khỉ' ('con khỉ kia', 'chó, khỉ').\n"
        "3. LỖI THIẾU CHỮ CÁI / NHẦM TỪ HÁN VIỆT TRONG BẢN DỊCH: Đọc ngữ cảnh để khôi phục chữ bị mất. "
        "Ví dụ: 'đánh nứt cả thán khí', 'tế ra thán khí' -> ngữ cảnh là vũ khí/pháp bảo nên phải sửa 'thán khí' thành 'Thánh khí'. "
        "Hoặc 'đế bin' -> 'Đế binh', 'thần thưc' -> 'thần thức', 'cảnh giơi' -> 'cảnh giới'.\n"
        "4. LỖI DẤU CÂU: Sửa dấu phẩy, dấu chấm đặt sai chỗ làm đứt mạch đọc.\n\n"
        "QUY TẮC BẮT BUỘC:\n"
        "- Giữ nguyên văn phong truyện, tên nhân vật, địa danh và không tự ý tóm tắt hay cắt bớt câu.\n"
        "- Trả về DUY NHẤT một JSON Object có các khóa (keys) giữ nguyên chính xác như đầu vào ('0', '1', ...):\n\n"
        + json.dumps(indexed_input, ensure_ascii=False)
    )

    models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash"]
    num_keys = len(api_keys)
    last_error = ""

    for offset in range(num_keys):
        key_idx = (start_key_index + offset) % num_keys
        current_key = api_keys[key_idx]

        for model_name in models_to_try:
            try:
                gemini_url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model_name}:generateContent?key={current_key}"
                )
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "temperature": 0.1,
                    },
                }
                resp = requests.post(gemini_url, json=payload, timeout=35)

                if resp.status_code == 200:
                    res_json = resp.json()
                    raw_output = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
                    raw_output = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_output, flags=re.IGNORECASE)
                    parsed = json.loads(raw_output)

                    polished_chunks = []
                    if isinstance(parsed, dict):
                        for i, orig_text in enumerate(chunks):
                            val = parsed.get(str(i))
                            polished_chunks.append(val.strip() if isinstance(val, str) and val.strip() else orig_text)
                    elif isinstance(parsed, list) and len(parsed) == len(chunks):
                        polished_chunks = [str(x).strip() for x in parsed]

                    if len(polished_chunks) == len(chunks):
                        return jsonify({
                            "chunks": polished_chunks,
                            "used_key_index": key_idx,
                            "next_key_index": (key_idx + 1) % num_keys,
                            "model_used": model_name,
                        })
                else:
                    last_error = f"HTTP {resp.status_code}"
            except Exception as e:
                last_error = str(e)
                continue

    return jsonify({"error": f"Không thể gọi AI ({last_error}). Đã giữ bản lọc NLP."}), 500


@app.route("/api/tts", methods=["GET"])
def api_tts():
    """API tạo và trả về luồng âm thanh MP3 cho từng đoạn văn (chunk)."""
    text = (request.args.get("text") or "").strip()
    voice = request.args.get("voice", "vi-VN-HoaiMyNeural")
    speed = float(request.args.get("speed", 1.0))

    if not text:
        return jsonify({"error": "Không có văn bản để đọc"}), 400

    rate_str = _speed_to_rate_str(speed)
    try:
        mp3_bytes = asyncio.run(_synthesize_audio_bytes(text, voice, rate_str))
        return send_file(
            io.BytesIO(mp3_bytes),
            mimetype="audio/mpeg",
            as_attachment=False,
            download_name="chunk.mp3",
        )
    except Exception as e:
        return jsonify({"error": f"Lỗi tạo giọng đọc TTS: {e}"}), 500


@app.route("/api/export-mp3", methods=["POST"])
def api_export_mp3():
    """API gộp toàn bộ chương truyện thành 1 file Audiobook MP3 duy nhất để tải về máy."""
    data = request.get_json(silent=True) or {}
    chunks = data.get("chunks") or []
    title = (data.get("title") or "chuong-truyen").strip()
    voice = data.get("voice", "vi-VN-HoaiMyNeural")
    speed = float(data.get("speed", 1.0))

    if not chunks:
        return jsonify({"error": "Không có nội dung chương để xuất file MP3!"}), 400

    full_text = "\n\n".join(chunks)
    rate_str = _speed_to_rate_str(speed)
    safe_filename = f"{_slugify_filename(title)}.mp3"

    try:
        mp3_bytes = asyncio.run(_synthesize_audio_bytes(full_text, voice, rate_str))
        return send_file(
            io.BytesIO(mp3_bytes),
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=safe_filename,
        )
    except Exception as e:
        return jsonify({"error": f"Lỗi khi xuất file MP3: {e}"}), 500


@app.route("/api/history", methods=["GET"])
def api_get_history():
    return jsonify(HistoryManager.load_history())


@app.route("/api/history/sync", methods=["POST"])
def api_sync_history():
    data = request.get_json(silent=True) or {}
    client_entries = data.get("history", [])
    merged = HistoryManager.sync_entries(client_entries)
    return jsonify(merged)


@app.route("/api/history/progress", methods=["POST"])
def api_update_progress():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    chunk_index = int(data.get("chunk_index") or 0)
    total_chunks = int(data.get("total_chunks") or 0)
    if not url:
        return jsonify({"error": "Missing url"}), 400
    updated = HistoryManager.update_progress(url, chunk_index, total_chunks)
    return jsonify(updated)


@app.route("/api/history/clear", methods=["POST"])
def api_clear_history():
    HistoryManager.clear_history()
    return jsonify({"status": "cleared"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
