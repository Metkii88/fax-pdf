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
    QGroupBox, QCheckBox, QSpinBox, QComboBox,
)

try:
    from faxpdf import hylafaxd as hx
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import hylafaxd as hx

APP_TITLE = "Факс-приёмник — Fax-PDF (HylaFAX)"
APP_VERSION = "1.3.9"


def _about_text():
    return (
        "<h3>Факс-приёмник (Fax-PDF) %s</h3>"
        "<p>Программа приёма и отправки факсов в формате PDF для "
        "Astra Linux 1.7 SE на основе HylaFAX.</p>"
        "<p><b>Возможности:</b> графический интерфейс «Факсимильный аппарат», "
        "автоопределение модемов (ttyS*/ttyUSB*/ttyACM*), голосовые вызовы, "
        "мандатный контроль целостности (МКЦ/PARSEC), сворачивание в трей, "
        "фоновый приём с уведомлениями.</p>"
        "<hr>"
        "<p><b>Автор:</b> Claude (AI-ассистент Anthropic)<br>"
        "<b>Идея, постановка задачи и дизайн:</b> пользователь (заказчик)<br>"
        "Проект создан по замыслу пользователя: https://github.com/Metkii88/fax-pdf</p>"
    ) % APP_VERSION


def _app_icon():
    """Иконка приложения: PNG-иконка факса (Flaticon) из data/ или /usr/share."""
    cand = [
        Path(__file__).resolve().parent / "resources" / "fax-app.png",
        Path(__file__).resolve().parents[2] / "data" / "fax-pdf.png",
        Path("/usr/share/icons/hicolor/512x512/apps/fax-pdf.png"),
        Path("/usr/share/icons/hicolor/scalable/apps/fax-pdf.svg"),
    ]
    for c in cand:
        if c.is_file():
            return QIcon(str(c))
    return QIcon.fromTheme("fax", QIcon.fromTheme("scanner"))
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
        # поток может быть уже мёртв (исключение/завершение) — не даём quit_app упасть
        try:
            if not self.isRunning():
                return
        except RuntimeError:
            return
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
        ic = _app_icon()
        if not ic.isNull():
            self.setWindowIcon(ic)
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
        act_min = QAction("🔽 В трей", self)
        act_min.setToolTip("Свернуть окно в системный трей; приём факсов продолжается в фоне")
        act_min.triggered.connect(self.hide_to_tray)
        tb.addAction(act_min)
        act_quit = QAction("Выход", self)
        act_quit.triggered.connect(self.quit_app)
        tb.addAction(act_quit)

        self.statusBar().showMessage("Готов")
        self.lbl_modem = QLabel("модем: …")
        self.statusBar().addPermanentWidget(self.lbl_modem)

        # -------- главное меню (верхняя строка): Справка → О программе ----
        from PyQt5.QtWidgets import QMenu
        mb = self.menuBar()
        m_help = QMenu("Справка", self)
        act_about = QAction("ℹ️ О программе", self)
        act_about.setMenuRole(QAction.AboutRole)
        act_about.triggered.connect(self.show_about)
        m_help.addAction(act_about)
        mb.addMenu(m_help)

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

        # -------- tray + notify (сворачивание в трей, работа в фоне) --------
        self._really_quit = False
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(_app_icon(), self)
            self.tray.setToolTip(APP_TITLE)
            from PyQt5.QtWidgets import QMenu
            m = QMenu()
            a_show = m.addAction("📠 Открыть факс-приёмник")
            a_show.triggered.connect(self.show_from_tray)
            a_min = m.addAction("🔽 Свернуть в трей")
            a_min.triggered.connect(self.hide_to_tray)
            m.addSeparator()
            a_quit = m.addAction("✕ Выход")
            a_quit.triggered.connect(self.quit_app)
            self.tray_menu = m
            self.tray.setContextMenu(m)
            self.tray.activated.connect(self._on_tray_activated)
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

        self._fill_modems()
        self.refresh_all()
        self.log_line("Интерфейс запущен. Spool: %s" % hx.SPOOL)
        if "--minimized" in sys.argv or os.environ.get("FAX_START_MINIMIZED") == "1":
            self.hide_to_tray(notify=False)
        if self.tray is None:
            self.log_line("Трей недоступен в текущем окружении — закрытие окна завершит приложение.")

    # ------------------------------------------------- вкладка «аппарат» ---
    def _fill_modems(self, select_dev=None):
        """Заполнить комбобокс модемов (ttyS*/ttyUSB*/ttyACM*) и выбрать сохранённый."""
        from faxpdf import settings as cfgmod
        self.cb_modem.clear()
        try:
            mods = hx.list_modems()
        except Exception as e:
            mods = []
            self.log_line("ОШИБКА сканирования модемов: " + str(e))
        if not mods:
            self.cb_modem.addItem("(модемы не найдены)", "")
        for m in mods:
            vidpid = ""
            if m.get("vendor_id"):
                vidpid = f" [{m['vendor_id']}:{m['product_id']}]"
            tag = " · HylaFAX" if m.get("busy") else ""
            label = f"{m['dev']} — {m.get('desc', '')}{vidpid}{tag}"
            self.cb_modem.addItem(label, m["dev"])
        sel = select_dev or cfgmod.get("modem") or ""
        idx = self.cb_modem.findData(sel)
        if idx >= 0:
            self.cb_modem.setCurrentIndex(idx)
        elif sel:
            self.cb_modem.insertItem(0, f"{sel} (сохранённый)", sel)
            self.cb_modem.setCurrentIndex(0)

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
        self.machine.voiceCall.connect(
            lambda ev: self.log_line(
                f"АППАРАТ: голосовой вызов от {ev.get('number') or 'неизвестен'}"
                + (" (пропущен)" if ev.get('kind') == 'voice-missed' else "")))

        # --- панель настроек: голосовые вызовы + путь сохранения по умолчанию
        from faxpdf import settings as cfgmod
        grp_v = QGroupBox("Голосовые вызовы")
        gv = QVBoxLayout(grp_v)
        self.chk_voice = QCheckBox("Отвечать на голосовые («трубка»)")
        self.chk_voice.setChecked(bool(cfgmod.get("voice_answer")))
        self.sp_rings = QSpinBox()
        self.sp_rings.setRange(1, 9)
        self.sp_rings.setValue(int(cfgmod.get("voice_rings") or 4))
        self.sp_rings.setSuffix(" кол.")
        def _apply_voice():
            cfgmod.set_voice(self.chk_voice.isChecked(), self.sp_rings.value())
            self.machine.set_voice_enabled(self.chk_voice.isChecked(),
                                           self.sp_rings.value())
        self.chk_voice.toggled.connect(_apply_voice)
        self.sp_rings.valueChanged.connect(_apply_voice)
        gv.addWidget(self.chk_voice)
        gv.addWidget(QLabel("Кольцев до ответа:"))
        gv.addWidget(self.sp_rings)
        gv.addWidget(QLabel(
            "Монитор голосовых вызовов: systemd-юнит <code>fax-pdf-voice</code> "
            "(voice-monitor.py)."))
        side = QVBoxLayout()
        side.addWidget(QLabel("<b>Факсимильный аппарат</b>"))
        side.addWidget(QLabel(
            "Наберите номер на клавиатуре и нажмите <b>TX▲</b> — выбор файлов "
            "(можно несколько) и отправка пачкой.<br>"
            "Входящие факсы принимаются автоматически: экран покажет RING → RX, "
            "готовый документ появится в лотке выдачи (клик — открыть).<br>"
            "<b>RxD</b> — папка «Входящие», <b>STOP</b> — прервать передачу/звонок, "
            "<b>HOLD</b> — пауза приёма, <b>VOICE</b> — снять/положить трубку.<br>"
            "Принятые файлы сохраняются в папку по умолчанию (/home/user); "
            "изменить — кнопка <b>SAVE…</b> на аппарате или поле ниже."))
        grp_m = QGroupBox("Оборудование — модем")
        gm = QVBoxLayout(grp_m)
        hm = QHBoxLayout()
        self.cb_modem = QComboBox()
        self.cb_modem.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        b_rescan = QPushButton("🔄 Скан")
        b_rescan.setToolTip("Автоопределение модемов ttyS*/ttyUSB*/ttyACM*")
        def _fill_modems(select_dev=None):    # noqa: E301 (метод класса ниже)
            return self._fill_modems(select_dev)
        def _modem_changed(dev):
            if not dev:
                return
            cfgmod.set_modem(dev)
            try:
                self.machine.selected_modem = dev
                self.machine._scan_modems(silent=True)
                self.machine.update()
            except Exception:
                pass
            self.lbl_modem.setText("модем: " + (hx.modem_status() or dev))
            self.log_line("Выбран модем: " + dev)
        def _rescan_modems():
            _fill_modems()
            self.log_line(f"Оборудование: найдено {self.cb_modem.count()} портов")
        b_rescan.clicked.connect(_rescan_modems)
        self.cb_modem.currentIndexChanged.connect(
            lambda i: _modem_changed(self.cb_modem.itemData(i)))
        hm.addWidget(self.cb_modem, 1)
        hm.addWidget(b_rescan)
        b_cfg = QPushButton("⚙ Настроить модем (faxaddmodem)")
        b_cfg.setToolTip("Автонастройка выбранного порта как модема HylaFAX "
                         "(нужны права root/sudo)")
        def _cfg_modem():
            dev = self.cb_modem.currentData()
            if not dev:
                QMessageBox.information(self, "Модем", "Сначала выберите устройство.")
                return
            port = os.path.basename(dev)
            r = hx.add_modem(dev)
            if r["ok"]:
                hx.restart_getty(port)
                QMessageBox.information(
                    self, "faxaddmodem",
                    f"Модем {port} настроен, faxgetty перезапущен.")
                _rescan_modems()
            else:
                QMessageBox.warning(
                    self, "faxaddmodem",
                    "Не удалось настроить:\n" + r["msg"] +
                    f"\n\nВручную: sudo faxaddmodem {port}")
        b_cfg.clicked.connect(_cfg_modem)
        gm.addLayout(hm)
        gm.addWidget(b_cfg)

        grp_d = QGroupBox("Папка для принятых факсов")
        gd = QHBoxLayout(grp_d)
        self.ed_incoming = QLineEdit(str(cfgmod.get("incoming_dir")))
        b_pick = QPushButton("Выбрать…")
        b_open = QPushButton("Открыть")
        def _pick_dir():
            d = QFileDialog.getExistingDirectory(
                self, "Куда сохранять принятые факсы", self.ed_incoming.text())
            if d:
                self.ed_incoming.setText(d)
                cfgmod.set_incoming_dir(d)
                self.log_line("Папка сохранения принятых факсов: " + d)
        def _open_dir():
            d = Path(self.ed_incoming.text() or str(Path.home()))
            d.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))
        b_pick.clicked.connect(_pick_dir)
        b_open.clicked.connect(_open_dir)
        gd.addWidget(self.ed_incoming, 1)
        gd.addWidget(b_pick)
        gd.addWidget(b_open)
        side.addWidget(grp_m)
        side.addWidget(grp_v)
        side.addWidget(grp_d)
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
        _notify(self.tray, "Входящий факс",
                f"От: {rx.sender_csid or 'неизвестно'}\n{rx.path.name}", "info")
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
        fn, _ = QFileDialog.getOpenFileNames(
            self, "Документы для отправки (можно выбрать несколько)",
            str(Path.home()), "Документы PDF/TIFF (*.pdf *.tif *.tiff)")
        if fn:
            self.ed_pdf.setText("; ".join(fn))

    def do_send(self):
        raw = self.ed_pdf.text().strip()
        pdfs = [p.strip() for p in raw.replace(",", ";").split(";") if p.strip()]
        pdfs = [p for p in pdfs if Path(p).exists()]
        num = self.ed_num.text().strip()
        if not pdfs:
            QMessageBox.warning(self, APP_TITLE,
                                "Укажите существующий PDF (или несколько через «;»).")
            return
        if not num:
            QMessageBox.warning(self, APP_TITLE, "Укажите номер получателя.")
            return
        self.btn_send.setEnabled(False)
        self.statusBar().showMessage(
            f"Конвертация {len(pdfs)} файл(ов) → TIFF G4 и постановка в очередь…")
        self._send_batch = pdfs
        self._send_idx = 0
        self._sw = SendWorker(pdfs[0], num, self.ed_cmt.text().strip(), self)
        self._sw.done.connect(self.on_sent)
        self._sw.fail.connect(self.on_send_fail)
        self._sw.start()

    def on_sent(self, out):
        # пакетная отправка: если в списке остались файлы — ставим следующий
        batch = getattr(self, "_send_batch", [])
        idx = getattr(self, "_send_idx", 0) + 1
        num = self.ed_num.text().strip()
        if batch and idx < len(batch):
            self._send_idx = idx
            self.statusBar().showMessage(
                f"Очередь {idx + 1}/{len(batch)}: {Path(batch[idx]).name}")
            self.log_line("ОТПРАВКА: " + out.replace("\n", " | "))
            self.refresh_jobs()
            self._sw = SendWorker(batch[idx], num,
                                  self.ed_cmt.text().strip(), self)
            self._sw.done.connect(self.on_sent)
            self._sw.fail.connect(self.on_send_fail)
            self._sw.start()
            return
        self._send_batch = []
        self._send_idx = 0
        self.btn_send.setEnabled(True)
        self.statusBar().showMessage("Отправлено в очередь")
        self.log_line("ОТПРАВКА: " + out.replace("\n", " | "))
        self.refresh_jobs()

    def on_send_fail(self, err):
        self._send_batch = []
        self._send_idx = 0
        self.btn_send.setEnabled(True)
        self.statusBar().showMessage("Ошибка отправки")
        self.log_line("ОШИБКА отправки: " + err)
        QMessageBox.critical(self, APP_TITLE, "Не удалось отправить:\n" + err)

    def refresh_jobs(self):
        jobs = hx.queue_jobs()
        # отслеживаем завершение задач: уведомляем, даже если окно свёрнуто в трей
        prev = {j.jobid: j.status for j in self._jobs_cache}
        cur = {j.jobid: j for j in jobs}
        for jid, st in prev.items():
            if jid not in cur and "отправ" not in st.lower():
                _notify(self.tray, "Отправка факса",
                        f"Задача {jid} покинула очередь (проверьте журнал).",
                        "warn")
        self._jobs_cache = jobs
        self.lst_jobs.clear()
        for j in jobs:
            it = QListWidgetItem(f"[{j.jobid}] → {j.number}  {j.info[:60]}")
            it.setData(Qt.UserRole, j.jobid)
            self.lst_jobs.addItem(it)
            old = prev.get(j.jobid, "")
            if old != j.status and "отправ" in j.status.lower():
                _notify(self.tray, "Факс отправлен",
                        f"[{j.jobid}] → {j.number}", "info")
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

    # ---- трей / фоновый режим --------------------------------------------
    def hide_to_tray(self, notify=True):
        """Свернуть окно в трей; мониторинг и приём продолжают работать."""
        if self.tray is None:
            self.log_line("Трей недоступен — окно не сворачивается.")
            return
        self.hide()
        if notify:
            self.tray.showMessage(
                APP_TITLE, "Факс-приёмник работает в фоне.\n"
                           "Двойной клик по значку — открыть окно.",
                QSystemTrayIcon.Information, 4000)

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible():
                self.hide_to_tray(notify=False)
            else:
                self.show_from_tray()

    def quit_app(self):
        """Полный выход из приложения (остановка фонового монитора)."""
        self._really_quit = True
        try:
            self.worker.stop()
            if self.worker.isRunning():
                self.worker.wait(2000)
        except Exception:
            pass
        if self.tray is not None:
            self.tray.hide()
        QApplication.instance().quit()

    # ---- «О программе» ----------------------------------------------------
    def show_about(self):
        box = QMessageBox(self)
        box.setWindowTitle("О программе")
        ic = _app_icon()
        if not ic.isNull():
            box.setIconPixmap(ic.pixmap(QSize(64, 64)))
        else:
            box.setIcon(QMessageBox.Information)
        box.setText(_about_text())
        box.setStandardButtons(QMessageBox.Ok)
        box.exec_()

    def closeEvent(self, ev):
        # закрытие окна (крестик) => сворачивание в трей, приложение живёт в фоне
        if self.tray is not None and not self._really_quit:
            ev.ignore()
            self.hide_to_tray()
            self.log_line("Окно свёрнуто в трей. Выход: правый клик по значку → «Выход».")
            return
        if not self._really_quit:
            self.worker.stop()
            self.worker.wait(3000)
        super().closeEvent(ev)


