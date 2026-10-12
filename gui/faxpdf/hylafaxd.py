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

# ------------------------------------------------------- голосовые вызовы ---
# HylaFAX 7.x: при звонке без CNG-сигнала факса faxgetty пишет в syslog строку
#   "UNANSWERED VOICE CALL from <CSID>" (или "+RING" для модемов без CID).
# Отдельный «голосовой монитор» (systemd-юнит voice-monitor) читает syslog и
# складывает события сюда; GUI опрашивает этот файл как очередь пропущенных.
VOICE_QUEUE = Path(os.environ.get(
    "FAX_VOICE_QUEUE", str(Path.home() / ".fax-pdf" / "voice_calls.jsonl")))


def read_voice_calls(limit: int = 50) -> List[dict]:
    """Последние N голосовых/пропущенных вызовов из очереди."""
    try:
        lines = VOICE_QUEUE.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:
        return []
    out = []
    for ln in reversed(lines):
        try:
            import json as _json
            rec = _json.loads(ln)
            if isinstance(rec, dict):
                out.append(rec)
        except Exception:
            continue
    return out


def apply_voice_settings(answer: bool, rings: int,
                        config_path: Optional[Path] = None) -> bool:
    """Записать параметры ответа на голосовые вызовы в etc/config.tty* HylaFAX.

    Ключи HylaFAX: RingsVoiceAnswer (число колец; 0 — не отвечать голосом),
    VoiceCallModem / NonFaxSession — поведение после ответа. Возвращает True,
    если хотя бы один конфиг обновлён (иначе — права/отсутствие spool)."""
    import glob as _glob
    targets = [config_path] if config_path else \
        [Path(p) for p in _glob.glob(str(SPOOL / "etc" / "config.tty*"))]
    if not targets:
        targets = [SPOOL / "etc" / "config"]      # глобальный fallback
    changed = False
    val = str(max(1, min(9, int(rings)))) if answer else "0"
    for t in targets:
        if not t.is_file():
            continue
        try:
            text = t.read_text(encoding="utf-8", errors="ignore")
            new_lines = []
            done = False
            for line in text.splitlines():
                if re.match(r"\s*RingsVoiceAnswer\s*:", line):
                    new_lines.append(f"RingsVoiceAnswer:   {val}")
                    done = True
                else:
                    new_lines.append(line)
            if not done:
                new_lines.append(f"RingsVoiceAnswer:   {val}")
            tmp = t.with_suffix(t.suffix + ".new")
            tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            os.replace(tmp, t)
            changed = True
        except OSError:
            continue  # нет прав — молча пропускаем (GUI подскажет оператору)
    return changed


# ------------------------------------------------------------- модемы ---

# Поддерживаемые win-модемы Conexant HSF (Linuxant/Intel "hsfmodem", в Astra —
# пакет hsfmodem / DGCPCI56).  У таких модемов нет полноценного AT-порта для
# faxgetty; факс и голос обслуживает драйвер через виртуальный последовательный
# порт /dev/ttySHSF* (эмуляция Class 2.0).  Для голоса дополнительно нужен
# ALSA-конфиг voip/ccmux (см. setup_vpmcall).
HSF_DRIVERS = ("hsf", "dgcpci56", "lt_modem", "winmodem")
HSF_VENDOR_IDS = {   # USB idVendor:idProduct известных HSF/win-модемов
    "14f1": "Conexant (HSF)",       # Conexant Systems (Rockwell) — USB SoftModem
    "16ec": "USRobotics (HSF)",
    "0a7c": "OSS/Orient (HSF)",
}


def is_hsf_device(name: str, drv: str = "", desc: str = "") -> bool:
    """True, если устройство похоже на модем HSF (ttySHSF*/драйвер hsf)."""
    s = f"{name} {drv} {desc}".lower()
    return any(k in s for k in HSF_DRIVERS) or "shsf" in s


