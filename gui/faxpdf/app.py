#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
faxpdf/app.py — графический интерфейс "Факс-приёмник" (PyQt5).

Возможности:
  Вкладка «Приём»:
    * live-мониторинг recvq HylaFAX: как только faxgetty принимает факс,
      он появляется в списке; по мере поступления страниц растёт миниатюра
      (TIFF конвертируется на лету через pdftoppm/convert);
    * большая панель предпросмотра текущей страницы + лист миниатюр;
    * кнопки: «Сохранить как PDF», «Открыть в просмотрщике», «Печать»;
    * звуковое/системное уведомление о новом входящем факсе.
  Вкладка «Отправка»:
    * выбор PDF, номер, комментарий; очередь задач (faxstat) со статусами,
      отмена задачи, повтор.
  Журнал внизу: все события приёма/отправки.

Запуск:  fax-pdf-gui   (или python3 -m faxpdf)
"""

import os
import sys
import time
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QSize, QUrl
from PyQt5.QtGui import (
    QIcon, QPixmap, QImageReader, QDesktopServices, QColor, QPalette,
    QStandardItemModel, QStandardItem,
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QListWidget, QListWidgetItem, QTextEdit, QLineEdit, QPushButton, QLabel,
    QFileDialog, QMessageBox, QSplitter, QToolBar, QAction, QStatusBar,
    QListView, QSizePolicy, QProgressBar, QFrame, QSystemTrayIcon,
)

try:
    from faxpdf import hylafaxd as hx
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import hylafaxd as hx

APP_TITLE = "Факс-приёмник — Fax-PDF (HylaFAX)"
PREVIEW_DIR = Path(os.environ.get("FAX_GUI_PREVIEW",
                                  str(Path.home() / ".cache" / "fax-pdf" / "preview")))
INBOX_DIR = Path(os.environ.get("FAX_INCOMING", str(Path.home() / "Факсы" / "Входящие")))


# ---------------------------------------------------------------- helpers ---

def tiff_to_pixmap(path: Path, page_index: int = 0):
    """Конвертировать страницу TIFF/PDF -> QPixmap (png через ImageMagick/pdftoppm)."""
    suffix = path.suffix.lower()
    tmp = PREVIEW_DIR / f"{path.stem}_p{page_index}.png"
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    if not tmp.exists():
        try:
            if suffix in (".tif", ".tiff"):
                conv = shutil.which("convert") or shutil.which("magick")
                if conv:
                    src = f"{path}[{page_index}]"
                    args = ([conv, "convert"] if conv.endswith("magick") else [conv]) \
                        + ["-density", "150", src, str(tmp)]
                    subprocess.run(args, timeout=60, capture_output=True)
            elif suffix == ".pdf":
                pp = shutil.which("pdftoppm")
                if pp:
                    base = PREVIEW_DIR / f"{path.stem}"
                    subprocess.run([pp, "-f", str(page_index + 1),
                                    "-l", str(page_index + 1), "-r", "100",
                                    "-png", str(path), str(base)],
                                   timeout=60, capture_output=True)
                    gen = Path(f"{base}-{page_index + 1:02d}.png")
                    if gen.exists():
                        tmp = gen
        except Exception:
            pass
    if tmp.exists():
        pm = QPixmap(str(tmp))
        if not pm.isNull():
            return pm
    return None


class ReceiveWorker(QThread):
    """Фоновый опрос recvq/doneq (без inotify-зависимости в GUI)."""
    new_fax = pyqtSignal(object)      # ReceivedFax
    updated = pyqtSignal(object)      # ReceivedFax (страниц добавилась)
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = False
        self._seen = {}               # name -> pages

    def stop(self):
        self._stop = True

    def run(self):
        while not self._stop:
            try:
                for rx in hx.list_incoming():
                    pages = hx.count_pages(rx.path)
                    known = self._seen.get(rx.key)
                    if known is None:
                        self._seen[rx.key] = pages
                        self.new_fax.emit(rx)
                    elif pages > known:
                        self._seen[rx.key] = pages
                        self.updated.emit(rx)
            except Exception as e:          # noqa: BLE001
                self.error.emit(str(e))
            time.sleep(2)


class SendWorker(QThread):
    done = pyqtSignal(str)   # текст результата
    fail = pyqtSignal(str)

    def __init__(self, pdf_path, number, comment="", parent=None):
        super().__init__(parent)
        self.args = (pdf_path, number, comment)

    def run(self):
        try:
            out = hx.send_pdf(*self.args)
            self.done.emit(out)
        except Exception as e:              # noqa: BLE001
            self.fail.emit(str(e))


# ------------------------------------------------------------------ main ---

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1100, 720)
        self._active_rx = None
        self._jobs_cache = []

        # -------- toolbar / statusbar --------
        tb = QToolBar("Главная")
        tb.setMovable(False)
        self.addToolBar(tb)
        act_refresh = QAction("⟳ Обновить", self)
        act_refresh.triggered.connect(self.refresh_all)
        tb.addAction(act_refresh)
        act_quit = QAction("Выход", self)
        act_quit.triggered.connect(self.close)
        tb.addAction(act_quit)

        self.statusBar().showMessage("Готов")
        self.lbl_modem = QLabel("модем: …")
        self.statusBar().addPermanentWidget(self.lbl_modem)

        # -------- tabs (вкладка «аппарат» строится первой) --------
        self.machine_tab = self._build_machine_tab()
        self.recv_tab = self._build_recv_tab()
        self.send_tab = self._build_send_tab()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.machine_tab, "📠 Факс-аппарат")
        self.tabs.addTab(self.recv_tab, "📥 Приём")
        self.tabs.addTab(self.send_tab, "📤 Отправка")

        # -------- log --------
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.tabs, 3)
        lay.addWidget(self.log, 1)
        self.setCentralWidget(central)

        # -------- tray + notify --------
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(QIcon.fromTheme("fax"), self)
            self.tray.setToolTip(APP_TITLE)
            self.tray.show()

        # -------- workers/timers --------
        self.worker = ReceiveWorker(self)
        self.worker.new_fax.connect(self.on_new_fax)
        self.worker.updated.connect(self.on_update_fax)
        self.worker.error.connect(lambda m: self.log_line("ОШИБКА монитора: " + m))
        self.worker.start()

        self.timer_jobs = QTimer(self)
        self.timer_jobs.timeout.connect(self.refresh_jobs)
        self.timer_jobs.start(10000)

        self.refresh_all()
        self.log_line("Интерфейс запущен. Spool: %s" % hx.SPOOL)

    # ------------------------------------------------- вкладка «аппарат» ---
    def _build_machine_tab(self):
        """Главная вкладка: факс в виде настоящего факсимильного аппарата."""
        from PyQt5.QtWidgets import QHBoxLayout, QLabel, QSizePolicy
        from faxpdf.faxwidget import FaxMachineWidget
        w = QWidget()
        h = QHBoxLayout(w)
        self.machine = FaxMachineWidget(demo=False)
        self.machine.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # связываем аппарат с событиями основного монитора HylaFAX
        self.machine.docReceived.connect(
            lambda d: self.log_line(f"АППАРАТ: принят {d.path.name} ({d.pages} стр.) от {d.number}"))
        self.machine.docSent.connect(
            lambda d: self.log_line(f"АППАРАТ: отправлен {d.path.name} → {d.number}"))
        self.machine.error.connect(lambda m: self.log_line("АППАРАТ: ошибка — " + m))
        side = QVBoxLayout()
        side.addWidget(QLabel("<b>Факсимильный аппарат</b>"))
        side.addWidget(QLabel(
            "Наберите номер на клавиатуре и нажмите <b>TX▲</b> — выбор PDF и отправка.<br>"
            "Входящие факсы принимаются автоматически: экран покажет RING → RX, "
            "готовый документ появится в лотке выдачи (клик — открыть).<br>"
            "<b>RxD</b> — папка «Входящие», <b>STOP</b> — прервать передачу, "
            "<b>HOLD</b> — пауза приёма."))
        side.addStretch(1)
        h.addWidget(self.machine, 3)
        h.addLayout(side, 1)
        return w

    # ------------------------------------------------------------ приём ---
    def _build_recv_tab(self):
        # ВАЖНО: h — layout БЕЗ родителя; виджеты добавляются в него, затем он
        # передаётся через addLayout в ol (родитель outer). Иначе PyQt удаляет
        # дочерние C++-объекты вместе с локальной переменной w (см. историю).
        h = QHBoxLayout()

        left = QVBoxLayout()
        self.lst_recv = QListWidget()
        self.lst_recv.currentItemChanged.connect(self.on_select_recv)
        self.lst_recv.setAlternatingRowColors(True)
        left.addWidget(QLabel("<b>Входящие факсы</b>"))
        left.addWidget(self.lst_recv, 1)
        btns = QHBoxLayout()
        self.btn_save_pdf = QPushButton("💾 Сохранить PDF")
        self.btn_save_pdf.clicked.connect(self.save_current_as_pdf)
        self.btn_open = QPushButton("📂 Открыть")
        self.btn_open.clicked.connect(self.open_current)
        self.btn_next_pg = QPushButton("▶ стр.")
        self.btn_next_pg.clicked.connect(lambda: self.step_page(+1))
        self.btn_prev_pg = QPushButton("◀ стр.")
        self.btn_prev_pg.clicked.connect(lambda: self.step_page(-1))
        btns.addWidget(self.btn_save_pdf)
        btns.addWidget(self.btn_open)
        btns.addStretch(1)
        btns.addWidget(self.btn_prev_pg)
        btns.addWidget(self.btn_next_pg)
        left.addLayout(btns)
        h.addLayout(left, 2)

        right = QVBoxLayout()
        self.lbl_fax_info = QLabel("Факс не выбран")
        self.lbl_fax_info.setWordWrap(True)
        right.addWidget(self.lbl_fax_info)
        self.pg_recv = QProgressBar()
        self.pg_recv.setRange(0, 0)   # busy-indicator при живом приёме
        self.pg_recv.setVisible(False)
        right.addWidget(self.pg_recv)
        self.thumbs_model = QStandardItemModel(self)
        self.thumbs = QListView()
        self.thumbs.setModel(self.thumbs_model)
        self.thumbs.setViewMode(QListView.IconMode)
        self.thumbs.setIconSize(QSize(96, 128))
        self.thumbs.setResizeMode(QListView.Adjust)
        self.thumbs.setMovement(QListView.Static)
        self.thumbs.setMaximumHeight(170)
        self.thumbs.clicked.connect(
            lambda idx: self.show_page(idx.data(Qt.UserRole)))
        right.addWidget(self.thumbs, 0)
        self.view = QLabel("Ожидание вызова…\n\nПринимаемые факсы появятся здесь автоматически.")
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.view.setStyleSheet("background:#555;color:#ddd;border-radius:6px;")
        self.view.setScaledContents(False)
        right.addWidget(self.view, 1)
        h.addLayout(right, 3)

        outer = QWidget()
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.addLayout(h)
        return outer

    def _item_for(self, rx: hx.ReceivedFax) -> QListWidgetItem:
        state = "✔ принят" if rx.done else "⏳ идёт приём…"
        it = QListWidgetItem(
            f"{state}\nОт: {rx.sender_csid or rx.sender_tsi or 'неизвестно'}\n"
            f"{rx.path.name}")
        it.setData(Qt.UserRole, str(rx.path))
        it.setToolTip(str(rx.path))
        return it

    def on_new_fax(self, rx):
        self.log_line(f"НОВЫЙ ВХОДЯЩИЙ факс от {rx.sender_csid or '?'}: {rx.path.name}")
        # показать входящий на «аппарате» (вкладка Факс-аппарат)
        if getattr(self, "machine", None) is not None:
            try:
                from faxpdf.faxwidget import FaxDoc, MODE_RX, MODE_IDLE
                csid = rx.sender_csid or "?"
                self.machine.add_tray_doc(FaxDoc(rx.path, rx.pages or 1,
                                                 csid, "in"))
                self.machine._set_mode(MODE_RX)
                self.machine._log_lcd(f"RX FROM {csid}")
                # через 6 с экран возвращается в READY, если приём завершён
                def _reset():
                    if self.machine.mode == MODE_RX:
                        self.machine._set_mode(MODE_IDLE)
                QTimer.singleShot(6000, _reset)
            except Exception as e:
                self.log_line("АППАРАТ: не удалось показать входящий: %s" % e)
        self.lst_recv.addItem(self._item_for(rx))
        item = self.lst_recv.item(self.lst_recv.count() - 1)
        self.lst_recv.setCurrentItem(item)
        if self.tray:
            self.tray.showMessage("Входящий факс",
                                  f"От: {rx.sender_csid or 'неизвестно'}",
                                  QSystemTrayIcon.Information, 8000)
        QApplication.beep()

    def on_update_fax(self, rx):
        for i in range(self.lst_recv.count()):
            it = self.lst_recv.item(i)
            if it.data(Qt.UserRole) == str(rx.path):
                it.setText(self._item_for(rx).text())
        if self._active_rx and self._active_rx.path == rx.path:
            self.render_fax(rx)

    def on_select_recv(self, cur, _prev):
        if not cur:
            return
        p = Path(cur.data(Qt.UserRole))
        for rx in hx.list_incoming():
            if rx.path == p:
                self._active_rx = rx
                self._cur_page = 0
                self.render_fax(rx)
                return
        # файла уже нет (монитор унёс в архив) — показываем из архива
        rx = hx.ReceivedFax(path=p, done=True)
        rx.is_pdf = p.suffix.lower() == ".pdf"
        self._active_rx = rx
        self._cur_page = 0
        self.render_fax(rx)

    def show_page(self, page_no):
        if not self._active_rx:
            return
        try:
            self._cur_page = int(page_no)
        except (TypeError, ValueError):
            return
        self.render_fax(self._active_rx)

    def step_page(self, delta):
        if not self._active_rx:
            return
        total = max(1, hx.count_pages(self._active_rx.path))
        self._cur_page = max(0, min(total - 1, getattr(self, "_cur_page", 0) + delta))
        self.render_fax(self._active_rx)

    def render_fax(self, rx):
        info = (f"<b>{rx.path.name}</b><br>"
                f"Отправитель (CSID): {rx.sender_csid or '—'}<br>"
                f"Статус: {'принят полностью' if rx.done else 'приём продолжается…'}")
        pages = hx.count_pages(rx.path)
        if pages:
            info += f"<br>Страниц принято: {pages}"
        self.lbl_fax_info.setText(info)
        self.pg_recv.setVisible(not rx.done)

        # перерисовываем миниатюры только при изменении числа страниц —
        # иначе каждый тик опроса перечитывал бы все страницы
        sig = (str(rx.path), pages)
        if getattr(self, "_thumbs_sig", None) != sig:
            self._thumbs_sig = sig
            self.thumbs_model.clear()
            for i in range(max(1, pages)):
                pm = tiff_to_pixmap(rx.path, i)
                it = QStandardItem(
                    QIcon(pm) if pm else QIcon.fromTheme("application-pdf"),
                    f"стр. {i + 1}")
                it.setData(i, Qt.UserRole)
                it.setEditable(False)
                self.thumbs_model.appendRow(it)

        pm = tiff_to_pixmap(rx.path, getattr(self, "_cur_page", 0))
        if pm and not pm.isNull():
            scaled = pm.scaled(self.view.size(), Qt.KeepAspectRatio,
                               Qt.SmoothTransformation)
            self.view.setPixmap(scaled)
            self.view.setStyleSheet("background:#fff;border-radius:6px;padding:4px;")
        else:
            self.view.setText("Изображение ещё не готово\n(страницы принимаются)")

    def save_current_as_pdf(self):
        rx = self._active_rx
        if not rx:
            QMessageBox.information(self, APP_TITLE, "Выберите факс в списке.")
            return
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        dst = hx.archive_received(rx, INBOX_DIR)
        if dst:
            self.log_line(f"Сохранено: {dst}")
            QMessageBox.information(self, APP_TITLE, f"PDF сохранён:\n{dst}")
        else:
            QMessageBox.warning(self, APP_TITLE,
                                "Не удалось сконвертировать в PDF (нужен tiff2pdf/ghostscript).")

    def open_current(self):
        rx = self._active_rx
        if not rx:
            return
        if rx.is_pdf:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(rx.path)))
        else:
            INBOX_DIR.mkdir(parents=True, exist_ok=True)
            dst = hx.tiff_to_pdf(rx.path, INBOX_DIR)
            if dst:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(dst)))

    # ---------------------------------------------------------- отправка ---
    def _build_send_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        form = QHBoxLayout()
        self.ed_pdf = QLineEdit()
        self.ed_pdf.setPlaceholderText("Документ PDF для отправки…")
        btn_browse = QPushButton("Обзор…")
        btn_browse.clicked.connect(self.pick_pdf)
        self.ed_num = QLineEdit()
        self.ed_num.setPlaceholderText("+7XXXXXXXXXX")
        self.ed_num.setMaximumWidth(180)
        self.ed_cmt = QLineEdit()
        self.ed_cmt.setPlaceholderText("Комментарий (в ID-строке/логе)")
        self.ed_cmt.setMaximumWidth(220)
        self.btn_send = QPushButton("📨 Отправить факс")
        self.btn_send.clicked.connect(self.do_send)
        form.addWidget(QLabel("PDF:"))
        form.addWidget(self.ed_pdf, 3)
        form.addWidget(btn_browse)
        form.addWidget(QLabel("№:"))
        form.addWidget(self.ed_num)
        form.addWidget(self.ed_cmt)
        form.addWidget(self.btn_send)
        v.addLayout(form)

        split = QSplitter(Qt.Horizontal)
        self.lst_jobs = QListWidget()
        box_left = QVBoxLayout()
        box_left.addWidget(QLabel("<b>Очередь отправки (faxstat)</b>"))
        box_left.addWidget(self.lst_jobs)
        wl = QWidget(); wl.setLayout(box_left)
        split.addWidget(wl)

        self.txt_jobinfo = QTextEdit(); self.txt_jobinfo.setReadOnly(True)
        box_right = QVBoxLayout()
        box_right.addWidget(QLabel("<b>Подробности задачи</b>"))
        box_right.addWidget(self.txt_jobinfo)
        wr = QWidget(); wr.setLayout(box_right)
        split.addWidget(wr)
        v.addWidget(split, 1)

        row = QHBoxLayout()
        b_cancel = QPushButton("✖ Отменить задачу")
        b_cancel.clicked.connect(self.cancel_selected)
        b_refr = QPushButton("⟳ Обновить очередь")
        b_refr.clicked.connect(self.refresh_jobs)
        row.addWidget(b_cancel); row.addWidget(b_refr); row.addStretch(1)
        v.addLayout(row)
        return w

    def pick_pdf(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Выберите PDF", str(Path.home()),
                                            "Документы PDF (*.pdf)")
        if fn:
            self.ed_pdf.setText(fn)

    def do_send(self):
        pdf, num = self.ed_pdf.text().strip(), self.ed_num.text().strip()
        if not pdf or not Path(pdf).exists():
            QMessageBox.warning(self, APP_TITLE, "Укажите существующий PDF.")
            return
        if not num:
            QMessageBox.warning(self, APP_TITLE, "Укажите номер получателя.")
            return
        self.btn_send.setEnabled(False)
        self.statusBar().showMessage("Конвертация PDF → TIFF G4 и постановка в очередь…")
        self._sw = SendWorker(pdf, num, self.ed_cmt.text().strip(), self)
        self._sw.done.connect(self.on_sent)
        self._sw.fail.connect(self.on_send_fail)
        self._sw.start()

    def on_sent(self, out):
        self.btn_send.setEnabled(True)
        self.statusBar().showMessage("Отправлено в очередь")
        self.log_line("ОТПРАВКА: " + out.replace("\n", " | "))
        self.refresh_jobs()

    def on_send_fail(self, err):
        self.btn_send.setEnabled(True)
        self.statusBar().showMessage("Ошибка отправки")
        self.log_line("ОШИБКА отправки: " + err)
        QMessageBox.critical(self, APP_TITLE, "Не удалось отправить:\n" + err)

    def refresh_jobs(self):
        jobs = hx.queue_jobs()
        self._jobs_cache = jobs
        self.lst_jobs.clear()
        for j in jobs:
            it = QListWidgetItem(f"[{j.jobid}] → {j.number}  {j.info[:60]}")
            it.setData(Qt.UserRole, j.jobid)
            self.lst_jobs.addItem(it)
        st = hx.modem_status()
        self.lbl_modem.setText("модем: " + st)

    def cancel_selected(self):
        it = self.lst_jobs.currentItem()
        if not it:
            return
        try:
            out = hx.cancel_job(it.data(Qt.UserRole))
            self.log_line(f"Отмена [{it.data(Qt.UserRole)}]: {out}")
        except Exception as e:              # noqa: BLE001
            QMessageBox.warning(self, APP_TITLE, str(e))
        self.refresh_jobs()

    # -------------------------------------------------------------- misc ---
    def log_line(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {msg}")

    def refresh_all(self):
        self.refresh_jobs()

    def closeEvent(self, ev):
        self.worker.stop()
        self.worker.wait(3000)
        super().closeEvent(ev)


def run(argv):
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(argv)
    app.setApplicationName("Факс-приёмник")
    if "--demo" in argv:
        # демонстрационный режим: факсимильный аппарат с имитацией
        # звонка/приёма/отправки — работает без HylaFAX и модема
        from faxpdf.faxwidget import DemoWindow
        w = DemoWindow()
    else:
        w = MainWindow()
    w.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(run(sys.argv))
