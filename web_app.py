# File: web_app.py
import asyncio
import io
import os
from flask import Flask, render_template, request, jsonify, send_file
import edge_tts

from core.scraper import NovelScraper
from utils.storage import HistoryManager

app = Flask(__name__)


def _speed_to_rate_str(speed: float) -> str:
    """Chuyển hệ số tốc độ (0.5 - 2.0) thành chuỗi phần trăm cho edge-tts."""
    percent = int(round((float(speed) - 1.0) * 100))
    return f"+{percent}%" if percent >= 0 else f"{percent}%"


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
    """API tải chương truyện và lưu vào lịch sử."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "Vui lòng nhập đường dẫn (URL) của chương truyện!"}), 400

    try:
        title, chunks, prev_url, next_url = NovelScraper.fetch_chapter(url)
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