def hsf_status() -> dict:
    """Состояние поддержки HSF в системе: драйвер, модуль, утилиты vpm.

    Возвращает {'driver_loaded': bool, 'vpm_available': bool,
               'vpmcall': bool, 'ports': [ttySHSF*], 'notes': str}.
    """
    res = {"driver_loaded": False, "vpm_available": False,
           "vpmcall": False, "ports": [], "notes": ""}
    try:  # модуль ядра hsf (или dgcpci56_v80 для PCI-варианта Conexant)
        out = subprocess.run(["lsmod"], capture_output=True, text=True,
                             timeout=10).stdout.lower()
        res["driver_loaded"] = any(m in out for m in ("hsf", "dgcpci56"))
    except Exception:
        pass
    if Path("/dev/vpmctl").exists():
        res["vpm_available"] = True
    for u in ("vpmcall", "vpmdrop", "vpmstat"):
        if shutil.which(u):
            res["vpmcall"] = True
            break
    import glob as _glob
    for p in sorted(_glob.glob("/dev/ttySHSF*")):
        res["ports"].append(os.path.basename(p))
    if not res["driver_loaded"]:
        res["notes"] = ("Модуль HSF не загружен. Установите пакет hsfmodem "
                        "(Astra 1.7: репозиторий common/extra) или соберите "
                        "драйвер Linuxant; после установки — «modprobe hsf».")
    elif not res["vpm_available"]:
        res["notes"] = ("/dev/vpmctl отсутствует: загрузите модули "
                        "hsf/vpmdrv, либо включите Voice-over-IP в /etc/hsfconf.")
    elif not res["ports"]:
        res["notes"] = "Модем виден драйвером, но /dev/ttySHSF* нет: «hsftest», переподключите."
    else:
        res["notes"] = "HSF-модем готов: факс через ttySHSF*, голос через vpmcall."
    return res


def setup_vpmcall(enable: bool = True) -> dict:
    """Включить/выключить маршрутизацию голоса HSF (ccmux/VoIP) через конфиг
    /etc/hsfconf.d/99-fax-pdf.conf.  Требует root.  После изменения нужно
    перезагрузить драйвер (rmmod/modprobe hsf) или перезагрузить ПК."""
    conf_dir = Path("/etc/hsfconf.d")
    conf = conf_dir / "99-fax-pdf.conf"
    if enable:
        body = ("\n# Fax-PDF: голосовые вызовы на HSF-модеме\n"
                "CCMUX_ENABLED=1\n"
                "VOICE_OVER_IP=1\n"
                "VPM_AUDIO_MODE=1\n")
        try:
            conf_dir.mkdir(parents=True, exist_ok=True)
            old = conf.read_text(encoding="utf-8") if conf.is_file() else ""
            if "CCMUX_ENABLED=1" not in old:
                conf.write_text(old + body, encoding="utf-8")
            return {"ok": True,
                    "msg": "Голос HSF включён (ccmux/VoIP). Перезагрузите "
                           "модуль: sudo rmmod hsf && sudo modprobe hsf"}
        except OSError as e:
            return {"ok": False, "msg": f"нет прав на {conf}: {e}"}
    try:
        if conf.is_file():
            conf.unlink()
        return {"ok": True, "msg": "Конфиг голоса HSF удалён, голос выключен"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


def dial_voice_hsf(number: str, seconds: int = 30) -> dict:
    """Позвонить голосом с HSF-модема через vpmcall (off-hook → DTMF).

    Требуется, чтобы faxgetty НЕ держал модем в этот момент (порт освобождается
    автоматически при простое).  Возвращает {'ok', 'msg'}.
    """
    if not shutil.which("vpmcall"):
        return {"ok": False,
                "msg": "vpmcall не найден — установите пакет hsfmodem"}
    cmd = ["vpmcall", "-t", str(seconds), "--", number]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=seconds + 20)
        ok = r.returncode == 0
        return {"ok": ok, "msg": ((r.stdout or "") + (r.stderr or ""))[-400:]
                .strip() or ("вызов завершён" if ok else
                            f"код возврата {r.returncode}")}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "msg": str(e)}


