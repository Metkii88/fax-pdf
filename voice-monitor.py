#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
voice-monitor.py — монитор голосовых вызовов Fax-PDF (Astra 1.7 / HylaFAX).

HylaFAX сам НЕ поднимает «трубку» на голосовой звонок, но фиксирует его:
faxgetty пишет в syslog строки вида
    faxgetty[123]: /dev/ttyS0: UNANSWERED VOICE CALL from 74951234567
    faxgetty[123]: /dev/ttyS0: +RING 74951234567        (CID)
    faxgetty[123]: /dev/ttyS0: RING                      (без CID)

Скрипт читает syslog-файл (journalctl -> fallback /var/log/fax-sessions.log),
разбирает события и складывает JSONL-записи в очередь (~/.fax-pdf/voice_calls.jsonl
или $FAX_VOICE_QUEUE), из которой GUI показывает список пропущенных вызовов.

Если включён режим ответа (FAX_VOICE_ANSWER=1, по умолчанию), после N колец
скрипт «поднимает трубку»: переводит модем в офф-хук и включает громкую связь
через AT-команды (/dev/ttyACM*/ttyUSB* не трогаем — только тот порт, что
указан в FAX_MODEM_TTY). Для реальной голосовой связи нужен модем с поддержкой
голоса (Class 1.0 voice / speaker); без неё вызов просто отмечается принятым.

Запуск как systemd-сервис: systemd/fax-pdf-voice.service
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

SPOOL = Path(os.environ.get("HYLASPOOL", "/var/spool/hylafax"))
QUEUE = Path(os.environ.get(
    "FAX_VOICE_QUEUE", str(Path.home() / ".fax-pdf" / "voice_calls.jsonl")))
MODEM_TTY = os.environ.get("FAX_MODEM_TTY", "")          # напр. /dev/ttyS0
ANSWER = os.environ.get("FAX_VOICE_ANSWER", "1") not in ("0", "false", "no")
RINGS_BEFORE_ANSWER = int(os.environ.get("FAX_VOICE_RINGS", "4"))
LOG_SOURCES = [p for p in [
    os.environ.get("FAX_SYSLOG", "/var/log/syslog"),
    "/var/log/messages",
    SPOOL / "fifo" / "session.log",
] if Path(p).exists()]
POLL_SEC = int(os.environ.get("FAX_POLL", "2"))

RE_VOICE = re.compile(r"(UNANSWERED VOICE CALL|VOICE CALL)\s+(?:from\s+)?([\d+*#]+)?", re.I)
RE_RING_CID = re.compile(r"\+RING[,:\s]*(?:\"?([^\"\r\n]+?)\"?[,;]?|)(\d{5,})?", re.I)
RE_RING = re.compile(r"\bRING\b")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def push_event(rec: dict) -> None:
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"СОБЫТИЕ: {rec}")


def is_hsf_tty(dev: str) -> bool:
    """ttySHSF* — порт win-модема Conexant HSF: AT-команды туда не проходят,
    линия поднимается утилитами драйвера vpmcall/vpmdrop (alsa-utils-hsf)."""
    return "shsf" in (dev or "").lower()


def answer_call_hsf(hangup_after: int = 0) -> bool:
    """Поднять линию на HSF-модеме через vpmcall (off-hook без номера).

    hangup_after>0 — через N секунд повесить трубку (vpmdrop).
    Возвращает True при успехе; если vpmcall недоступна — False.
    """
    import shutil as _sh
    import subprocess as _sp
    if not _sh.which("vpmcall"):
        log("HSF: vpmcall не найден — установите пакет hsfmodem (Astra) или Linuxant")
        return False
    try:
        args = ["vpmcall", "-t", str(max(hangup_after, 1) or 30)] if hangup_after else ["vpmcall"]
        _sp.run(args, timeout=15, capture_output=True)
        if hangup_after and _sh.which("vpmdrop"):
            threading.Timer(hangup_after, lambda: _sp.run(["vpmdrop"], timeout=10,
                                                           capture_output=True)).start()
        return True
    except Exception as e:
        log(f"HSF: vpmcall ошибка: {e}")
        return False


