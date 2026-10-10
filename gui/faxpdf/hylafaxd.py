#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
faxpdf/hylafaxd.py — слой взаимодействия GUI с HylaFAX.

Функции:
  * разбор вывода faxstat (очередь отправки, активные вызовы, модем);
  * постановка задачи отправки PDF (через sendfax / утилиту пакета fax-pdf-send);
  * мониторинг каталога recvq: новые принятые факсы (TIFF/PDF) и файлы
    .done/.status — для live-отображения страниц при приёме;
  * конвертация TIFF -> PDF (tiff2pdf, fallback ghostscript/pnm2pdf).
Всё через subprocess, без жёсткой зависимости от PyQt.
"""

import os
import re
import glob
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

SPOOL = Path(os.environ.get("HYLASPOOL", "/var/spool/hylafax"))
RECVQ = SPOOL / "recvq"
DONEQ = SPOOL / "doneq"
ARCHIVE_RECV = SPOOL / "archive" / "recv"


@dataclass
class Job:
    """Задача отправки из очереди HylaFAX."""
    jobid: str
    number: str = ""
    status: str = ""          # «Отправлен», «В процессе», «Ожидает» ...
    pages: str = ""
    info: str = ""


@dataclass
class ReceivedFax:
    """Принятый факс в recvq/doneq."""
    path: Path
    sender_csid: str = ""
    sender_tsi: str = ""
    pages: int = 0
    is_pdf: bool = False
    done: bool = False        # приём завершён (.done существует)

    @property
    def key(self) -> str:
        return self.path.name


def run(cmd: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def hylafax_bin(name: str) -> Optional[str]:
    """Ищем бинарник HylaFAX: сначала в PATH, затем в /opt/hylafax/bin|sbin."""
    p = shutil.which(name)
    if p:
        return p
    for d in ("/opt/hylafax/bin", "/opt/hylafax/sbin", "/usr/sbin", "/usr/bin"):
        cand = Path(d) / name
        if cand.exists():
            return str(cand)
    return None


# ---------------------------------------------------------------- faxstat ---

_FAXSTAT_ROW = re.compile(r"^([1-9]\d*)\s+(\S+)\s+(.*?)\s*$")


def parse_jobs(faxstat_out: str) -> List[Job]:
    """Разбираем секцию 'Jobs' вывода faxstat -a (или -i)."""
    jobs: List[Job] = []
    in_jobs = False
    for line in faxstat_out.splitlines():
        s = line.strip()
        if s.lower().startswith("jobs") or s.startswith("JID"):
            in_jobs = True
            continue
        if not in_jobs or not s:
            continue
        m = _FAXSTAT_ROW.match(s)
        if m:
            jobid, number, rest = m.groups()
            jobs.append(Job(jobid=jobid, number=number, info=rest))
        elif s.startswith("Done") or s.lower().startswith("sent"):
            break
    return jobs


def queue_jobs(remote: bool = True) -> List[Job]:
    binf = hylafax_bin("faxstat")
    if not binf:
        return []
    cmd = [binf, "-a"] if remote else [binf, "-a", "-l"]
    try:
        res = run(cmd, timeout=20)
    except Exception:
        return []
    return parse_jobs(res.stdout)


def modem_status() -> str:
    """Краткое состояние первого модема по faxstat -s."""
    binf = hylafax_bin("faxstat")
    if not binf:
        return "HylaFAX не найден"
    try:
        res = run([binf, "-s"], timeout=20)
    except Exception:
        return "faxstat недоступен"
    for line in res.stdout.splitlines():
        if line.strip().startswith("tty") or "Modem" in line:
            parts = line.split()
            if len(parts) >= 4:
                return f"{parts[0]}: {parts[3]}"
    return "нет данных"


# ------------------------------------------------------------- отправка ---

def send_pdf(pdf_path: str, number: str, comment: str = "") -> str:
    """Поставить PDF в очередь отправки. Возвращает текст результата/id задачи."""
    # 1) предпочтительно — обёртка пакета (PDF->TIFF G4 + sendfax)
    wrapper = hylafax_bin("fax-pdf-send") or (
        str(Path(__file__).resolve().parents[2] / "sendfax.sh")
        if Path(__file__).resolve().parents[2].joinpath("sendfax.sh").exists() else None
    )
    if wrapper and wrapper.endswith(("fax-pdf-send", "sendfax.sh")):
        cmd = [wrapper, pdf_path, number]
        res = run(cmd, timeout=300)
        out = (res.stdout + res.stderr).strip()
        if res.returncode != 0:
            raise RuntimeError(out or f"Код возврата {res.returncode}")
        return out
    # 2) напрямую: конвертируем и отправляем sendfax
    tiff = pdf_to_tiff_g4(pdf_path)
    binf = hylafax_bin("sendfax")
    if not binf:
        raise RuntimeError("sendfax не найден в PATH или /opt/hylafax/bin")
    cmd = [binf, "-n", "-m", comment or "fax-pdf GUI", number, tiff]
    res = run(cmd, timeout=120)
    out = (res.stdout + res.stderr).strip()
    if res.returncode != 0:
        raise RuntimeError(out or f"Код возврата {res.returncode}")
    return out


def cancel_job(jobid: str) -> str:
    binf = hylafax_bin("sendfax")
    if not binf:
        raise RuntimeError("sendfax не найден")
    res = run([binf, "-X", jobid], timeout=30)
    return (res.stdout + res.stderr).strip()


# ------------------------------------------------------------ приём/live ---

_CSID_RE = re.compile(r"recv\.(?:TIFF|PDF)\.[^./]*\.?([^./]*)\.?\d+")


def list_incoming() -> List[ReceivedFax]:
    """Факсы в recvq (принимается сейчас/не разобраны) и свежие в архиве."""
    items: List[ReceivedFax] = []
    for directory, done in ((RECVQ, False), (DONEQ, False)):
        if not directory.is_dir():
            continue
        for f in sorted(directory.iterdir()):
            if f.name.startswith("."):
                continue
            suf = f.suffix.lower()
            # HylaFAX складывает принимаемые файлы как recv.TIFF.<tsid>.<csid>.NNN
            # (без расширения .tif) либо *.tif/*.tiff/*.pdf
            known = suf in (".tif", ".tiff", ".pdf") or \
                f.name.startswith(("recv.TIFF", "recv.PDF"))
            if not known:
                continue
            rf = _parse_recv_name(f)
            rf.done = Path(str(f) + ".done").exists() or directory == DONEQ
            rf.is_pdf = f.suffix.lower() == ".pdf"
            items.append(rf)
    return items


def _parse_recv_name(p: Path) -> ReceivedFax:
    parts = p.stem.split(".")
    csid = tsi = ""
    # recv.TIFF.<tsid>.<csid>.NNN  либо <TSI>-<CSID>-...
    if len(parts) >= 4 and parts[0] == "recv":
        tsi, csid = parts[2], parts[3]
    elif "-" in p.stem:
        seg = p.stem.split("-")
        if len(seg) >= 2:
            tsi, csid = seg[0], seg[1]
    return ReceivedFax(path=p, sender_csid=csid, sender_tsi=tsi)


def count_pages(path: Path) -> int:
    """Число страниц TIFF/PDF (для live-индикатора приёма)."""
    suf = path.suffix.lower()
    is_pdf = suf == ".pdf" or path.name.startswith("recv.PDF")
    if is_pdf:
        inf = hylafax_bin("pdfinfo")
        if inf:
            try:
                res = run([inf, str(path)], timeout=15)
                for line in res.stdout.splitlines():
                    if line.lower().startswith("pages:"):
                        return int(line.split(":")[1].strip())
            except Exception:
                pass
        return 0
    info = hylafax_bin("tiffinfo")
    if info:
        try:
            res = run([info, str(path)], timeout=15)
            n = res.stdout.count("Image Width")
            if n:
                return n
        except Exception:
            pass
    # fallback: быстрый разбор заголовков TIFF (II/MM) без внешних утилит
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
            if head[:2] not in (b"II", b"MM"):
                return 0
            endian = "<" if head[:2] == b"II" else ">"
            off = int.from_bytes(fh.read(4), endian)
            n = 0
            while off:
                fh.seek(off)
                cnt_b = fh.read(2)
                if len(cnt_b) < 2:
                    break
                cnt = int.from_bytes(cnt_b, endian)
                if cnt == 0 or cnt > 10000:
                    break
                n += 1
                body = fh.read(12 * cnt + 4)
                if len(body) < 4:
                    break
                nxt = int.from_bytes(body[12 * cnt:12 * cnt + 4], endian)
                off = nxt if nxt else 0
        return max(1, n) if n else 0
    except Exception:
        return 0


def tiff_to_pdf(tiff_path: Path, out_dir: Path) -> Optional[Path]:
    """Конвертация принятого TIFF в PDF. tiff2pdf -> gs fallback."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (tiff_path.stem + ".pdf")
    if out.exists():
        return out
    t2p = hylafax_bin("tiff2pdf")
    if t2p:
        res = run([t2p, "-o", str(out), str(tiff_path)], timeout=120)
        if res.returncode == 0 and out.stat().st_size > 0:
            return out
    gs = hylafax_bin("gs")
    if gs:
        res = run([gs, "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                   f"-sOutputFile={out}", str(tiff_path)], timeout=180)
        if res.returncode == 0 and out.exists():
            return out
    return None


def pdf_to_tiff_g4(pdf_path: str) -> str:
    """PDF -> многостраничный TIFF Group4 200x200 dpi (для HylaFAX)."""
    import tempfile
    src = Path(pdf_path)
    if not src.exists():
        raise RuntimeError(f"Файл не найден: {pdf_path}")
    outdir = Path(tempfile.mkdtemp(prefix="faxpdf-"))
    tiff = outdir / (src.stem + ".tif")
    pdftoppm = hylafax_bin("pdftoppm")
    img2tiff = hylafax_bin("img2tiff") or shutil.which("convert")
    if pdftoppm and img2tiff:
        base = outdir / "pg"
        res = run([pdftoppm, "-r", "200", "-mono", "-tiff", str(src), str(base)],
                  timeout=300)
        pages = sorted(outdir.glob("pg*.tif*"))
        if res.returncode == 0 and pages:
            if len(pages) == 1:
                shutil.copy2(pages[0], tiff)
                return str(tiff)
            if img2tiff.endswith("convert"):  # ImageMagick
                cmd = [img2tiff] + [str(p) for p in pages] + \
                      ["-compress", "ccitt", "-density", "200", str(tiff)]
            else:                              # netpbm img2tiff
                cmd = [img2tiff, "-C", "g4"] + [str(p) for p in pages] + [str(tiff)]
            res2 = run(cmd, timeout=300)
            if res2.returncode == 0 and tiff.exists():
                return str(tiff)
    gs = hylafax_bin("gs")
    if gs:
        res = run([gs, "-dBATCH", "-dNOPAUSE", "-sDEVICE=tiffg4", "-r200",
                   f"-sOutputFile={tiff}", str(src)], timeout=300)
        if res.returncode == 0 and tiff.exists():
            return str(tiff)
    raise RuntimeError("Не удалось конвертировать PDF в TIFF (нужны pdftoppm+img2tiff или ghostscript)")


def archive_received(rx: ReceivedFax, dest_dir: Path) -> Optional[Path]:
    """Готовый принятый факс: конвертировать в PDF и положить в dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if rx.is_pdf:
        dst = dest_dir / rx.path.name
        shutil.copy2(rx.path, dst)
        return dst
    return tiff_to_pdf(rx.path, dest_dir)