def _udev_prop(devpath: str, prop: str) -> str:
    """Свойство udev (/sys/.../uevent) без pyserial/pyudev — только чтение файлов."""
    try:
        with open(os.path.join(devpath, "uevent"), encoding="utf-8",
                  errors="ignore") as fh:
            for line in fh:
                if line.startswith(prop + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def list_modems() -> List[dict]:
    """Автоопределение модемов (ttyS*/ttyUSB*/ttyACM*): путь устройства,
    производитель/модель по sysfs/udev, занят ли он faxgetty.

    Возвращает список словарей: dev, real, desc, vendor, product, busy.
    Никаких внешних зависимостей; работает и без прав root (busy тогда
    определяется только по syslog-подсказкам)."""
    import glob as _glob
    found = []
    for pat in ("/dev/ttyS*", "/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttySHSF*"):
        for dev in sorted(_glob.glob(pat)):
            try:
                rp = os.path.realpath(dev)
            except OSError:
                continue
            if not os.path.exists(rp):
                continue
            entry = {"dev": dev, "real": rp, "desc": "",
                     "vendor": "", "product": "", "busy": False,
                     "hsf": False}
            # имя tty-устройства (ttyS0/ttyUSB0/ttyACM0) для поиска в sysfs
            name = os.path.basename(rp)
            # 1) sysfs-драйвер модема: /sys/class/tty/<name>/device/driver
            drv = ""
            try:
                drv = os.path.basename(
                    os.readlink(f"/sys/class/tty/{name}/device/driver"))
            except OSError:
                pass
            if drv == "serial8250":
                drv = ""          # встроенный COM-порт — не «USB»
            # 2) USB-модемы: idVendor/idProduct из uevent + /dev/bus/usb/*
            if not drv and ("ttyUSB" in name or "ttyACM" in name):
                ent = _udev_prop(rp, "DEVPATH")   # .../usbX/X-1:1.0/ttyUSBn
                mnum = re.search(r"(\d+-[\d.]+)", ent)
                vid = pid = ""
                usbdir = None
                if mnum:
                    usbdir = f"/sys/bus/usb/devices/{mnum.group(1)}"
                    def _rf(p):
                        try:
                            return Path(p).read_text().strip()
                        except OSError:
                            return ""
                    vid = _rf(os.path.join(usbdir, "idVendor"))
                    pid = _rf(os.path.join(usbdir, "idProduct"))
                    vend = _rf(os.path.join(usbdir, "manufacturer"))
                    prod = _rf(os.path.join(usbdir, "product"))
                    if vend or prod:
                        entry["vendor"], entry["product"] = vend, prod
                        entry["desc"] = " ".join(x for x in (vend, prod) if x)
                if not entry["desc"]:
                    # fallback: lsusb -z (NULL-terminated) по bus/dev из DEVPATH
                    try:
                        bm = re.search(r"/usb(\d)/(\d+)", ent)
                        if bm and shutil.which("lsusb"):
                            out = subprocess.run(["lsusb", "-z"], capture_output=True,
                                                 text=True, timeout=10).stdout
                            rec = "\x00".join([f"Bus 0{bm.group(1)}",
                                                f"Device {int(bm.group(2)):03d}"])
                            for blk in out.split("\x00\x00"):
                                if rec in blk.replace("\x00", "\x00"):
                                    fields = [f for f in blk.split("\x00") if f]
                                    # формат: Bus.. Device.. ID vvvv:pppp Vendor Product
                                    for i, f in enumerate(fields):
                                        if f.startswith("ID ") and i + 2 < len(fields):
                                            vp = f[3:].split(":")
                                            if len(vp) == 2:
                                                entry["vendor"] = fields[i + 1]
                                                entry["product"] = fields[i + 2]
                                                entry["desc"] = " ".join(
                                                    fields[i + 1:i + 3])
                                                entry["vendor_id"] = vp[0]
                                                entry["product_id"] = vp[1]
                                            break
                    except Exception:
                        pass
                if vid and pid and not entry.get("vendor_id"):
                    entry["vendor_id"], entry["product_id"] = vid, pid
            # 3) описание по драйверу
            if name.startswith("ttySHSF") or is_hsf_device(name, drv):
                entry["hsf"] = True
                entry["desc"] = "Conexant HSF winmodem (эмуляция Class 2.0)"
                entry["vendor"], entry["product"] = "Conexant", "HSF"
            elif not entry["desc"]:
                if drv:
                    entry["desc"] = f"USB serial ({drv})"
                    entry["vendor"], entry["product"] = drv, ""
                elif name.startswith("ttyS"):
                    entry["desc"] = "Встроенный последовательный порт (COM)"
                    entry["vendor"], entry["product"] = "onboard", name[4:]
            else:
                entry.setdefault("vendor", "USB")
            # 4) занятость: настроен ли модем в HylaFAX (etc/config.<port>)
            #    или активный getty — проверяем по выводу faxstat -s.
            port = name
            binf = hylafax_bin("faxstat")
            if binf:
                try:
                    out = subprocess.run([binf, "-s"], capture_output=True,
                                         text=True, timeout=10).stdout
                    low = out.lower()
                    if port.lower() in low and ("active" in low or "ready" in low
                                                or "down" in low):
                        entry["busy"] = True
                except Exception:
                    pass
            found.append(entry)
    return found


def modem_for_device(dev: str) -> Optional[str]:
    """Имя модема HylaFAX (ttyXXX) для пути /dev/ttyXXX."""
    return os.path.basename(dev) if dev else None


def add_modem(dev: str, country: str = "RU", speakerphone: bool = False) -> dict:
    """Автонастройка модема через faxaddmodem (аналог интерактива:
    CountryCode + ответ на «prepare the device»). Требует root/sudo.

    Возвращает {'ok': bool, 'msg': str}."""
    binf = hylafax_bin("faxaddmodem")
    if not binf:
        return {"ok": False, "msg": "faxaddmodem не найден (не установлен HylaFAX?)"}
    port = modem_for_device(dev)
    cmd = [binf, "-c", country, "-y" if speakerphone else "-n", port]
    try:
        res = subprocess.run(cmd, input=f"y\n{port}\ny\n", capture_output=True,
                             text=True, timeout=180)
        ok = res.returncode == 0
        msg = (res.stdout or "")[-500:] + ("\n" + res.stderr[-300:] if res.stderr else "")
        return {"ok": ok, "msg": msg.strip() or
                ("модем настроен" if ok else f"код возврата {res.returncode}")}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "msg": str(e)}


