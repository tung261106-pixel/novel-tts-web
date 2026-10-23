# File: web_app.py
import asyncio
import io
import json
import os
import re
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


class ContextualTextPolisher:
    """
    Tầng 1: Bộ lọc Ngữ cảnh Tiếng Việt & Tiên Hiệp tức thì.
    Phân tích các từ đứng trước/sau để khôi phục dấu thanh và chữ cái bị thiếu.
    """

    CONTEXT_PHRASE_FIXES = [
        # Khôi phục "nao khi" -> "não khỉ" và các ngữ cảnh liên quan
        (r"\bnao\s+khi\b", "não khỉ"),
        (r"\bnão\s+khi\b", "não khỉ"),
        (r"\bnao\s+khỉ\b", "não khỉ"),
        (r"\b(ăn|húp|múc|bổ|lấy|thưởng\s+thức|món)\s+nao\b", r"\1 não"),
        # Nhận diện "khi" -> "khỉ" khi đi kèm từ chỉ con vật / loài vật
        (r"\b(con|lũ|bầy|đám|một\s+con|hai\s+con|ba\s+con)\s+khi\b", r"\1 khỉ"),
        (r"\b(chó|gà|heo|lợn|ngựa|trâu|bò|chim|rắn|hổ|báo)\s*,\s*khi\b", r"\1, khỉ"),
        (r"\bkhi\s+(vàng|đen|trắng|đột\s+biến|thái\s+cổ|vương|con|nhỏ)\b", r"khỉ \1"),
        # Các lỗi từ láy & dấu thanh phổ biến trong truyện dịch
        (r"\bngoan\s+ngoản\b", "ngoan ngoãn"),
        (r"\blăn\s+tan\b", "lăn tăn"),
        (r"\bvô\s+cũng\b", "vô cùng"),
        (r"\b(như|vì|đúng|thế|cũng|chính)\s+vây\b", r"\1 vậy"),
        (r"\bxẩy\s+ra\b", "xảy ra"),
        (r"\blàm\s+cái\s+gi\b", "làm cái gì"),
        (r"\bkhông\s+biết\s+la\b", "không biết là"),
        (r"\bnhư\s+thế\s+nao\b", "như thế nào"),
        (r"\bmột\s+chut\b", "một chút"),
        (r"\bchẳn\s+lẽ\b", "chẳng lẽ"),
        (r"\bkhổng\s+lổ\b", "khổng lồ"),
        (r"\brỏ\s+ràng\b", "rõ ràng"),
        (r"\bdể\s+dàng\b", "dễ dàng"),
        (r"\bvội\s+vàn\b", "vội vàng"),
        (r"\blỏng\s+lẽo\b", "lỏng lẻo"),
        # Các từ Tiên hiệp / Huyền huyễn bị thiếu chữ cái theo ngữ cảnh
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
    ]

    @classmethod
    def polish_chunk(cls, text: str, custom_dict: dict = None) -> str:
        if not text:
            return ""

        result = text

        # 1. Chuẩn hóa khoảng trắng quanh dấu câu
        result = re.sub(r",\s*,+", ",", result)
        result = re.sub(r"\.\s*\.(?!\.)", ".", result)
        result = re.sub(r"([,;:!?])([^\s\d\"'”’)])", r"\1 \2", result)
        result = re.sub(r"\s+([,.;:!?])", r"\1", result)

        # 2. Sửa các cụm từ sai dấu / mất chữ theo ngữ cảnh
        for pattern, replacement in cls.CONTEXT_PHRASE_FIXES:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        # 3. Áp dụng Từ điển tùy chỉnh của người dùng
        if custom_dict and isinstance(custom_dict, dict):
            for wrong_word, right_word in custom_dict.items():
                w = wrong_word.strip()
                r = right_word.strip()
                if w and r:
                    pattern = rf"(?<!\w){re.escape(w)}(?!\w)"
                    result = re.sub(pattern, r, result, flags=re.IGNORECASE)

        return re.sub(r"\s+", " ", result).strip()


async def _synthesize_audio_bytes(text: str, voice: str, rate_str: str) -> bytes:
    """
    Chuyển văn bản thành âm thanh MP3 trực tiếp trên RAM.
    Có cơ chế tự thử lại 2 lần nếu kết nối tới máy chủ Edge-TTS bị trễ.
    """
    last_err = None
    for attempt in range(2):
        try:
            communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate_str)
            audio_buffer = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_buffer.write(chunk["data"])
            data = audio_buffer.getvalue()
            if len(data) > 100:
                return data
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.3)
    raise RuntimeError(f"Không thể tạo âm thanh TTS: {last_err}")


