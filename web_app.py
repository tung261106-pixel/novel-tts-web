# File: web_app.py
import asyncio
import io
from flask import Flask, render_template, request, jsonify, send_file
import edge_tts

from core.scraper import NovelScraper
from utils.storage import HistoryManager

app = Flask(__name__)


def _speed_to_rate_str(speed: float) -> str:
    """Chuyển hệ số tốc độ (0.8 - 1.5) thành chuỗi phần trăm cho edge-tts (VD: +15%)."""
    percent = int(round((float(speed) - 1.0) * 100))
    return f"+{percent}%" if percent >= 0 else f"{percent}%"


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
    """API nhận URL chương truyện, bóc tách văn bản sạch và lưu lịch sử."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "Vui lòng nhập đường dẫn (URL) của chương truyện!"}), 400

    try:
        title, chunks, prev_url, next_url = NovelScraper.fetch_chapter(url)
        history = HistoryManager.add_entry(title, url)
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


@app.route("/api/history", methods=["GET"])
def api_get_history():
    """API lấy danh sách lịch sử đọc truyện."""
    return jsonify(HistoryManager.load_history())


@app.route("/api/history/clear", methods=["POST"])
def api_clear_history():
    """API xóa toàn bộ lịch sử đọc truyện."""
    HistoryManager.clear_history()
    return jsonify({"status": "cleared"})


if __name__ == "__main__":
    print("=========================================================")
    print("🌐 Novel TTS Reader Web đã khởi động!")
    print("👉 Mở trình duyệt và truy cập: http://127.0.0.1:5000")
    print("=========================================================")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
