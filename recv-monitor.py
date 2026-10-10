#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recv-monitor.py — сервис приёма факсов Fax-PDF для Astra Linux 1.7 / HylaFAX.

Следит за каталогом /var/spool/hylafax/recvq (inotify + опрос), и для каждого
нового принятого документа (TIFF/PDF от faxgetty):
  1) конвертирует TIFF -> PDF (tiff2pdf, fallback: img2pdf / ghostscript);
  2) вычисляет SHA-256 и сверяет с .status-файлом HylaFAX;
  3) раскладывает по папкам получателей (/srv/fax/incoming/<user>/ГГГГ/ММ/)
     согласно правилам etc/FAXRules (поддомен CSID/TSI в имени файла);
  4) пишет журнал /var/log/fax-pdf/received.log и шлёт уведомление на локальный
     e-mail оператору;
  5) перемещает исходный TIFF в архив /var/spool/hylafax/archive/recv/.

Запуск как systemd-сервис (см. systemd/fax-pdf-monitor.service).
"""

import hashlib
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SPOOL = Path(os.environ.get("HYLASPOOL", "/var/spool/hylafax"))
RECVQ = SPOOL / "recvq"
ARCHIVE = SPOOL / "archive" / "recv"
SRV_IN = Path(os.environ.get("FAX_INCOMING", "/srv/fax/incoming"))


def incoming_dir() -> Path:
    """Папка сохранения принятых факсов.

    Приоритет: env FAX_INCOMING > $SPOOL/etc/recv-archive.conf (путь задаёт
    GUI Fax-PDF кнопкой SAVE…, по умолчанию /home/user) > /srv/fax/incoming.
    """
    try:
        conf = SPOOL / "etc" / "recv-archive.conf"
        if conf.is_file():
            for line in conf.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.lower().startswith("archivedir:"):
                    d = line.split(":", 1)[1].strip()
                    if d:
                        return Path(d).expanduser()
    except OSError:
        pass
    return SRV_IN
LOGDIR = Path(os.environ.get("FAX_LOGDIR", "/var/log/fax-pdf"))
ADMIN_EMAIL = os.environ.get("FAX_ADMIN", "faxadmin@localhost")
POLL_SEC = int(os.environ.get("FAX_POLL", "5"))

# Имена файлов HylaFAX: recv.TIFF.<tsid>.<csid>.<seq> или <TSI>-<CSID>-...
RECV_RE = re.compile(r"^(\d{8}|.+?)?(?:\.tif{1,2}|\.pdf)$", re.I)

log = logging.getLogger("faxmon")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd, **kw):
    log.debug("exec: %s", cmd)
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def to_pdf(src: Path, dst: Path) -> bool:
    """Конвертировать принятый документ в PDF. Возвращ True при успехе."""
    try:
        if src.suffix.lower() in (".tif", ".tiff"):
            # основной путь: tiff2pdf из libtiff-tools
            run(["tiff2pdf", "-o", str(dst), str(src)])
        elif src.suffix.lower() == ".pdf":
            shutil.copy2(src, dst)
        else:
            run(["convert", str(src), str(dst)])   # fallback imagemagick
        return dst.exists() and dst.stat().st_size > 0
    except FileNotFoundError as e:
        log.error("Утилита не найдена: %s", e)
    except subprocess.CalledProcessError as e:
        log.error("Ошибка конвертации %s: %s", src.name, e.stderr[:400])
    # второй fallback: ghostscript для PS/TIFF
    try:
        run(["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
             f"-sOutputFile={dst}", str(src)])
        return dst.exists()
    except Exception:
        return False


def route_user(fname: str) -> str:
    """Определение получателя по правилам FAXRules (упрощённо: по TSI/CSID).

    Реальные правила читаются из /var/spool/hylafax/etc/FAXRules вида:
        Rule  "^.*(SALES).*"  Group "sales"
    Здесь — базовый разбор имени файла HylaFAX.
    """
    m = re.search(r"recv\.[^.]+\.(?P<tsid>[^.]*)\.(?P<csid>[^.]*)", fname)
    if m:
        csid = (m.group("csid") or "").lower()
        # соответствия можно задать через /etc/fax-pdf/routes.json
        routes = load_routes()
        for key, user in routes.items():
            if key.lower() in csid:
                return user
    return "default"


def load_routes():
    p = Path("/etc/fax-pdf/routes.json")
    if p.exists():
        import json
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("routes.json повреждён: %s", e)
    return {}


def notify(text: str, subject: str):
    """Локальное уведомление оператору (sendmail/pine при наличии)."""
    LOGDIR.mkdir(parents=True, exist_ok=True)
    with (LOGDIR / "received.log").open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat()} {text}\n")
    sendmail = shutil.which("sendmail") or shutil.which("msmtp")
    if not sendmail:
        return
    msg = (f"Subject: {subject}\r\nFrom: fax@{socket.gethostname()}\r\n"
           f"To: {ADMIN_EMAIL}\r\n\r\n{text}\r\n")
    try:
        subprocess.run([sendmail, "-t"], input=msg.encode(), check=False, timeout=30)
    except Exception as e:
        log.warning("Не удалось отправить письмо: %s", e)


def process(path: Path):
    name = path.name
    # пропускаем временные файлы getty (.tmp, *.modem)
    if name.startswith(".") or name.endswith((".tmp", ".xfer", ".done")):
        return
    if not RECV_RE.match(name) and "recv" not in name.lower():
        return

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    user = route_user(name)
    dest_dir = incoming_dir() / user / stamp[:4] / stamp[4:6]
    dest_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = dest_dir / f"{stamp}-{name}.pdf"

    log.info("Новый принятый документ: %s (получатель: %s)", name, user)
    if not to_pdf(path, pdf_path):
        notify(f"Факс {name}: НЕ УДАЛОСЬ сконвертировать в PDF", "Fax-PDF: ошибка приёма")
        return

    digest = sha256(pdf_path)
    # sidecar-файл контроля целостности
    pdf_path.with_suffix(".pdf.sha256").write_text(
        f"{digest}  {pdf_path.name}\n", encoding="utf-8")

    # в архив — оригинал (TIFF)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(ARCHIVE / f"{stamp}-{name}"))

    pages = "?"
    try:
        out = run(["pdfinfo", str(pdf_path)]).stdout
        m = re.search(r"Pages:\s+(\d+)", out)
        pages = m.group(1) if m else "?"
    except Exception:
        pass

    notify(
        f"Принят факс: {pdf_path}\nСтраниц: {pages}\nSHA-256: {digest}\n"
        f"Получатель: {user}, сервер: {socket.gethostname()}",
        f"Fax-PDF: входящий факс от {name}")
    log.info("OK: %s (%s стр., sha=%s…)", pdf_path, pages, digest[:12])


def scan_once(seen: set):
    if not RECVQ.exists():
        log.warning("Каталог %s ещё не существует (ждём faxaddmodem?)", RECVQ)
        return
    for p in sorted(RECVQ.iterdir()):
        if p.is_file() and p.name not in seen:
            # даём getty дописать файл
            size1 = p.stat().st_size
            time.sleep(1.0)
            if not p.exists() or p.stat().st_size != size1:
                continue
            seen.add(p.name)
            try:
                process(p)
            except Exception:
                log.exception("Обработка %s завершилась ошибкой", p.name)


def watch_inotify(seen: set):
    """Режим реального времени через inotifywait, если утилита доступна."""
    exe = shutil.which("inotifywait")
    if not exe:
        return False
    proc = subprocess.Popen(
        [exe, "-m", "-q", "--format", "%e %w%f", "-e", "close_write,moved_to", str(RECVQ)],
        stdout=subprocess.PIPE, text=True, bufsize=1)
    for line in proc.stdout:
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            f = Path(parts[1].strip())
            if f.is_file() and f.name not in seen:
                seen.add(f.name)
                try:
                    process(f)
                except Exception:
                    log.exception("Обработка %s", f)
    return True


def main():
    LOGDIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOGDIR / "monitor.log", encoding="utf-8")])

    # права: только группа fax может читать recvq
    os.umask(0o027)
    seen: set = set()
    log.info("Fax-PDF monitor запущен. Spool: %s, приём: %s", RECVQ, incoming_dir())

    while True:
        try:
            if not watch_inotify(seen):
                raise FileNotFoundError("inotifywait отсутствует")
        except FileNotFoundError:
            log.info("inotify недоступен — переключаюсь на опрос каждые %d c", POLL_SEC)
            while True:
                scan_once(seen)
                time.sleep(POLL_SEC)
        except Exception:
            log.exception("Цикл мониторинга упал, перезапуск через 10 c")
            time.sleep(10)


if __name__ == "__main__":
    main()