def _notify(tray, title, body, level="info", ms=8000):
    """Всплывающее уведомление: сначала через трей, затем fallback на notify-send."""
    if tray is not None:
        kind = {"info": QSystemTrayIcon.Information,
                "warn": QSystemTrayIcon.Warning,
                "err": QSystemTrayIcon.Critical}.get(level, QSystemTrayIcon.Information)
        tray.showMessage(title, body, kind, ms)
        return True
    for prog in ("notify-send", "zenity"):
        exe = shutil.which(prog)
        if not exe:
            continue
        try:
            if prog == "notify-send":
                subprocess.run([exe, "-t", str(ms * 1000), title, body],
                               timeout=5, capture_output=True)
            else:
                subprocess.run([exe, "--notification",
                                f"--text={title}: {body}"],
                               timeout=5, capture_output=True)
            return True
        except Exception:
            pass
    return False


def run(argv):
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(argv)
    app.setApplicationName("Факс-приёмник")
    app.setApplicationDisplayName(APP_TITLE)
    # не завершать приложение при закрытии последнего окна — живём в трее
    app.setQuitOnLastWindowClosed(False)
    if "--demo" in argv:
        # демонстрационный режим: факсимильный аппарат с имитацией
        # звонка/приёма/отправки — работает без HylaFAX и модема
        from faxpdf.faxwidget import DemoWindow
        w = DemoWindow()
    else:
        w = MainWindow()
    if "--minimized" in argv or os.environ.get("FAX_START_MINIMIZED") == "1":
        pass  # окно уже скрыто в конструкторе (hide_to_tray)
    else:
        w.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(run(sys.argv))
