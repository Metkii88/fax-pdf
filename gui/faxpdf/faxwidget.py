#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
faxpdf/faxwidget.py — «Факс в виде факсимильного аппарата».

Полноценный виджет FaxMachineWidget: аппарат нарисован QPainter'ом
(корпус, крышка ADF со щелью подачи, панель управления, дисплей-экран,
клавиши цифровой клавиатуры, трубка с витым шнуром, кнопки FWD/STOP/HOLD,
индикаторы). Вся панель управления рабочая:

  * ЭКРАН (LCD): показывает состояние — READY / DIALING / RX pages /
    TX % / BUSY / ERROR; печатает «термо-ленту» событий.
  * Клавиатура 0-9 * # + кнопка REDIAL: набор номера, Backspace/Clear.
  * TxD (отправка) : выбор PDF → конвертация → sendfax; прогресс по строкам
    лога HylaFAX («Doc 1 of N, Page 1..k»); отмена STOP.
  * RxD (приём)   : live-мониторинг recvq/archivedir; при звонке экран
    «RING…», приём страницы -> «RX p/N», готовый документ попадает
    в лоток выдачи (Output tray) под крышкой — клик открывает PDF.
  * HOLD         : пауза/возобновление мониторинга и передачи.
  * INDICATOR    : мигает во время RX/TX, зелёный в простое.
  * Динамик      : гудки/пики через QSoundEffect (если доступны), иначе без звука.

Виджет самодостаточен: FaxMachineWidget(parent, spool=...) добавляется в любой
QMainWindow/диалог. Запуск демо-аппарата:  python3 -m faxpdf.faxwidget
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from PyQt5.QtCore import (Qt, QTimer, QRect, QRectF, QPointF, QSize, pyqtSignal,
                          pyqtProperty, QPropertyAnimation, QEasingCurve)
from PyQt5.QtGui import (QColor, QFont, QFontDatabase, QIcon, QImage, QImageReader,
                         QPainter, QPainterPath, QPen, QBrush, QLinearGradient,
                         QRadialGradient, QPixmap, QDesktopServices)
from PyQt5.QtWidgets import (QWidget, QApplication, QMainWindow, QVBoxLayout, QWidget,
                           QHBoxLayout, QLabel, QPushButton, QFileDialog,
                           QMessageBox, QSizePolicy, QToolTip)
from PyQt5.QtCore import QUrl

try:  # пакетный импорт или запуск из исходников
    from faxpdf import hylafaxd as hx
    from faxpdf import settings as cfgmod
except ImportError:  # pragma: no cover
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import hylafaxd as hx
    import settings as cfgmod


# ------------------------------------------------------------------ model ---

MODE_IDLE = "IDLE"
MODE_RING = "RING"
MODE_RX = "RX"
MODE_TX = "TX"
MODE_BUSY = "BUSY"
MODE_ERROR = "ERROR"
MODE_HOLD = "HOLD"
MODE_VOICE = "VOICE"      # голосовой вызов на линии / трубка поднята


@dataclass
class FaxDoc:
    """Документ в лотке выдачи (принятый или отправленный)."""
    path: Path
    pages: int
    number: str            # кому/от кого
    kind: str              # "in" | "out"
    ts: float = field(default_factory=time.time)
    thumbnail: Optional[QPixmap] = None


class FaxBackend:
    """Тонкая обёртка над HylaFAX; легко подменяется мок-объектом (демо)."""

    def __init__(self, spool: str = "/var/spool/hylafax"):
        self.spool = Path(os.environ.get("FAX_SPOOL", spool))

    def watch_dirs(self) -> List[Path]:
        dirs = [self.spool / "recvq", self.spool / "archive" / "received"]
        return [d for d in dirs if d.is_dir()] or []

    def queue_jobs(self) -> List[str]:
        try:
            out = subprocess.run(["faxstat", "-s"], capture_output=True,
                                 text=True, timeout=10).stdout
            return [l for l in out.splitlines() if l.strip()]
        except Exception:
            return []

    def send(self, pdf: Path, number: str) -> Optional[int]:
        """Отправка через sendfax.sh (PDF->TIFF + sendfax). Возвращает JobID."""
        script = self.spool / ".." / ".." / "opt" / "fax-pdf" / "sendfax.sh"
        cand = [Path("/opt/fax-pdf/sendfax.sh"), Path("sendfax.sh"), script]
        for c in cand:
            if c.is_file():
                r = subprocess.run([str(c), str(pdf), number],
                                   capture_output=True, text=True, timeout=600)
                m = re.search(r"(?:JobID|job)\s*[:=]\s*(\d+)", r.stdout, re.I)
                if m:
                    return int(m.group(1))
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or r.stdout)[-400:])
                return None
        raise RuntimeError("sendfax.sh не найден")

    def abort(self, jobid: Optional[int]) -> None:
        if jobid:
            subprocess.run(["sendfax", "-D", str(jobid)], capture_output=True)