@app.route("/")
def index():
    """Trang chủ hiển thị giao diện đọc truyện."""
    return render_template("index.html")


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    """API tải chương truyện, lọc ngữ cảnh Tầng 1 và lưu vào lịch sử."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    custom_dict = data.get("custom_dict") or {}

    if not url:
        return jsonify({"error": "Vui lòng nhập đường dẫn (URL) của chương truyện!"}), 400

    try:
        scrape_res = NovelScraper.fetch_chapter(url)
        title, raw_chunks, prev_url, next_url = scrape_res[:4]

        chunks = [ContextualTextPolisher.polish_chunk(c, custom_dict) for c in raw_chunks]
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
    Tầng 2: API dùng Gemini AI đọc hiểu ngữ cảnh câu để sửa các từ bị mất dấu thanh
    (VD: 'nao khi' -> 'não khỉ', 'con khi' -> 'con khỉ'), từ bị thiếu chữ ('thán khí' -> 'Thánh khí')
    và sai dấu hỏi/ngã. Hỗ trợ xoay vòng nhiều API Key.
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

    if not chunks or not api_keys:
        return jsonify({"chunks": chunks, "used_key_index": 0, "next_key_index": 0})

    indexed_input = {str(i): text for i, text in enumerate(chunks)}

    prompt = (
        "Bạn là biên tập viên hiệu đính tiểu thuyết Tiên Hiệp / Huyền Huyễn tiếng Việt.\n"
        "NHIỆM VỤ: Đọc hiểu ngữ cảnh từng câu trong JSON đầu vào và sửa các lỗi chính tả, mất dấu, thiếu chữ:\n"
        "1. LỖI MẤT DẤU THANH THEO NGỮ CẢNH: Phải dựa vào ý nghĩa cả câu để thêm dấu đúng. "
        "Ví dụ: 'nao khi' -> 'não khỉ', 'con khi kia' -> 'con khỉ kia', 'như vây' -> 'như vậy', 'vô cũng' -> 'vô cùng'.\n"
        "2. LỖI TỪ LÁY & DẤU HỎI/NGÃ: Ví dụ 'ngoan ngoản' -> 'ngoan ngoãn', 'lăn tan' -> 'lăn tăn', 'rỏ ràng' -> 'rõ ràng'.\n"
        "3. LỖI THIẾU CHỮ CÁI TRONG TỪ HÁN VIỆT / TU TIÊN: Ví dụ vũ khí 'thán khí' -> 'Thánh khí', 'đế bin' -> 'Đế binh', 'thần thưc' -> 'thần thức'.\n"
        "4. LỖI DẤU CÂU: Sửa dấu phẩy, dấu chấm đặt sai chỗ làm đứt câu.\n\n"
        "QUY TẮC BẮT BUỘC:\n"
        "- Giữ nguyên văn phong gốc, tên riêng nhân vật, chiêu thức và tuyệt đối không tóm tắt hay cắt bớt câu.\n"
        "- Trả về DUY NHẤT một JSON Object có các khóa ('0', '1', ...) khớp chính xác với đầu vào:\n\n"
        + json.dumps(indexed_input, ensure_ascii=False)
    )

    models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash"]
    num_keys = len(api_keys)

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
                resp = requests.post(gemini_url, json=payload, timeout=30)

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
                        })
            except Exception:
                continue

    # Nếu tất cả Key đều bận, trả về nguyên vẹn văn bản Tầng 1 để app không bao giờ báo lỗi
    return jsonify({
        "chunks": chunks,
        "used_key_index": start_key_index,
        "next_key_index": start_key_index,
    })


@app.route("/api/tts", methods=["GET"])
def api_tts():
    """API tạo luồng âm thanh MP3 cho từng đoạn văn."""
    text = (request.args.get("text") or "").strip()
    voice = request.args.get("voice", "vi-VN-HoaiMyNeural")
    speed = float(request.args.get("speed", 1.0))

    if not text:
        return jsonify({"error": "Không có văn bản để đọc"}), 400

    rate_str = _speed_to_rate_str(speed)
    try:
        mp3_bytes = asyncio.run(_synthesize_audio_bytes(text, voice, rate_str))
        response = send_file(
            io.BytesIO(mp3_bytes),
            mimetype="audio/mpeg",
            as_attachment=False,
            download_name="chunk.mp3",
        )
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response
    except Exception as e:
        return jsonify({"error": f"Lỗi tạo giọng đọc TTS: {e}"}), 500


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
