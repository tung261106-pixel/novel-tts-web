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
    Bộ xử lý văn bản thông minh 2 tầng:
    1. Sửa lỗi phát âm theo Từ điển người dùng (kiểm tra ranh giới từ độc lập).
    2. Chuẩn hóa dấu câu & lỗi dấu tiếng Việt dựa trên ngữ cảnh cụm từ xung quanh.
    """

    # Các cụm từ sai dấu phổ biến trong truyện dịch/convert (chỉ sửa khi khớp đúng ngữ cảnh cả cụm)
    CONTEXT_PHRASE_FIXES = [
        (r"\bxẩy\s+ra\b", "xảy ra"),
        (r"\bbổn\s+tọa\b", "bổn tọa"),
        (r"\blàm\s+cái\s+gi\b", "làm cái gì"),
        (r"\bkhông\s+biết\s+la\b", "không biết là"),
        (r"\bcũng\s+la\b", "cũng là"),
        (r"\bchính\s+la\b", "chính là"),
        (r"\bnhư\s+thế\s+nao\b", "như thế nào"),
        (r"\blàm\s+sao\s+bây\s+giơ\b", "làm sao bây giờ"),
        (r"\bmột\s+chut\b", "một chút"),
        (r"\bngươi\s+nghỉ\s+sao\b", "ngươi nghĩ sao"),
    ]

    @classmethod
    def polish_chunk(cls, text: str, custom_dict: dict = None, enable_context_punct: bool = True) -> str:
        if not text:
            return ""

        result = text

        # 1. Sửa dấu câu và ngữ cảnh thông minh
        if enable_context_punct:
            # Sửa các dấu chấm/phẩy lặp vô nghĩa trong bản convert (VD: ", ," hoặc ". ." -> ",")
            result = re.sub(r",\s*,+", ",", result)
            result = re.sub(r";\s*;+", ";", result)
            result = re.sub(r"\.\s*\.(?!\.)", ".", result)  # Giữ nguyên dấu "..." (ba chấm)

            # Khoảng trắng bị dính sau dấu câu (tránh sửa nhầm số thập phân như 1.5 hoặc tên miền)
            result = re.sub(r"([,;:!?])([^\s\d\"'”’)])", r"\1 \2", result)
            result = re.sub(r"(?<![\d])\.([A-ZÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ])", r". \1", result)

            # Xóa khoảng trắng thừa trước dấu câu
            result = re.sub(r"\s+([,.;:!?])", r"\1", result)

            # Sửa các cụm từ sai dấu khi khớp ngữ cảnh từ đi kèm
            for pattern, replacement in cls.CONTEXT_PHRASE_FIXES:
                result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        # 2. Áp dụng Từ điển phát âm tùy chỉnh (Chỉ thay thế khi là từ độc lập, không cắt ngang từ khác)
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
    API sử dụng AI (Gemini 2.5 Flash) để kiểm tra ngữ cảnh và sửa lỗi dấu câu / dấu thanh
    cho các đoạn văn bản bị lỗi convert nặng mà không làm thay đổi cốt truyện hay từ Hán Việt.
    """
    data = request.get_json(silent=True) or {}
    chunks = data.get("chunks") or []
    api_key = (data.get("api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()

    if not chunks:
        return jsonify({"error": "Chưa có nội dung chương để xử lý."}), 400

    if not api_key:
        return jsonify({
            "error": "Vui lòng nhập Gemini API Key miễn phí trong mục Từ điển & AI (hoặc cài biến môi trường GEMINI_API_KEY trên Render)."
        }), 400

    prompt = (
        "Bạn là biên tập viên tiểu thuyết tiên hiệp/huyền huyễn tiếng Việt. "
        "Hãy kiểm tra ngữ cảnh từng câu dưới đây và CHỈ sửa những chỗ bị lỗi dấu câu (dấu phẩy, chấm câu đặt sai chỗ) "
        "hoặc lỗi chính tả/dấu thanh tiếng Việt hiển nhiên do quá trình convert. "
        "TUYỆT ĐỐI giữ nguyên tên riêng, cảnh giới, chiêu thức Hán Việt, số lượng phần tử trong mảng JSON và không tóm tắt nội dung. "
        "Trả về kết quả dưới dạng một mảng JSON (JSON array of strings) khớp chính xác số lượng đoạn đầu vào:\n\n"
        + json.dumps(chunks, ensure_ascii=False)
    )

    try:
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        }
        resp = requests.post(gemini_url, json=payload, timeout=45)
        resp.raise_for_status()
        res_json = resp.json()
        raw_output = res_json["candidates"][0]["content"]["parts"][0]["text"]
        polished_chunks = json.loads(raw_output)

        if isinstance(polished_chunks, list) and len(polished_chunks) == len(chunks):
            return jsonify({"chunks": polished_chunks})
        return jsonify({"error": "Kết quả AI trả về không khớp định dạng đoạn, giữ nguyên văn bản gốc."}), 500
    except Exception as e:
        return jsonify({"error": f"Lỗi kết nối AI: {e}"}), 500


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
