"""
zapret-manager — GUI для автоматического подбора и мониторинга zapret
Требования: pip install customtkinter requests
"""

import sys
import os
import re
import time
import threading
import subprocess
import ctypes
import requests
from datetime import datetime

# ──────────────────────────────────────────────
# Права администратора (UAC)
# ──────────────────────────────────────────────
def ensure_admin():
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        is_admin = False
    if not is_admin:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, " ".join(f'"{a}"' for a in sys.argv), None, 1
        )
        sys.exit(0)


# ──────────────────────────────────────────────
# Поиск файлов запуска
# ──────────────────────────────────────────────
def get_base_dir():
    """Папка рядом с exe или скриптом."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_winws():
    """Ищет winws.exe в bin/ рядом с exe/скриптом."""
    base = get_base_dir()
    candidate = os.path.join(base, "bin", "winws.exe")
    if os.path.isfile(candidate):
        return candidate
    # Иногда лежит прямо рядом
    candidate2 = os.path.join(base, "winws.exe")
    if os.path.isfile(candidate2):
        return candidate2
    return None


def find_bat_files():
    """Возвращает отсортированный список .bat-файлов в корневой папке."""
    base = get_base_dir()
    try:
        bats = sorted(
            f for f in os.listdir(base)
            if f.lower().endswith(".bat") and os.path.isfile(os.path.join(base, f))
        )
    except Exception:
        bats = []
    return [os.path.join(base, b) for b in bats]


def extract_winws_args(bat_path):
    """
    Вытаскивает аргументы для winws.exe из .bat-файла.
    Поддерживает разные форматы записи:
      - %~dp0bin\winws.exe ...
      - bin\winws.exe ...
      - winws.exe ...
      - start "" "...winws.exe" ...
      - "...\winws.exe" ...
    Также разворачивает переменные окружения %VAR%.
    """
    for enc in ("utf-8", "cp1251", "cp866", "latin-1"):
        try:
            with open(bat_path, "r", encoding=enc, errors="strict") as f:
                content = f.read()
            break
        except Exception:
            content = None
    if content is None:
        return None

    # Убираем rem-комментарии и пустые строки
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("rem") or stripped.startswith("::"):
            continue
        lines.append(stripped)

    for line in lines:
        # Паттерн: что угодно + winws.exe + пробел + аргументы
        # Поддерживаем: %~dp0bin\winws.exe, bin\winws.exe, "c:\...\winws.exe" и т.д.
        m = re.search(
            r'(?:"[^"]*winws\.exe"|[^\s"]*winws\.exe)\s+(.*)',
            line,
            re.IGNORECASE,
        )
        if m:
            args = m.group(1).strip()
            # Убираем ^, & pause, >> и другой мусор в конце строки
            args = re.sub(r'\s*[\^&>|].*$', '', args).strip()
            # Раскрываем %~dp0 → папка батника
            bat_dir = os.path.dirname(bat_path)
            args = args.replace("%~dp0", bat_dir + os.sep)
            args = args.replace("%~DP0", bat_dir + os.sep)
            # Раскрываем стандартные переменные окружения
            args = os.path.expandvars(args)
            if args:
                return args

    return None


def debug_bat_content(bat_path):
    """Возвращает первые 20 строк батника для отладки."""
    for enc in ("utf-8", "cp1251", "cp866", "latin-1"):
        try:
            with open(bat_path, "r", encoding=enc, errors="strict") as f:
                lines = f.readlines()
            return "".join(lines[:20])
        except Exception:
            pass
    return "<не удалось прочитать>"


# ──────────────────────────────────────────────
# Сетевые проверки
# ──────────────────────────────────────────────
TEST_URLS = {
    "YouTube": "https://www.youtube.com",
    "Discord": "https://discord.com",
}
TIMEOUT = 8


def measure_latency(url: str):
    """Возвращает задержку в мс или None при ошибке."""
    try:
        t0 = time.perf_counter()
        r = requests.get(
            url, timeout=TIMEOUT, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
            verify=False,  # на случай проблем с сертификатами через DPI
        )
        elapsed = (time.perf_counter() - t0) * 1000
        if r.status_code < 500:
            return round(elapsed, 1)
    except Exception:
        pass
    return None


def check_both() -> dict:
    """Проверяет обе цели, возвращает {name: ms | None}."""
    results = {}

    def probe(name, url):
        results[name] = measure_latency(url)

    threads = [
        threading.Thread(target=probe, args=(name, url), daemon=True)
        for name, url in TEST_URLS.items()
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ──────────────────────────────────────────────
# Менеджер процесса winws / bat
# ──────────────────────────────────────────────
class WinwsManager:
    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()

    def start_with_args(self, winws_path: str, args: str) -> bool:
        """Запускает winws.exe напрямую с аргументами."""
        self.stop()
        cmd = f'"{winws_path}" {args}'
        try:
            with self._lock:
                self._proc = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    cwd=os.path.dirname(winws_path),  # CWD = папка winws
                )
            time.sleep(0.3)
            # Проверяем что процесс живой
            if self._proc.poll() is not None:
                self._proc = None
                return False
            return True
        except Exception:
            return False

    def start_bat(self, bat_path: str) -> bool:
        """
        Запускает bat-файл напрямую (fallback когда не смогли извлечь аргументы).
        Bat запускается из своей папки — именно так оно и должно работать.
        """
        self.stop()
        bat_dir = os.path.dirname(bat_path)
        bat_name = os.path.basename(bat_path)
        try:
            with self._lock:
                self._proc = subprocess.Popen(
                    ["cmd.exe", "/c", bat_name],
                    cwd=bat_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            time.sleep(0.5)
            # bat мог запустить дочерний winws и завершиться сам — это нормально
            return True
        except Exception:
            return False

    def stop(self):
        with self._lock:
            if self._proc:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=3)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None
        # Принудительно гасим все winws.exe
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "winws.exe"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
        time.sleep(0.3)

    def is_running(self) -> bool:
        """
        Считаем 'работающим' если либо наш процесс жив,
        либо winws.exe есть в списке процессов (bat мог запустить его отдельно).
        """
        with self._lock:
            proc_alive = self._proc is not None and self._proc.poll() is None

        if proc_alive:
            return True

        # Проверяем наличие winws.exe в системе
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq winws.exe", "/NH"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stderr=subprocess.DEVNULL,
            ).decode("cp866", errors="ignore")
            return "winws.exe" in out
        except Exception:
            return False


# ──────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────
try:
    import customtkinter as ctk
except ImportError:
    import subprocess as _sp
    _sp.run([sys.executable, "-m", "pip", "install", "customtkinter", "--quiet"])
    import customtkinter as ctk

ACCENT      = "#5B6CF9"
ACCENT_DARK = "#4756E8"
SUCCESS     = "#22C55E"
DANGER      = "#EF4444"
WARN        = "#F59E0B"
BG          = "#0F1117"
PANEL       = "#181C24"
CARD        = "#1E2330"
TEXT        = "#E2E8F0"
MUTED       = "#64748B"
BORDER      = "#2A3040"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title(".")
        self.geometry("900x600")
        self.minsize(800, 520)
        self.configure(fg_color=BG)

        self.winws_mgr = WinwsManager()
        self.winws_path = find_winws()
        self.bat_files = find_bat_files()
        self._stop_event = threading.Event()
        self._auto_thread = None
        self._ping_thread = None
        self._active_bat = None
        self._testing = False

        self._build_ui()
        self._log_startup()
        self._start_ping_loop()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ──────────────────────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=0)
        self.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        left.grid(row=0, column=0, sticky="nsew", padx=(16, 8), pady=16)
        left.grid_rowconfigure(2, weight=1)
        left.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(left, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 0))

        self._status_badge = ctk.CTkLabel(
            hdr, text="● Не запущен",
            font=ctk.CTkFont(size=12),
            text_color=MUTED,
        )
        self._status_badge.pack(side="right", padx=4)

        btn_frame = ctk.CTkFrame(left, fg_color="transparent")
        btn_frame.grid(row=1, column=0, sticky="ew", padx=20, pady=16)
        btn_frame.grid_columnconfigure(0, weight=1)

        self._quick_btn = ctk.CTkButton(
            btn_frame, text="⚡  Быстрое подключение",
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color="#16A34A", hover_color="#15803D",
            height=48, corner_radius=12,
            command=self._on_quick_connect,
        )
        self._quick_btn.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        self._start_btn = ctk.CTkButton(
            btn_frame, text="▶  Подобрать обход",
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_DARK,
            height=48, corner_radius=12,
            command=self._on_start,
        )
        self._start_btn.grid(row=1, column=0, sticky="ew", padx=(0, 8))

        self._stop_btn = ctk.CTkButton(
            btn_frame, text="■  Стоп",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=CARD, hover_color=BORDER,
            border_color=BORDER, border_width=1,
            text_color=MUTED,
            height=48, corner_radius=12,
            command=self._on_stop, state="disabled",
        )
        self._stop_btn.grid(row=1, column=1)

        self._log = ctk.CTkTextbox(
            left,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=CARD,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
            corner_radius=10,
            wrap="word",
            state="disabled",
        )
        self._log.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 20))

        right = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, width=220)
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 16), pady=16)
        right.grid_propagate(False)
        right.grid_rowconfigure(10, weight=1)

        ctk.CTkLabel(
            right, text="Мониторинг",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=TEXT,
        ).pack(pady=(22, 4), padx=16, anchor="w")

        ctk.CTkLabel(
            right, text="Задержка HTTPS",
            font=ctk.CTkFont(size=11),
            text_color=MUTED,
        ).pack(padx=16, anchor="w")

        ctk.CTkFrame(right, height=1, fg_color=BORDER).pack(fill="x", padx=16, pady=12)

        self._ping_cards = {}
        icons = {"YouTube": "▶", "Discord": "🎮"}
        for name in ("YouTube", "Discord"):
            card = ctk.CTkFrame(right, fg_color=CARD, corner_radius=10)
            card.pack(fill="x", padx=14, pady=6)

            top_row = ctk.CTkFrame(card, fg_color="transparent")
            top_row.pack(fill="x", padx=12, pady=(10, 2))

            ctk.CTkLabel(
                top_row,
                text=f"{icons[name]}  {name}",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=TEXT,
            ).pack(side="left")

            val_lbl = ctk.CTkLabel(
                card, text="—",
                font=ctk.CTkFont(family="Consolas", size=20, weight="bold"),
                text_color=MUTED,
            )
            val_lbl.pack(padx=12, pady=(0, 10), anchor="w")
            self._ping_cards[name] = val_lbl

        ctk.CTkFrame(right, height=1, fg_color=BORDER).pack(fill="x", padx=16, pady=12)

        self._bat_label = ctk.CTkLabel(
            right,
            text="Конфиг: не выбран",
            font=ctk.CTkFont(size=10),
            text_color=MUTED,
            wraplength=190,
            justify="left",
        )
        self._bat_label.pack(padx=16, anchor="w")

        self._last_update = ctk.CTkLabel(
            right, text="",
            font=ctk.CTkFont(size=10),
            text_color=MUTED,
        )
        self._last_update.pack(padx=16, pady=(6, 0), anchor="w")

    # ── Logging ─────────────────────────────────
    def _log_line(self, msg: str, color: str = TEXT):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}]  {msg}\n"
        self._log.configure(state="normal")
        self._log.insert("end", line)
        self._log.see("end")
        self._log.configure(state="disabled")

    def _log_startup(self):
        base = get_base_dir()
        self._log_line(f"Рабочая папка: {base}")

        if self.winws_path:
            self._log_line(f"✔ winws.exe: {self.winws_path}")
        else:
            self._log_line("⚠  winws.exe НЕ найден (ищу в bin/ и рядом с exe)!", WARN)

        if self.bat_files:
            self._log_line(f"✔ Найдено .bat-конфигов: {len(self.bat_files)}")
        else:
            self._log_line("⚠  .bat-файлы не найдены в корневой папке!", WARN)

        # Сразу диагностируем несколько первых батников
        parsed_ok = 0
        bat_fallback = 0
        for bat in self.bat_files[:5]:
            args = extract_winws_args(bat)
            name = os.path.basename(bat)
            if args:
                parsed_ok += 1
                self._log_line(f"   ✔ {name} — аргументы извлечены")
            else:
                bat_fallback += 1
                self._log_line(f"   ⚠  {name} — аргументы не распознаны, будет использован прямой запуск bat", WARN)

        if bat_fallback > 0 and parsed_ok == 0:
            self._log_line(
                "ℹ  Все батники будут запускаться напрямую (прямой запуск bat работает так же, как ручной запуск)",
                WARN,
            )

    # ── Button handlers ──────────────────────────
    def _on_quick_connect(self):
        """Быстрое подключение — перебирает батники и подключается к первому,
        где работают и YouTube, и Discord. Без сортировки по пингу."""
        if not self.bat_files:
            self._log_line("✗ Нет .bat-конфигов для подключения.", DANGER)
            return

        self._quick_btn.configure(state="disabled")
        self._start_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal", text_color=DANGER)
        self._set_status("⏳ Быстрое подключение...", WARN)
        self._stop_event.clear()

        self._auto_thread = threading.Thread(target=self._quick_connect_thread, daemon=True)
        self._auto_thread.start()

    def _quick_connect_thread(self):
        self._log_line("⚡ Быстрое подключение — ищем первый рабочий конфиг...")
        self._testing = True
        found = False

        try:
            for bat in self.bat_files:
                if self._stop_event.is_set():
                    break

                name = os.path.basename(bat)
                self._log_line(f"→ Пробуем: {name}")

                ok, method = self._try_start_bat(bat, name)
                if not ok:
                    self._log_line(f"  ✗ Не удалось запустить {name}", DANGER)
                    continue

                wait_sec = 3 if method == "bat" else 2
                self._log_line(f"  Ждём {wait_sec} с...")
                for _ in range(wait_sec * 2):
                    if self._stop_event.is_set():
                        break
                    time.sleep(0.5)

                if self._stop_event.is_set():
                    self.winws_mgr.stop()
                    break

                self._log_line("  Проверяем YouTube и Discord...")
                results = check_both()

                yt_ok = results.get("YouTube") is not None
                dc_ok = results.get("Discord") is not None

                if yt_ok and dc_ok:
                    self._log_line(
                        f"✔ Подключено: {name}  "
                        f"(YT {results['YouTube']} мс / DC {results['Discord']} мс)",
                        SUCCESS,
                    )
                    self._active_bat = name
                    self.after(0, lambda n=name: self._bat_label.configure(text=f"Конфиг: {n}"))
                    self._set_status("● Подключено", SUCCESS)
                    self._update_ping_ui(results)
                    found = True
                    break
                else:
                    self.winws_mgr.stop()
                    time.sleep(0.5)
                    yt_s = f"{results['YouTube']} мс" if yt_ok else "✗"
                    dc_s = f"{results['Discord']} мс" if dc_ok else "✗"
                    self._log_line(f"  ✗ YT: {yt_s} | DC: {dc_s} — следующий...", MUTED)
        finally:
            self._testing = False

        if self._stop_event.is_set():
            return

        if not found:
            self._log_line(
                "✗ Ни один конфиг не дал результата для обоих сервисов.\n"
                "  Попробуйте 'Подобрать обход' для полного перебора.",
                DANGER,
            )
            self._set_status("● Не подключено", DANGER)
            self.winws_mgr.stop()
            self.after(0, lambda: (
                self._quick_btn.configure(state="normal"),
                self._start_btn.configure(state="normal"),
                self._stop_btn.configure(state="disabled", text_color=MUTED),
            ))
        else:
            self.after(0, lambda: (
                self._quick_btn.configure(state="normal"),
                self._start_btn.configure(state="normal"),
            ))

    def _on_start(self):
        if not self.bat_files:
            self._log_line("✗ Нет .bat-конфигов для перебора.", DANGER)
            return

        self._quick_btn.configure(state="disabled")
        self._start_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal", text_color=DANGER)
        self._set_status("⏳ Подбираем...", WARN)
        self._stop_event.clear()

        self._auto_thread = threading.Thread(target=self._auto_select, daemon=True)
        self._auto_thread.start()

    def _on_stop(self):
        self._stop_event.set()
        self.winws_mgr.stop()
        self._active_bat = None
        self._set_status("● Не запущен", MUTED)
        self._bat_label.configure(text="Конфиг: не выбран")
        self._quick_btn.configure(state="normal")
        self._start_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled", text_color=MUTED)
        self._reset_pings()
        self._log_line("■ Остановлено пользователем.")

    def _on_close(self):
        self._stop_event.set()
        self.winws_mgr.stop()
        self.destroy()

    # ── Auto-selection logic ─────────────────────
    def _try_start_bat(self, bat_path: str, log_name: str):
        """
        Пробует запустить конфиг: сначала через извлечённые аргументы,
        если не удалось — запускает bat напрямую (как ручной запуск).
        Возвращает (success: bool, method: str).
        """
        args = extract_winws_args(bat_path)

        if args and self.winws_path:
            ok = self.winws_mgr.start_with_args(self.winws_path, args)
            if ok:
                return True, "args"
            self._log_line(f"  → Прямой запуск с аргументами не удался, пробуем bat...", MUTED)

        # Fallback: запускаем bat напрямую (так же как двойной клик)
        ok = self.winws_mgr.start_bat(bat_path)
        if ok:
            return True, "bat"
        return False, "failed"

    def _auto_select(self):
        self._log_line("▶ Начинаем перебор конфигураций (ищем самый быстрый)...")
        self._testing = True
        candidates = []

        try:
            for bat in self.bat_files:
                if self._stop_event.is_set():
                    break

                name = os.path.basename(bat)
                self._log_line(f"→ Пробуем: {name}")

                ok, method = self._try_start_bat(bat, name)
                if not ok:
                    self._log_line(f"  ✗ Не удалось запустить {name}", DANGER)
                    continue

                wait_sec = 3 if method == "bat" else 2
                self._log_line(f"  Ждём {wait_sec} с... (метод: {method})")
                for _ in range(wait_sec * 2):
                    if self._stop_event.is_set():
                        break
                    time.sleep(0.5)

                if self._stop_event.is_set():
                    self.winws_mgr.stop()
                    break

                self._log_line("  Проверяем YouTube и Discord...")
                results = check_both()
                self.winws_mgr.stop()
                time.sleep(0.5)

                yt_ok = results.get("YouTube") is not None
                dc_ok = results.get("Discord") is not None

                if yt_ok and dc_ok:
                    avg_ms = (results["YouTube"] + results["Discord"]) / 2
                    self._log_line(
                        f"✔ Работает: {name}  "
                        f"(YT {results['YouTube']} мс / DC {results['Discord']} мс, средн. {avg_ms:.1f} мс)"
                    )
                    candidates.append((avg_ms, name, bat, method, results))
                elif yt_ok or dc_ok:
                    # Частично работает — тоже добавляем с большим штрафом
                    yt_ms = results.get("YouTube") or 9999
                    dc_ms = results.get("Discord") or 9999
                    avg_ms = (yt_ms + dc_ms) / 2
                    status_yt = f"{results['YouTube']} мс" if yt_ok else "недоступен"
                    status_dc = f"{results['Discord']} мс" if dc_ok else "недоступен"
                    self._log_line(
                        f"~ Частично: {name} — YT: {status_yt} | Discord: {status_dc}", WARN
                    )
                    candidates.append((avg_ms, name, bat, method, results))
                else:
                    self._log_line(
                        f"  ✗ YouTube: недоступен | Discord: недоступен — пробуем следующий", MUTED
                    )
        finally:
            self._testing = False

        if self._stop_event.is_set():
            return

        if not candidates:
            self._log_line(
                "✗ Ни один конфиг не дал результата.\n"
                "  Проверьте: 1) запускаете от администратора? 2) сам zapret работает при ручном запуске?\n"
                "  3) Попробуйте отключить антивирус на время теста.",
                DANGER,
            )
            self._set_status("● Не подключено", DANGER)
            self.after(0, lambda: (
                self._quick_btn.configure(state="normal"),
                self._start_btn.configure(state="normal"),
                self._stop_btn.configure(state="disabled", text_color=MUTED),
            ))
            return

        candidates.sort(key=lambda c: c[0])
        best_avg, best_name, best_bat, best_method, best_results = candidates[0]

        if len(candidates) > 1:
            self._log_line(f"Найдено рабочих конфигов: {len(candidates)}. Сортировка по пингу:")
            for avg_ms, name, *_ in candidates:
                self._log_line(f"   • {name} — средн. {avg_ms:.1f} мс")

        self._log_line(
            f"🏆 Выбран: {best_name} (средн. {best_avg:.1f} мс)", SUCCESS
        )

        # Запускаем лучший конфиг финально
        ok, method = self._try_start_bat(best_bat, best_name)
        if not ok:
            self._log_line(f"✗ Не удалось запустить лучший конфиг {best_name}.", DANGER)
            self._set_status("● Не подключено", DANGER)
            self.after(0, lambda: (
                self._quick_btn.configure(state="normal"),
                self._start_btn.configure(state="normal"),
                self._stop_btn.configure(state="disabled", text_color=MUTED),
            ))
            return

        self._active_bat = best_name
        self.after(0, lambda: self._bat_label.configure(text=f"Конфиг: {best_name}"))
        self._set_status("● Подключено", SUCCESS)
        self._update_ping_ui(best_results)

    # ── Ping monitoring loop ─────────────────────
    def _start_ping_loop(self):
        self._ping_thread = threading.Thread(target=self._ping_loop, daemon=True)
        self._ping_thread.start()

    def _ping_loop(self):
        while True:
            time.sleep(5)
            if self._testing:
                continue
            if self.winws_mgr.is_running():
                results = check_both()
                self._update_ping_ui(results)
                ts = datetime.now().strftime("%H:%M:%S")
                self.after(0, lambda t=ts: self._last_update.configure(text=f"Обновлено: {t}"))

    def _update_ping_ui(self, results: dict):
        def _do():
            for name, ms in results.items():
                lbl = self._ping_cards.get(name)
                if not lbl:
                    continue
                if ms is not None:
                    lbl.configure(
                        text=f"{ms} мс",
                        text_color=SUCCESS if ms < 400 else WARN,
                    )
                else:
                    lbl.configure(text="Недоступен", text_color=DANGER)
        self.after(0, _do)

    def _reset_pings(self):
        def _do():
            for lbl in self._ping_cards.values():
                lbl.configure(text="—", text_color=MUTED)
        self.after(0, _do)

    def _set_status(self, text: str, color: str):
        self.after(0, lambda: self._status_badge.configure(text=text, text_color=color))


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    if sys.platform == "win32":
        ensure_admin()

    # Подавляем предупреждения requests об SSL
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

    app = App()
    app.mainloop()
