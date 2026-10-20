# File: utils/storage.py
import json
import os
import time
from datetime import datetime
from typing import List, Dict, Any

HISTORY_FILE = "history.json"
MAX_HISTORY_ITEMS = 30


class HistoryManager:
    """Quản lý và đồng bộ lịch sử đọc truyện 2 chiều (kèm ảnh bìa truyện)."""

    @staticmethod
    def load_history() -> List[Dict[str, Any]]:
        if not os.path.exists(HISTORY_FILE):
            return []
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    @staticmethod
    def save_history(history: List[Dict[str, Any]]) -> None:
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history[:MAX_HISTORY_ITEMS], f, ensure_ascii=False, indent=4)
        except OSError as e:
            print(f"[Storage Error] Không thể lưu lịch sử: {e}")

    @staticmethod
    def add_entry(title: str, url: str, total_chunks: int = 0, cover_url: str = "") -> List[Dict[str, Any]]:
        history = HistoryManager.load_history()
        url = url.strip()

        existing_chunk = 0
        existing_cover = ""
        for item in history:
            if item.get("url") == url:
                existing_chunk = item.get("chunk_index", 0)
                existing_cover = item.get("cover_url", "")
                break

        history = [item for item in history if item.get("url") != url]

        new_entry = {
            "title": title.strip() or url,
            "url": url,
            "cover_url": cover_url.strip() or existing_cover,
            "timestamp": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "updated_at": time.time(),
            "chunk_index": existing_chunk,
            "total_chunks": total_chunks,
        }
        history.insert(0, new_entry)
        history = history[:MAX_HISTORY_ITEMS]
        HistoryManager.save_history(history)
        return history

    @staticmethod
    def update_progress(url: str, chunk_index: int, total_chunks: int = 0) -> List[Dict[str, Any]]:
        history = HistoryManager.load_history()
        url = url.strip()
        for item in history:
            if item.get("url") == url:
                item["chunk_index"] = max(0, int(chunk_index))
                if total_chunks > 0:
                    item["total_chunks"] = int(total_chunks)
                item["updated_at"] = time.time()
                item["timestamp"] = datetime.now().strftime("%d/%m/%Y %H:%M")
                break
        history.sort(key=lambda x: float(x.get("updated_at", 0)), reverse=True)
        HistoryManager.save_history(history)
        return history

    @staticmethod
    def sync_entries(client_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        server_entries = HistoryManager.load_history()
        merged_map: Dict[str, Dict[str, Any]] = {}

        for item in server_entries:
            url = (item.get("url") or "").strip()
            if url:
                merged_map[url] = item

        if isinstance(client_entries, list):
            for c_item in client_entries:
                if not isinstance(c_item, dict):
                    continue
                url = (c_item.get("url") or "").strip()
                if not url:
                    continue

                c_time = float(c_item.get("updated_at") or 0)
                if url not in merged_map:
                    merged_map[url] = {
                        "title": c_item.get("title", url),
                        "url": url,
                        "cover_url": c_item.get("cover_url", ""),
                        "timestamp": c_item.get("timestamp", ""),
                        "updated_at": c_time,
                        "chunk_index": int(c_item.get("chunk_index") or 0),
                        "total_chunks": int(c_item.get("total_chunks") or 0),
                    }
                else:
                    s_time = float(merged_map[url].get("updated_at") or 0)
                    if c_time > s_time:
                        if not c_item.get("cover_url") and merged_map[url].get("cover_url"):
                            c_item["cover_url"] = merged_map[url]["cover_url"]
                        merged_map[url] = c_item

        merged_list = list(merged_map.values())
        merged_list.sort(key=lambda x: float(x.get("updated_at") or 0), reverse=True)
        merged_list = merged_list[:MAX_HISTORY_ITEMS]

        HistoryManager.save_history(merged_list)
        return merged_list

    @staticmethod
    def clear_history() -> None:
        if os.path.exists(HISTORY_FILE):
            try:
                os.remove(HISTORY_FILE)
            except OSError:
                pass