# ------------------------------------------------------------- the machine ---

class FaxMachineWidget(QWidget):
    """Факсимильный аппарат: графика + рабочая панель управления."""

    docReceived = pyqtSignal(object)     # FaxDoc
    docSent = pyqtSignal(object)         # FaxDoc
    error = pyqtSignal(str)
    voiceCall = pyqtSignal(object)       # dict события голосового вызова

    MIN_W, MIN_H = 420, 300

    def __init__(self, parent=None, backend: Optional[FaxBackend] = None,
                 demo: bool = False):
        super().__init__(parent)
        self.backend = backend or FaxBackend()
        self.demo = demo
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

        # ---- состояние
        self.mode = MODE_IDLE
        self.digits = ""                 # набираемый номер
        self.rx_pages = 0
        self.tx_progress = 0             # 0..100
        self.indicator_on = True
        self.held = False
        self.tray_docs: List[FaxDoc] = []
        self.seen_files = set()
        self._active_job: Optional[int] = None
        self._pending_pdf: Optional[Path] = None
        self.lcd_log: List[str] = []     # термо-лента событий на экране
        self.incoming_sel: List[Path] = []   # выбранная пачка для TX (мн. файлов)
        self._tx_queue_idx = 0
        # --- голос
        self.voice_enabled = cfgmod.get("voice_answer")
        self.voice_rings = cfgmod.get("voice_rings")
        self.offhook = False             # «трубка поднята»
        self._ring_count = 0
        self._seen_voice_ts = set()

        # анимация мигания индикатора
        self._blink = QTimer(self, interval=450, timeout=self._toggle_blink)
        # мониторинг входящих
        self._poll = QTimer(self, interval=1500, timeout=self._check_incoming)
        self._poll.start()
        # «звонок» — периодический случайный вход (только демо)
        self._ringgen = QTimer(self, interval=25000, timeout=self._demo_ring)
        if demo:
            self._ringgen.start()
        # автосброс режима RX в IDLE после приёма
        self._rx_reset = QTimer(self, singleShot=True,
                                timeout=lambda: self._set_mode(MODE_IDLE)
                                if self.mode == MODE_RX else None)
        # опрос голосовых вызовов (очередь voice-monitor / syslog)
        self._voice_poll = QTimer(self, interval=3000, timeout=self._check_voice)
        self._voice_poll.start()

        # шрифты
        self.f_lcd = QFont("Monospace", 11)
        self.f_lcd.setStyleHint(QFont.TypeWriter)
        self.f_btn = QFont("Sans Serif", 7, QFont.Bold)
        self.f_lbl = QFont("Sans Serif", 6)

        self._hover_key: Optional[str] = None
        self._pressed_key: Optional[str] = None

    # ---------------- public API ----------------

    def add_tray_doc(self, doc: FaxDoc) -> None:
        self.tray_docs.append(doc)
        self.update()

    def open_tray_doc(self, idx: int) -> None:
        if 0 <= idx < len(self.tray_docs):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.tray_docs[idx].path)))

    # ---------------- события панели ----------------

    def keyPressEvent(self, e):  # физическая клавиатура тоже набирает
        ch = e.text()
        if ch.isdigit() or ch in "*#+":
            self._press_key(ch)
        elif e.key() == Qt.Key_Backspace:
            self._press_key("<")
        elif e.key() == Qt.Key_Return:
            self._press_key("TxD")
        elif e.key() == Qt.Key_Escape:
            self._press_key("STOP")

    # ---------------- on-screen keypad geometry ----------------

    KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#", "<", "REDIAL", "+"]

    def _key_rects(self, panel: QRect) -> dict:
        """Раскладка кнопок внутри панели управления."""
        r = {}
        # цифровая клавиатура 3x4 + доп. ряд справа снизу панели
        kw, kh = 34, 24
        gx, gy = 6, 6
        x0 = panel.left() + 12
        y0 = panel.top() + 34
        order = [["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"], ["*", "0", "#"]]
        for rowi, row in enumerate(order):
            for coli, k in enumerate(row):
                r[k] = QRect(x0 + coli * (kw + gx), y0 + rowi * (kh + gy), kw, kh)
        extra_y = y0 + 4 * (kh + gy) + 4
        r["<"] = QRect(x0, extra_y, kw, kh)
        r["+"] = QRect(x0 + kw + gx, extra_y, kw, kh)
        r["REDIAL"] = QRect(x0 + 2 * (kw + gx), extra_y, 2 * kw + gx, kh)
        # нижний ряд панели: выбор папки сохранения + голосовая «трубка»
        by = panel.bottom() - 34
        r["SAVE"] = QRect(x0, by, 2 * kw + gx, 26)
        r["VOICE"] = QRect(x0 + 2 * (kw + gx) + 8, by, 2 * kw + gx, 26)
        # функциональные кнопки справа от клавиатуры
        fx = panel.right() - 96
        fy = panel.top() + 12
        fw, fh = 88, 26
        r["TxD"] = QRect(fx, fy, fw, fh)
        r["RxD"] = QRect(fx, fy + fh + 6, fw, fh)
        r["STOP"] = QRect(fx, fy + 2 * (fh + 6), fw, fh)
        r["HOLD"] = QRect(fx, fy + 3 * (fh + 6), fw, fh)
        return r

    def _lcd_rect(self, panel: QRect) -> QRect:
        w = min(230, max(150, panel.width() // 3))
        return QRect(panel.center().x() - w // 2 + 40, panel.top() + 12, w, 84)

    def _tray_slot(self) -> QRect:
        """Щель выдачи документов под передней кромкой крышки."""
        b = self.rect()
        top_band = int(b.height() * 0.30)
        return QRect(int(b.width() * 0.16), top_band - 6,
                     int(b.width() * 0.55), 14)

    # ---------------- mouse handling ----------------

    def mouseMoveEvent(self, e):
        pos = e.pos()
        panel = self._panel_rect()
        hk = None
        for k, rect in self._key_rects(panel).items():
            if rect.contains(pos):
                hk = k
                break
        if hk != self._hover_key:
            self._hover_key = hk
            self.update()

    def mouseReleaseEvent(self, e):
        if self._pressed_key:
            k, self._pressed_key = self._pressed_key, None
            self.update()
            self._press_key(k)

    def mousePressEvent(self, e):
        pos = e.pos()
        # клик по лотку выдачи — открыть последний документ
        if self._tray_slot().adjusted(-6, -18, 6, 6).contains(pos) and self.tray_docs:
            self.open_tray_doc(len(self.tray_docs) - 1)
            return
        panel = self._panel_rect()
        for k, rect in self._key_rects(panel).items():
            if rect.contains(pos):
                self._pressed_key = k
                self.update()
                return

    def _press_key(self, k: str) -> None:
        if k == "<":
            self.digits = self.digits[:-1]
        elif k == "REDIAL":
            if self._last_number:
                self.digits = self._last_number
        elif k == "TxD":
            self._start_send()
            return
        elif k == "RxD":
            self._open_inbox_folder()
            return
        elif k == "STOP":
            self._stop_current()
            return
        elif k == "HOLD":
            self._toggle_hold()
            return
        elif k == "SAVE":
            self._choose_save_dir()
            return
        elif k == "VOICE":
            self._toggle_offhook()
            return
        else:
            if len(self.digits) < 24:
                self.digits += k
        self._render_lcd()
        self.update()

    # ---------------- actions ----------------

    _last_number: Optional[str] = None

    def _choose_save_dir(self):
        """Выбор папки сохранения принятых факсов (по умолчанию /home/user)."""
        cur = cfgmod.get("incoming_dir") or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Куда сохранять принятые факсы", cur)
        if d:
            cfgmod.set_incoming_dir(d)
            self._log_lcd("SAVE " + Path(d).name.upper()[:10])
            self._save_settings_to_spool()
            self.update()

    def _save_settings_to_spool(self):
        """Синхронизировать выбор пользователя с серверной конфигурацией.

        1) ~/.config/fax-pdf/config.json — для GUI;
        2) $SPOOL/etc/recv-archive.conf — путь архивации для recv-monitor.py;
        3) RingsVoiceAnswer в etc/config.tty* — ответ на голосовые вызовы.
        Файлы в spool могут требовать прав группы fax — ошибки не фатальны."""
        try:
            hx.SPOOL.joinpath("etc").mkdir(parents=True, exist_ok=True)
            p = hx.SPOOL / "etc" / "recv-archive.conf"
            p.write_text(f"# задано GUI Fax-PDF\nArchiveDir: {cfgmod.get('incoming_dir')}\n",
                         encoding="utf-8")
        except OSError:
            pass
        try:
            hx.apply_voice_settings(self.voice_enabled, self.voice_rings)
        except Exception:
            pass

    def _toggle_offhook(self):
        """«Снять/положить трубку» — приём голосового вызова."""
        self.offhook = not self.offhook
        if self.offhook:
            self._set_mode(MODE_VOICE)
            self._log_lcd("OFF-HOOK ГОЛОС")
            self._beep((500, 150))
        else:
            self._log_lcd("ON-HOOK")
            if self.mode == MODE_VOICE:
                self._set_mode(MODE_IDLE)
        self.update()

    def _start_send(self):
        if self.mode in (MODE_TX, MODE_RX):
            self._beep_err()
            return
        num = self.digits.strip("+")
        if not re.fullmatch(r"[0-9*#+]{5,24}", num):
            self._show_error("НЕТ НОМЕРА", "Наберите номер на клавиатуре аппарата.")
            return
        pdfs, _ = QFileDialog.getOpenFileNames(
            self, "Документы для отправки (можно выбрать несколько)",
            cfgmod.get("incoming_dir") or str(Path.home()),
            "Документы PDF/TIFF (*.pdf *.tif *.tiff)")
        if not pdfs:
            return
        self.incoming_sel = [Path(x) for x in pdfs]
        self._tx_queue_idx = 0
        self._last_number = num
        self.digits = ""
        self._set_mode(MODE_TX)
        self._log_lcd(f"DIALL {num}")
        self._blink.start()
        QTimer.singleShot(1200, lambda: self._do_send(num, self.incoming_sel[0]))

    def _send_next_in_batch(self):
        self._tx_queue_idx += 1
        if self._tx_queue_idx < len(self.incoming_sel):
            pdf = self.incoming_sel[self._tx_queue_idx]
            n = len(self.incoming_sel)
            i = self._tx_queue_idx + 1
            self._log_lcd(f"TX {i}/{n} {pdf.name[:12]}")
            QTimer.singleShot(800, lambda: self._do_send(self._last_number or "", pdf))
        else:
            self.incoming_sel = []
            self._tx_queue_idx = 0
            self._finish_tx_ok(self._last_number or "")

    def _do_send(self, num: str, pdf: Path):
        if self.demo:
            self._sim_tx(pdf, num)
            return
        try:
            self._active_job = self.backend.send(pdf, num)
            doc = FaxDoc(pdf, self._pdf_pages(pdf), num, "out")
            self.add_tray_doc(doc)
            self.docSent.emit(doc)
            # если выбрана пачка файлов — продолжаем с следующим
            if self.incoming_sel and self._tx_queue_idx < len(self.incoming_sel) - 1:
                self._send_next_in_batch()
            else:
                self.incoming_sel = []
                self._finish_tx_ok(num)
        except Exception as ex:
            self.incoming_sel = []
            self._show_error("TX FAIL", str(ex))

    def _finish_tx_ok(self, num: str):
        self._blink.stop()
        self._set_mode(MODE_IDLE)
        self._log_lcd(f"TX OK {num}")
        self.update()

    def _stop_current(self):
        if self.mode == MODE_TX:
            self.backend.abort(self._active_job)
            self.incoming_sel = []          # отменяем всю пачку
            self._blink.stop()
            self._set_mode(MODE_IDLE)
            self._log_lcd("TX CNCL")
        elif self.mode == MODE_RX:
            self._set_mode(MODE_IDLE)
            self._log_lcd("RX ABORT")
        elif self.mode in (MODE_VOICE, MODE_RING):
            self.offhook = False
            self._set_mode(MODE_IDLE)
            self._log_lcd("CALL END")
        self.update()

    def _toggle_hold(self):
        self.held = not self.held
        if self.held:
            self._poll.stop()
            self._set_mode(MODE_HOLD)
            self._log_lcd("HELD")
        else:
            self._poll.start()
            self._set_mode(MODE_IDLE)
            self._log_lcd("READY")
        self.update()

    def _open_inbox_folder(self):
        inbox = Path(cfgmod.get("incoming_dir") or str(Path.home()))
        inbox.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(inbox)))

    # ---------------- голосовые вызовы ----------------

    def set_voice_enabled(self, answer: bool, rings: Optional[int] = None):
        """Включить/выключить автоответ на голосовые вызовы (RingsVoiceAnswer)."""
        self.voice_enabled = bool(answer)
        if rings is not None:
            self.voice_rings = max(1, min(9, int(rings)))
        cfgmod.set_voice(self.voice_enabled, self.voice_rings)
        self._save_settings_to_spool()
        self._log_lcd("VOICE " + ("ON" if self.voice_enabled else "OFF"))
        self.update()

    def _check_voice(self):
        """Новые события из очереди voice-monitor: RING → автоответ по порогу."""
        if self.held:
            return
        try:
            events = hx.read_voice_calls(limit=8)
        except Exception:
            return
        for ev in events:                      # список newest-first
            key = ev.get("seen_at") or ev.get("ts") or ""
            if not key or key in self._seen_voice_ts:
                continue
            self._seen_voice_ts.add(key)
            if ev.get("kind", "").startswith("voice"):
                self._on_voice_event(ev)
                break                          # одно событие за тик
        if len(self._seen_voice_ts) > 500:
            self._seen_voice_ts = set(list(self._seen_voice_ts)[-200:])

    def _on_voice_event(self, ev: dict):
        num = ev.get("number") or "неизвестен"
        self.voiceCall.emit(ev)
        if self.mode in (MODE_TX, MODE_RX):    # линия занята факсом — не мешаем
            self._log_lcd(f"VOICE {num} BUSY")
            return
        self._set_mode(MODE_RING)
        self._log_lcd(f"VOICE FROM {num}")
        self._beep((440, 600))
        self.update()
        if self.voice_enabled:
            # «автоответ»: через N секунд имитируем снятие трубки
            delay = max(2, self.voice_rings * 1500)
            QTimer.singleShot(delay, lambda: self._auto_answer(num))

    def _auto_answer(self, num: str):
        if self.mode != MODE_RING:
            return
        self.offhook = True
        self._set_mode(MODE_VOICE)
        self._log_lcd(f"ANSWERED {num}")
        self._beep((500, 150))
        self.update()
        # «разговор» идёт, пока оператор не положит трубку кнопкой VOICE/STOP;
        # автосброс через 60 с, чтобы аппарат не залип в VOICE
        QTimer.singleShot(60000, lambda: self._hangup_if_voice())

    def _hangup_if_voice(self):
        if self.mode == MODE_VOICE:
            self.offhook = False
            self._set_mode(MODE_IDLE)
            self._log_lcd("TIMEOUT HANGUP")
            self.update()

    # ---------------- incoming monitoring ----------------

    def _check_incoming(self):
        if self.held:
            return
        for d in self.backend.watch_dirs():
            for f in sorted(d.glob("*.tif")) + sorted(d.glob("*.tiff")) + sorted(d.glob("*.pdf")):
                key = str(f)
                if key in self.seen_files:
                    continue
                self.seen_files.add(key)
                self._on_new_file(f)

    def _on_new_file(self, f: Path):
        pages = self._tiff_pages(f) if f.suffix.lower() in (".tif", ".tiff") else 1
        csid = self._extract_csid(f)
        if self.demo:
            self._sim_rx(f, pages, csid)
        else:
            doc = FaxDoc(f, pages, csid, "in")
            self.add_tray_doc(doc)
            self.docReceived.emit(doc)
            self._set_mode(MODE_RX)
            self._log_lcd(f"RX {pages}p FROM {csid}")
            self._rx_reset.setInterval(4000)
            self._rx_reset.start()
            self.update()

    @staticmethod
    def _extract_csid(f: Path) -> str:
        sidecar = f.with_suffix(".info")
        try:
            if sidecar.exists():
                for line in sidecar.read_text(errors="ignore").splitlines():
                    if line.lower().startswith("csid"):
                        return line.split("=", 1)[-1].strip() or "UNKNOWN"
        except Exception:
            pass
        m = re.search(r"f\d{6,}", f.stem)
        return m.group(0) if m else "UNKNOWN"

    @staticmethod
    def _tiff_pages(p: Path) -> int:
        try:
            img = QImage()
            reader = QImageReader(str(p))
            return max(1, reader.imageCount())
        except Exception:
            return 1

    @staticmethod
    def _pdf_pages(p: Path) -> int:
        try:
            out = subprocess.run(["pdfinfo", str(p)], capture_output=True, text=True).stdout
            m = re.search(r"Pages:\s+(\d+)", out)
            return int(m.group(1)) if m else 1
        except Exception:
            return 1

    # ---------------- LCD / термо-лента ----------------

    def _log_lcd(self, s: str):
        stamp = time.strftime("%H:%M")
        self.lcd_log.append(f"{stamp} {s}")
        self.lcd_log = self.lcd_log[-6:]
        self._render_lcd()

    def _render_lcd(self):
        self.update()

    # ---------------- sound ----------------

    def _beep(self, freq_ms=(800, 120)):
        """Best-effort звуковой сигнал (не критично для работы UI)."""
        try:
            f, ms = freq_ms
            dur = max(0.05, ms / 1000.0)
            subprocess.Popen(
                f"(aplay -q -r 8000 -f S16_LE -c1 <(/usr/bin/sox -n -r 8000 -c1 -b16 "
                f"-t wav - synth {dur:.2f} sine {f}) 2>/dev/null || true)",
                shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _beep_err(self):
        self._beep((300, 400))

    # ---------------- demo simulation ----------------

    def _demo_ring(self):
        if self.mode != MODE_IDLE or self.held:
            return
        self._set_mode(MODE_RING)
        self._log_lcd("INCOMING CALL")
        self._beep((440, 600))
        self.update()
        QTimer.singleShot(2500, self._demo_accept)

    def _demo_accept(self):
        if self.mode != MODE_RING:
            return
        fake = Path("/tmp/faxdemo-in.tif")
        if not fake.exists():
            try:
                subprocess.run(["convert", "-size", "400x600", "xc:white",
                                "-pointsize", "28", "-draw",
                                "text 60,300 'ДЕМО-ФАКС 123'", str(fake)], check=False)
            except Exception:
                pass
        self._on_new_file(fake)

    def _sim_rx(self, f: Path, pages: int, csid: str):
        """Пошаговая имитация приёма страниц."""
        self._set_mode(MODE_RX)
        self.rx_pages = 0
        self._log_lcd(f"RX FROM {csid}")

        def step(n=0):
            if n >= pages:
                doc = FaxDoc(f, pages, csid, "in")
                self.add_tray_doc(doc)
                self.docReceived.emit(doc)
                self._set_mode(MODE_IDLE)
                self._log_lcd(f"RCVD {pages}p OK")
                self.update()
                return
            self.rx_pages = n + 1
            self._log_lcd(f"RX page {n+1}/{pages}")
            self.update()
            QTimer.singleShot(1200, lambda: step(n + 1))
        step()

    def _sim_tx(self, pdf: Path, num: str):
        total = 9
        prog = {"p": 0}

        def step():
            prog["p"] += 1
            self.tx_progress = int(prog["p"] / total * 100)
            if prog["p"] == 1:
                self._log_lcd(f"DIALL {num}")
            elif prog["p"] == 3:
                self._log_lcd("CONNECT 14400")
            elif self.tx_progress % 25 == 0:
                self._log_lcd(f"TX {self.tx_progress}%")
            if prog["p"] >= total:
                doc = FaxDoc(pdf, self._pdf_pages(pdf), num, "out")
                self.add_tray_doc(doc)
                self.docSent.emit(doc)
                self.tx_progress = 0
                # пачка выбранных файлов в демо-режиме — следующий по списку
                if self.incoming_sel and self._tx_queue_idx < len(self.incoming_sel) - 1:
                    self._send_next_in_batch()
                    return
                self.incoming_sel = []
                self._blink.stop()
                self._set_mode(MODE_IDLE)
                self._log_lcd(f"SENT OK {num}")
                self.update()
                return
            QTimer.singleShot(700, step)
        step()

    # ---------------- painting ----------------

    def _panel_rect(self) -> QRect:
        b = self.rect()
        return QRect(10, int(b.height() * 0.42), b.width() - 20,
                     int(b.height() * 0.50))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        b = self.rect()
        w, h = b.width(), b.height()

        # ---------- корпус (тёмный пластик, скруглённый) ----------
        body = QRectF(6, h * 0.16, w - 12, h * 0.80)
        grad = QLinearGradient(body.topLeft(), body.bottomLeft())
        grad.setColorAt(0, QColor("#4a4f57"))
        grad.setColorAt(1, QColor("#2b2e33"))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor("#1b1d20"), 2))
        p.drawRoundedRect(body, 18, 18)

        # ---------- крышка ADF (светлее, с бликом) ----------
        lid = QRectF(14, h * 0.10, w - 28, h * 0.24)
        lg = QLinearGradient(lid.topLeft(), lid.bottomLeft())
        lg.setColorAt(0, QColor("#6b7280"))
        lg.setColorAt(1, QColor("#3f444b"))
        p.setBrush(QBrush(lg))
        p.setPen(QPen(QColor("#22252a"), 2))
        p.drawRoundedRect(lid, 14, 14)
        # блик
        p.setBrush(QColor(255, 255, 255, 26))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(lid.x() + 8, lid.y() + 4, lid.width() - 16, lid.height() * 0.35), 10, 10)

        # щель подачи бумаги сверху крышки
        feed = QRectF(w * 0.18, h * 0.085, w * 0.5, 8)
        p.setBrush(QColor("#15171a"))
        p.setPen(QPen(QColor("#0a0b0d"), 1))
        p.drawRoundedRect(feed, 4, 4)

        # ---------- лоток выдачи: бумага с текстом торчит из щели ----------
        slot = QRectF(self._tray_slot())
        p.setBrush(QColor("#101214"))
        p.setPen(QPen(QColor("#000"), 1))
        p.drawRoundedRect(slot, 3, 3)
        if self.tray_docs:
            last = self.tray_docs[-1]
            paper = QRectF(slot.x() + 4, slot.y() - 30, slot.width() - 8, 30)
            p.setBrush(QColor("#f6f3ea"))
            p.setPen(QPen(QColor("#c9c4b4"), 1))
            p.drawRoundedRect(paper, 2, 2)
            p.setPen(QColor("#8a8676"))
            p.setFont(QFont("Sans Serif", 6))
            txt = f"{last.kind.upper()} {last.number} · {last.pages} стр."
            p.drawText(paper.adjusted(6, 4, -6, -2), Qt.AlignLeft | Qt.AlignTop, txt)
            for i in range(3):
                ln = QRectF(paper.x() + 6, paper.y() + 14 + i * 5,
                            paper.width() * (0.85 - i * 0.18), 2)
                p.fillRect(ln, QColor("#b9b4a2"))

        # ---------- трубка слева, витой шнур ----------
        hs_w = max(26, int(w * 0.075))
        hs = QRectF(10, h * 0.30, hs_w, h * 0.42)
        hg = QLinearGradient(hs.left(), 0, hs.right(), 0)
        hg.setColorAt(0, QColor("#33373d"))
        hg.setColorAt(0.5, QColor("#565c64"))
        hg.setColorAt(1, QColor("#2c2f34"))
        p.setBrush(QBrush(hg))
        p.setPen(QPen(QColor("#17181b"), 2))
        # форма трубки: утолщения по краям
        path = QPainterPath()
        path.addRoundedRect(hs.adjusted(0, 0, 0, 0), 10, 10)
        p.fillPath(path, QBrush(hg))
        p.drawRoundedRect(hs, 10, 10)
        p.setBrush(QColor("#1d1f22"))
        p.drawRoundedRect(QRectF(hs.x() + 3, hs.y() + 2, hs.width() - 6, hs.height() * 0.22), 8, 8)
        p.drawRoundedRect(QRectF(hs.x() + 3, hs.bottom() - hs.height() * 0.22 - 2,
                                 hs.width() - 6, hs.height() * 0.22), 8, 8)
        # витой шнур
        cord = QPainterPath()
        x_start = hs.center().x() + hs.width() / 2
        y_mid = hs.center().y()
        cord.moveTo(x_start, y_mid)
        coils = 7
        for i in range(coils):
            cx = x_start + 6 + i * 4
            cord.cubicTo(QPointF(cx, y_mid - 9), QPointF(cx + 3, y_mid + 9),
                         QPointF(cx + 6, y_mid))
        p.setPen(QPen(QColor("#1a1c1f"), 3))
        p.drawPath(cord)

        # ---------- панель управления ----------
        panel = self._panel_rect()
        pg = QLinearGradient(panel.topLeft(), panel.bottomLeft())
        pg.setColorAt(0, QColor("#23262b"))
        pg.setColorAt(1, QColor("#171a1e"))
        p.setBrush(QBrush(pg))
        p.setPen(QPen(QColor("#0d0f12"), 2))
        p.drawRoundedRect(QRectF(panel), 12, 12)

        # ---------- LCD ----------
        lcd = QRectF(self._lcd_rect(panel))
        p.setBrush(QColor("#0c2f18"))
        p.setPen(QPen(QColor("#050d07"), 3))
        p.drawRoundedRect(lcd, 6, 6)
        glow = QRadialGradient(lcd.center(), lcd.width() / 1.5)
        glow.setColorAt(0, QColor(40, 120, 60, 60))
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawRect(lcd)
        # текст экрана
        p.setFont(self.f_lcd)
        head_color = {MODE_IDLE: "#7CFC9A", MODE_RING: "#ffe066", MODE_RX: "#66d9ff",
                      MODE_TX: "#ffd166", MODE_BUSY: "#ff6b6b", MODE_ERROR: "#ff5252",
                      MODE_HOLD: "#c0a6ff", MODE_VOICE: "#ffb84d"}.get(self.mode, "#7CFC9A")
        p.setPen(QColor(head_color))
        status = self._status_line()
        p.drawText(lcd.adjusted(8, 5, -8, 0), Qt.AlignLeft | Qt.AlignTop, status)
        p.setPen(QColor("#4fae6d"))
        small = QFont(self.f_lcd)
        small.setPointSizeF(max(6.5, self.f_lcd.pointSizeF() - 2.5))
        p.setFont(small)
        lines = "\n".join(self.lcd_log[-3:])
        p.drawText(lcd.adjusted(8, 26, -8, -4), Qt.AlignLeft | Qt.AlignTop, lines)
        if self.digits:
            p.setPen(QColor("#e8f8ee"))
            p.drawText(lcd.adjusted(8, 5, -8, 0), Qt.AlignRight | Qt.AlignTop,
                       self.digits[-16:])

        # ---------- клавиатура ----------
        rects = self._key_rects(panel)
        for k, r in rects.items():
            hovered = self._hover_key == k
            pressed = self._pressed_key == k
            special = k in ("TxD", "RxD", "STOP", "HOLD", "SAVE", "VOICE")
            base = {"TxD": QColor("#1d6b34"), "RxD": QColor("#175a75"),
                    "STOP": QColor("#8c2020"), "HOLD": QColor("#5a4a17"),
                    "SAVE": QColor("#3a3f47"),
                    "VOICE": (QColor("#7a5a12") if self.offhook
                              else QColor("#8a6d1f"))}.get(
                        k, QColor("#3b4048") if not hovered else QColor("#4a505a"))
            if pressed:
                base = base.darker(130)
            p.setBrush(base)
            p.setPen(QPen(QColor("#0a0b0d"), 1))
            rr = QRectF(r).adjusted(0, 0 if not pressed else 2, 0, 0 if not pressed else 2)
            p.drawRoundedRect(rr, 5, 5)
            p.setPen(QColor("#e8ecf2"))
            p.setFont(self.f_btn if special else QFont(self.f_btn))
            label = {"TxD": "TX▲ ОТПР.", "RxD": "RX▼ ПРИН.", "STOP": "■ СТОП",
                     "HOLD": "⏸ HOLD", "REDIAL": "REDIAL", "<": "◀ C",
                     "SAVE": "💾 ПАПКА", "VOICE": "📞 ТРУБКА"}.get(k, k)
            p.drawText(rr, Qt.AlignCenter, label)

        # ---------- индикатор + динамика ----------
        ind_x = panel.right() - 26
        ind_y = panel.top() + 8
        active = self.mode in (MODE_RX, MODE_TX, MODE_RING, MODE_VOICE)
        col = (QColor("#2eff6a") if (not active or self.indicator_on) else QColor("#0b3d16")) \
            if self.mode != MODE_ERROR else QColor("#ff4040")
        rg = QRadialGradient(ind_x + 6, ind_y + 6, 14)
        rg.setColorAt(0, col.lighter(160))
        rg.setColorAt(1, col.darker(200))
        p.setPen(QPen(QColor("#000"), 1))
        p.setBrush(rg)
        p.drawEllipse(QRectF(ind_x, ind_y, 12, 12))
        p.setPen(QColor("#9aa3ad"))
        p.setFont(self.f_lbl)
        p.drawText(QRectF(ind_x - 40, ind_y - 2, 38, 12), Qt.AlignRight | Qt.AlignVCenter,
                   "INDICATOR")
        # grille динамика
        gx0 = panel.right() - 34
        gy0 = panel.bottom() - 30
        p.setPen(QPen(QColor("#0c0d0f"), 1))
        for i in range(4):
            p.drawLine(gx0, gy0 + i * 5, gx0 + 22, gy0 + i * 5)

        # ---------- бренд ----------
        p.setPen(QColor("#aab2bd"))
        p.setFont(QFont("Sans Serif", 8, QFont.Bold))
        p.drawText(QRectF(14, h * 0.10 + 2, w - 28, 20), Qt.AlignCenter,
                   "Fax-PDF · HYLAFAX")
        p.end()

    def _status_line(self) -> str:
        if self.mode == MODE_IDLE:
            return "READY"
        if self.mode == MODE_RING:
            return "RING... ВХОД. ВЫЗОВ"
        if self.mode == MODE_VOICE:
            return "📞 ГОЛОС. СВЯЗЬ (off-hook)"
        if self.mode == MODE_RX:
            return f"RX ПРИЁМ стр.{self.rx_pages}"
        if self.mode == MODE_TX:
            return f"TX ОТПРАВКА {self.tx_progress}%"
        if self.mode == MODE_HOLD:
            return "HOLD ПАУЗА"
        if self.mode == MODE_ERROR:
            return "ERROR!"
        return self.mode

    def _toggle_blink(self):
        self.indicator_on = not self.indicator_on
        self.update()

    def _set_mode(self, m: str):
        self.mode = m
        if m in (MODE_RX, MODE_TX, MODE_RING, MODE_VOICE):
            self._blink.start()
        else:
            self._blink.stop()
            self.indicator_on = True

    def _show_error(self, title: str, msg: str):
        self._set_mode(MODE_ERROR)
        self._log_lcd(title)
        self.error.emit(msg)
        QToolTip.showText(self.mapToGlobal(self.rect().center()), f"{title}: {msg}", self)
        QTimer.singleShot(3500, lambda: self._set_mode(MODE_IDLE))
        self.update()


# ------------------------------------------------------------------ demo ---

class DemoWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fax-PDF — демонстрация аппарата")
        m = FaxMachineWidget(demo=True)
        self.setCentralWidget(m)
        self.resize(560, 420)
        st = self.statusBar()
        m.docReceived.connect(lambda d: st.showMessage(f"Принято: {d.path.name} ({d.pages} стр.)"))
        m.docSent.connect(lambda d: st.showMessage(f"Отправлено: {d.path.name} → {d.number}"))
        m.error.connect(lambda s: st.showMessage(f"Ошибка: {s}"))


def main():
    app = QApplication([])
    app.setApplicationName("Fax-PDF")
    win = DemoWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
