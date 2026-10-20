# File: web_app.py
import asyncio
import io
import json
import os
import re
import unicodedata
import requests
from flask import Flask, render_template, request, jsonify, send_file
import edge_tts

from core.scraper import NovelScraper
from utils.storage import HistoryManager

app = Flask(__name__)


def _speed_to_rate_str(speed: float) -> str:
    """Chuyển hệ số tốc độ (0.5 - 2.0) thành chuỗi phần trăm cho edge-tts."""
    percent = int(round((float(speed) - 1.0) * 100))
    return f"+{percent}%" if percent >= 0 else f"{percent}%"


def _slugify_filename(text: str) -> str:
    """Chuyển tiêu đề chương thành tên file MP3 an toàn không dấu."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip()
    text = re.sub(r"[-\s]+", "-", text)
    return text[:80] or "chuong-truyen"


class ContextualTextPolisher:
    """
    Tầng 1: Bộ lọc Ngữ cảnh Tiếng Việt & Tiên Hiệp tức thì (Chạy ngay khi tải chương).
    Sử dụng biểu thức chính quy có kiểm tra từ đứng trước/sau (Lookahead / Lookbehind)
    để sửa lỗi từ láy, lỗi mất dấu, thiếu chữ mà không sửa nhầm ngữ cảnh.
    """

    # 1. Sửa lỗi từ láy, chính tả và mất dấu thanh phổ biến theo cụm ngữ cảnh
    CONTEXT_PHRASE_FIXES = [
        # Các lỗi xuất hiện trực tiếp trong ảnh Chương 1179 Già Thiên
        (r"\bngoan\s+ngoản\b", "ngoan ngoãn"),
        (r"\blăn\s+tan\b", "lăn tăn"),
        (r"\bvô\s+cũng\b", "vô cùng"),
        (r"\bnhư\s+vây\b", "như vậy"),
        (r"\bvì\s+vây\b", "vì vậy"),
        (r"\bđúng\s+vây\b", "đúng vậy"),
        (r"\bthế\s+vây\b", "thế vậy"),
        # Nhận diện "con khi" -> "con khỉ" theo ngữ cảnh danh từ chỉ con vật (không sửa "một khi", "trước khi")
        (r"\b(con|lũ|bầy|đám)\s+khi\s+(kia|này|đó|ấy|thôi|nhỏ|vàng|đen|trắng)\b", r"\1 khỉ \2"),
        (r"\b(con|một\s+con)\s+khi\b(?=\s+(?:kia|thôi|mà|này|đó|lợi\s+hại|biến\s+thái))", r"\1 khỉ"),
        (r"\b(chó|gà|heo|lợn|ngựa|trâu|bò)\s*,\s*khi\b", r"\1, khỉ"),
        # Nhận diện "thán khí" -> "Thánh khí" khi mang ngữ cảnh pháp bảo/vũ khí (phân biệt với "thán khí một tiếng" = thở dài)
        (
            r"\b(đánh\s+nứt(?:\s+cả)?|đánh\s+vỡ|tế\s+ra|khống\s+chế|một\s+kiện|hai\s+kiện|mảnh\s+vỡ|uy\s+năng|luyện\s+chế|cổ|truyền\s+thế|cấp|chuẩn\s+đế|vũ\s+khí)\s+thán\s+khí\b",
            r"\1 Thánh khí",
        ),
        (
            r"\bthán\s+khí\b(?=\s+(?:cổ\s+xưa|của\s+hắn|của\s+mình|bị\s+đánh|vỡ\s+nát|phát\s+sáng|rung\s+động|bay\s+ra|trấn\s+áp))",
            "Thánh khí",
        ),
        # Các từ Tiên hiệp / Huyền huyễn hay bị mất chữ cái cuối do lỗi convert/bộ gõ
        (r"\b(cực\s+đạo\s+)?đế\s+bin\b", r"\1Đế binh"),
        (r"\bthánh\s+bin\b", "Thánh binh"),
        (r"\bthần\s+thưc\b", "thần thức"),
        (r"\bthần\s+thuc\b", "thần thức"),
        (r"\bcảnh\s+giơi\b", "cảnh giới"),
        (r"\btruyền\s+thưa\b", "truyền thừa"),
        (r"\bthiên\s+kiêp\b", "thiên kiếp"),
        (r"\bdị\s+tương\b", "dị tượng"),
        (r"\bbất\s+tư\s+(thiên\s+hoàng|thần\s+dược|dược|thể|thần|kinh)\b", r"Bất Tử \1"),
        # Các lỗi chính tả / dấu thanh tiếng Việt phổ biến khác
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
            # Sửa các dấu chấm/phẩy lặp vô nghĩa
            result = re.sub(r",\s*,+", ",", result)
            result = re.sub(r";\s*;+", ";", result)
            result = re.sub(r"\.\s*\.(?!\.)", ".", result)

            # Khoảng trắng bị dính sau dấu câu
            result = re.sub(r"([,;:!?])([^\s\d\"'”’)])", r"\1 \2", result)
            result = re.sub(
                r"(?<![\d])\.([A-ZÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ])",
                r". \1",
                result,
            )
            result = re.sub(r"\s+([,.;:!?])", r"\1", result)

            # Sửa các cụm từ sai dấu / thiếu chữ theo ngữ cảnh
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


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    """API nhận URL chương truyện, bóc tách văn bản sạch, lọc ngữ cảnh và lưu lịch sử."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    custom_dict = data.get("custom_dict") or {}
    enable_punct = data.get("enable_punct", True)

    if not url:
        return jsonify({"error": "Vui lòng nhập đường dẫn (URL) của chương truyện!"}), 400

    try:
        title, raw_chunks, prev_url, next_url = NovelScraper.fetch_chapter(url)
        chunks = [
            ContextualTextPolisher.polish_chunk(c, custom_dict, enable_punct)
            for c in raw_chunks
        ]
        history = HistoryManager.add_entry(title, url, total_chunks=len(chunks))
        return jsonify({
            "title": title,
            "url": url,
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
    sai từ láy và chuẩn hóa dấu câu.
    Sử dụng cấu trúc Dictionary theo ID ("0", "1", "2"...) để đảm bảo KHÔNG BAO GIỜ lệch đoạn.
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
        return jsonify({
            "error": "Chưa có API Key! Vui lòng thêm Gemini API Key trong mục AI Ngữ cảnh."
        }), 400

    # Đóng gói các đoạn văn theo mã số cố định {"0": "...", "1": "..."} để không bị lệch câu
    indexed_input = {str(i): text for i, text in enumerate(chunks)}

    prompt = (
        "Bạn là chuyên gia hiệu đính văn học và biên tập viên tiểu thuyết Tiên Hiệp / Huyền Huyễn tiếng Việt (như Già Thiên, Thế Giới Hoàn Mỹ).\n"
        "NHIỆM VỤ: Đọc hiểu kỹ ngữ cảnh của từng câu trong JSON đầu vào và sửa toàn bộ các lỗi sau:\n"
        "1. LỖI TỪ LÁY & DẤU THANH TIẾNG VIỆT: Sửa các từ bị sai dấu hỏi/ngã/sắc/huyền/nặng hoặc sai vần. "
        "Ví dụ: 'ngoan ngoản' -> 'ngoan ngoãn', 'lăn tan' -> 'lăn tăn', 'hối hận vô cũng' -> 'hối hận vô cùng', 'như vây' -> 'như vậy'.\n"
        "2. LỖI MẤT DẤU LÀM BIẾN ĐỔI TỪ THEO NGỮ CẢNH: Phải đọc hiểu câu để khôi phục từ đúng. "
        "Ví dụ: 'con khi kia đánh nứt', 'trẻ con, chó, khi mà lợi hại' -> chữ 'khi' ở đây là con vật nên phải sửa thành 'khỉ' ('con khỉ kia', 'chó, khỉ').\n"
        "3. LỖI THIẾU CHỮ CÁI / NHẦM TỪ HÁN VIỆT TRONG BẢN DỊCH: Đọc ngữ cảnh để khôi phục chữ bị mất. "
        "Ví dụ: 'đánh nứt cả thán khí', 'tế ra thán khí' -> ngữ cảnh là vũ khí/pháp bảo nên phải sửa 'thán khí' thành 'Thánh khí' (chỉ giữ nguyên 'thán khí' nếu ngữ cảnh là thở dài). "
        "Hoặc 'đế bin' -> 'Đế binh', 'thần thưc' -> 'thần thức', 'cảnh giơi' -> 'cảnh giới'.\n"
        "4. LỖI DẤU CÂU: Sửa dấu phẩy, dấu chấm đặt sai chỗ làm đứt mạch đọc.\n\n"
        "QUY TẮC BẮT BUỘC:\n"
        "- Giữ nguyên văn phong truyện, tên nhân vật (Diệp Phàm, Hắc Hoàng...), địa danh và không tự ý tóm tắt hay cắt bớt câu.\n"
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
                    # Loại bỏ markdown ```json nếu có
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

    return jsonify({
        "error": f"Không thể gọi AI ({last_error}). Đã giữ bản lọc NLP."
    }), 500


@app.route("/api/tts", methods=["GET"])
def api_tts():
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
