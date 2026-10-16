# File: utils/storage.py
import json
import os
from datetime import datetime
from typing import List, Dict

HISTORY_FILE = "history.json"
MAX_HISTORY_ITEMS = 30


class HistoryManager:
    """Quản lý lịch sử đọc truyện lưu trữ dưới dạng file JSON."""

    @staticmethod
    def load_history() -> List[Dict[str, str]]:
        """Đọc danh sách lịch sử từ file JSON."""
        if not os.path.exists(HISTORY_FILE):
            return []
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    @staticmethod
    def add_entry(title: str, url: str) -> List[Dict[str, str]]:
        """
        Thêm một chương truyện mới vào đầu lịch sử.
        Nếu URL đã tồn tại, cập nhật lại thời gian và đưa lên đầu danh sách.
        """
        history = HistoryManager.load_history()
        # Loại bỏ bản ghi trùng URL cũ (nếu có)
        history = [item for item in history if item.get("url") != url]

        new_entry = {
            "title": title.strip() or url,
            "url": url.strip(),
            "timestamp": datetime.now().strftime("%d/%m/%Y %H:%M")
        }
        history.insert(0, new_entry)
        history = history[:MAX_HISTORY_ITEMS]

        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=4)
        except OSError as e:
            print(f"[Storage Error] Không thể lưu lịch sử: {e}")

        return history

    @staticmethod
    def clear_history() -> None:
        """Xóa toàn bộ lịch sử đọc."""
        if os.path.exists(HISTORY_FILE):
            try:
                os.remove(HISTORY_FILE)
            except OSError:
                pass