def restart_getty(port: str) -> dict:
    """Перезапустить getty HylaFAX на порту (systemd-юнит или kill faxgetty)."""
    unit = f"hylafax-getty@{port}.service"
    try:
        r = subprocess.run(["systemctl", "restart", unit], capture_output=True,
                           text=True, timeout=60)
        if r.returncode == 0:
            return {"ok": True, "msg": f"{unit}: перезапущен"}
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:  # fallback: убить faxgetty — faxq перезапустит его сам
        out = subprocess.run(["pkill", "-f", f"faxgetty.*{port}"],
                             capture_output=True, text=True, timeout=10)
        return {"ok": out.returncode == 0,
                "msg": "faxgetty остановлен, faxq перезапустит"
                if out.returncode == 0 else "процесс не найден"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


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
    """Готовый принятый факс: положить в dest_dir (конвертировать TIFF->PDF при необходимости)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if rx.is_pdf:
        dst = dest_dir / rx.path.name
        if rx.path.resolve() != dst.resolve():
            shutil.copy2(rx.path, dst)
        return dst
    # если файл уже лежит в dest_dir (например, монитор положил PDF рядом с TIFF),
    # ищем парный PDF с тем же stem — не конвертируем повторно
    twin = dest_dir / (rx.path.stem + ".pdf")
    if twin.exists():
        return twin
    return tiff_to_pdf(rx.path, dest_dir)