def answer_call(tty: str) -> bool:
    """best-effort: поднять трубку (off-hook) и включить динамик модема.

    Для HSF/win-модемов (ttySHSF*) используется vpmcall драйвера hsfmodem —
    AT-команды таким устройствам недоступны напрямую.
    """
    dev = tty or MODEM_TTY
    if is_hsf_tty(dev):
        return answer_call_hsf()
    if not dev or not Path(dev).exists():
        return False
    try:
        # через hylafax-утилиту faxrunq-подобного механизма нет; шлём AT напрямую
        import serial  # pyserial опционален
        s = serial.Serial(dev, 115200, timeout=1)
        s.write(b"ATH1\r")   # off-hook / include fax line to DTE
        s.write(b"ATIP=1\r") # speaker on (Voice features; зависит от модема)
        time.sleep(0.2)
        s.close()
        return True
    except Exception:
        # fallback: echo в порт (может не сработать на занятом getty порту)
        try:
            with open(dev, "wb", buffering=0) as f:
                f.write(b"ATH1\r")
            return True
        except Exception:
            return False


def tail_file(path: Path, pos: int):
    """Вернуть новые строки файла (обработка ротации)."""
    try:
        st = path.stat()
        if st.st_size < pos:            # ротация журнала
            pos = 0
        with path.open("r", errors="ignore") as f:
            f.seek(pos)
            data = f.read()
            newpos = f.tell()
        return data.splitlines(), newpos
    except Exception:
        return [], pos


def parse_lines(lines):
    events = []
    for ln in lines:
        if "faxgetty" not in ln and "hylafax" not in ln.lower():
            continue
        m = RE_VOICE.search(ln)
        num = ""
        kind = ""
        if m:
            num = m.group(2) or ""
            kind = "voice_missed" if "UNANSWERED" in ln.upper() else "voice"
        else:
            m2 = RE_RING_CID.search(ln)
            if m2 and (m2.group(1) or m2.group(2)):
                num = (m2.group(2) or "").strip()
                kind = "ring"
            elif RE_RING.search(ln):
                kind = "ring"
        if kind:
            ts = ln[:15]
            events.append({"ts": ts, "kind": kind, "number": num,
                           "raw": ln.strip()[:300],
                           "seen_at": datetime.now().isoformat(timespec="seconds")})
    return events


def journal_follow() -> iter:
    """Стрим строк из journald (если systemd доступен)."""
    cmd = ["journalctl", "-f", "-n", "0", "--output=short-iso",
           "-t", "faxgetty", "-t", "hyla"]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
        return p
    except Exception:
        return None


def main() -> int:
    log(f"voice-monitor: очередь={QUEUE} ответ={'вкл' if ANSWER else 'выкл'} "
        f"(порог {RINGS_BEFORE_ANSWER} звонков)")
    ring_counts = {}     # number -> consecutive rings without connect

    sources = LOG_SOURCES
    jproc = None
    if not sources:
        jproc = journal_follow()
        if jproc is None:
            log("Журналы недоступны (ни syslog-файла, ни journalctl). Завершение.")
            return 1

    positions = {Path(p): Path(p).stat().st_size for p in sources} if sources else {}

    while True:
        lines = []
        if jproc is not None:
            l = jproc.stdout.readline()
            if l:
                lines = [l.rstrip()]
        else:
            for p in list(positions):
                chunk, positions[p] = tail_file(p, positions[p])
                lines.extend(chunk)
        for ev in parse_lines(lines):
            num = ev["number"] or "?"
            if ev["kind"] == "ring":
                ring_counts[num] = ring_counts.get(num, 0) + 1
                if ANSWER and ring_counts[num] >= RINGS_BEFORE_ANSWER:
                    ok = answer_call(MODEM_TTY)
                    ev["answered"] = ok
                    ring_counts[num] = 0
            elif ev["kind"].startswith("voice"):
                ring_counts[num] = 0
                if ANSWER:
                    ev["answered"] = answer_call(MODEM_TTY)
            push_event(ev)
        time.sleep(POLL_SEC if jproc is None else 0.1)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
