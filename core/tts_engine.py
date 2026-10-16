# File: core/tts_engine.py
import asyncio
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from typing import List, Callable, Optional, Dict, Tuple
import edge_tts
import pygame


class TTSEngine:
    """
    Bộ máy xử lý Text-to-Speech và Audio Playback đa luồng.
    Hỗ trợ Preload (tải trước 2 chunks tiếp theo) giúp phát âm thanh liên tục không ngắt quãng.
    """

    VOICES = {
        "Nữ - Hoài My (Neural)": "vi-VN-HoaiMyNeural",
        "Nam - Nam Minh (Neural)": "vi-VN-NamMinhNeural",
    }

    def __init__(
        self,
        on_chunk_start: Optional[Callable[[int], None]] = None,
        on_playback_finished: Optional[Callable[[], None]] = None,
        on_status_change: Optional[Callable[[str], None]] = None,
    ):
        # Khởi tạo pygame mixer để phát âm thanh
        pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=2048)

        self.on_chunk_start = on_chunk_start
        self.on_playback_finished = on_playback_finished
        self.on_status_change = on_status_change

        self.chunks: List[str] = []
        self.current_index: int = 0
        self.voice: str = "vi-VN-HoaiMyNeural"
        self.speed: float = 1.0  # Từ 0.8x đến 1.5x

        # Cờ điều khiển luồng phát
        self.is_playing: bool = False
        self.is_paused: bool = False
        self._stop_event = threading.Event()
        self._playback_thread: Optional[threading.Thread] = None

        # Bộ nhớ đệm tạm thời và ThreadPool phục vụ Preload
        self._temp_dir = tempfile.TemporaryDirectory(prefix="novel_tts_")
        self._executor = ThreadPoolExecutor(max_workers=3)
        self._preload_cache: Dict[Tuple[int, str, str], Future] = {}
        self._cache_lock = threading.Lock()

    def set_chunks(self, chunks: List[str]) -> None:
        """Thiết lập danh sách các đoạn văn bản mới cho chương truyện."""
        self.stop()
        self._clear_cache()
        self.chunks = chunks
        self.current_index = 0

    def set_voice(self, voice_code: str) -> None:
        """Đổi giọng đọc (Nam/Nữ) và làm mới bộ đệm preload."""
        if self.voice != voice_code:
            self.voice = voice_code
            self._clear_cache()

    def set_speed(self, speed_multiplier: float) -> None:
        """Cập nhật tốc độ đọc (0.8x - 1.5x) và làm mới bộ đệm preload."""
        rounded = round(float(speed_multiplier), 2)
        if self.speed != rounded:
            self.speed = rounded
            self._clear_cache()

    def _get_rate_string(self) -> str:
        """Chuyển đổi hệ số tốc độ (VD: 1.2x) sang chuỗi định dạng của edge-tts (+20%)."""
        percent = int(round((self.speed - 1.0) * 100))
        return f"+{percent}%" if percent >= 0 else f"{percent}%"

    def _synthesize_to_file(self, index: int, text: str, voice: str, rate_str: str) -> str:
        """Hàm đồng bộ bọc (wrapper) hàm bất đồng bộ của edge-tts để lưu thành file MP3 tạm."""
        safe_rate = rate_str.replace("+", "p").replace("-", "m").replace("%", "")
        file_path = os.path.join(self._temp_dir.name, f"chunk_{index}_{voice}_{safe_rate}.mp3")

        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            return file_path

        async def _run_tts():
            communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate_str)
            await communicate.save(file_path)

        asyncio.run(_run_tts())
        return file_path

    def _schedule_preload(self, index: int) -> Optional[Future]:
        """Đưa một chunk vào hàng đợi tải ngầm nếu chưa có trong bộ nhớ đệm."""
        if index < 0 or index >= len(self.chunks):
            return None

        voice = self.voice
        rate_str = self._get_rate_string()
        cache_key = (index, voice, rate_str)

        with self._cache_lock:
            if cache_key not in self._preload_cache:
                future = self._executor.submit(
                    self._synthesize_to_file,
                    index,
                    self.chunks[index],
                    voice,
                    rate_str,
                )
                self._preload_cache[cache_key] = future
            return self._preload_cache[cache_key]

    def play(self, start_index: Optional[int] = None) -> None:
        """Bắt đầu phát hoặc tiếp tục phát từ trạng thái Pause."""
        if not self.chunks:
            return

        # Nếu đang tạm dừng và không yêu cầu nhảy đoạn mới -> Tiếp tục phát ngay
        if self.is_playing and self.is_paused and start_index is None:
            pygame.mixer.music.unpause()
            self.is_paused = False
            if self.on_status_change:
                self.on_status_change(f"Đang đọc đoạn {self.current_index + 1}/{len(self.chunks)}...")
            return

        # Nếu muốn phát lại từ một đoạn cụ thể
        if start_index is not None:
            self.stop()
            self.current_index = max(0, min(start_index, len(self.chunks) - 1))
        elif self.is_playing:
            return

        if self.current_index >= len(self.chunks):
            self.current_index = 0

        self._stop_event.clear()
        self.is_playing = True
        self.is_paused = False

        self._playback_thread = threading.Thread(target=self._playback_loop, daemon=True)
        self._playback_thread.start()

    def pause(self) -> None:
        """Tạm dừng phát âm thanh."""
        if self.is_playing and not self.is_paused:
            pygame.mixer.music.pause()
            self.is_paused = True
            if self.on_status_change:
                self.on_status_change("Đã tạm dừng.")

    def stop(self) -> None:
        """Dừng hẳn quá trình phát âm thanh và giải phóng file đang mở."""
        self._stop_event.set()
        self.is_playing = False
        self.is_paused = False

        try:
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
        except pygame.error:
            pass

        if self._playback_thread and self._playback_thread.is_alive():
            if threading.current_thread() != self._playback_thread:
                self._playback_thread.join(timeout=1.0)

    def _playback_loop(self) -> None:
        """
        Vòng lặp chạy ngầm (Background Worker Thread):
        - Lấy file audio của chunk hiện tại (i) và phát ngay.
        - Đồng thời kích hoạt preload cho chunk (i+1) và (i+2).
        """
        while self.current_index < len(self.chunks) and not self._stop_event.is_set():
            idx = self.current_index

            # Kích hoạt tải cho chunk hiện tại và preload trước 2 chunks kế tiếp
            current_future = self._schedule_preload(idx)
            self._schedule_preload(idx + 1)
            self._schedule_preload(idx + 2)

            if self.on_status_change:
                self.on_status_change(f"Đang chuẩn bị âm thanh đoạn {idx + 1}/{len(self.chunks)}...")

            try:
                audio_path = current_future.result(timeout=30)
            except Exception as e:
                if self.on_status_change:
                    self.on_status_change(f"Lỗi tạo TTS ở đoạn {idx + 1}: {e}")
                self.current_index += 1
                continue

            if self._stop_event.is_set():
                break

            # Báo cho giao diện (UI) đổi màu Highlight và tự động cuộn tới đoạn đang đọc
            if self.on_chunk_start:
                self.on_chunk_start(idx)

            if self.on_status_change:
                self.on_status_change(f"Đang đọc đoạn {idx + 1}/{len(self.chunks)}...")

            # Phát file MP3 bằng pygame.mixer
            try:
                pygame.mixer.music.load(audio_path)
                pygame.mixer.music.play()

                # Chờ cho tới khi đoạn âm thanh phát xong hoặc có lệnh Stop
                while not self._stop_event.is_set():
                    if self.is_paused:
                        time.sleep(0.1)
                        continue
                    if not pygame.mixer.music.get_busy():
                        break
                    time.sleep(0.05)

                pygame.mixer.music.unload()
            except Exception as e:
                if self.on_status_change:
                    self.on_status_change(f"Lỗi phát âm thanh: {e}")
                break

            if not self._stop_event.is_set():
                self.current_index += 1

        # Kết thúc chương truyện
        if not self._stop_event.is_set() and self.current_index >= len(self.chunks):
            self.is_playing = False
            self.is_paused = False
            self.current_index = 0
            if self.on_playback_finished:
                self.on_playback_finished()

    def _clear_cache(self) -> None:
        """Xóa các tác vụ preload cũ khi đổi chương, đổi giọng hoặc đổi tốc độ."""
        with self._cache_lock:
            for future in self._preload_cache.values():
                future.cancel()
            self._preload_cache.clear()

    def shutdown(self) -> None:
        """Dọn dẹp toàn bộ tài nguyên khi đóng ứng dụng."""
        self.stop()
        self._clear_cache()
        self._executor.shutdown(wait=False)
        try:
            pygame.mixer.quit()
        except Exception:
            pass
        try:
            self._temp_dir.cleanup()
        except Exception:
            pass
