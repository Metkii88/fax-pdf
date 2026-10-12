#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
faxpdf/settings.py — настройки пользователя Fax-PDF GUI.

Хранятся в ~/.config/fax-pdf/config.json.  Основные ключи:
  incoming_dir : путь для сохранения принятых факсов (по умолчанию /home/user,
                 т.е. Path.home());
  voice_answer : отвечать на голосовые вызовы (поднятие «трубки»);
  voice_rings  : сколько звонков ждать перед ответом (RingsVoiceAnswer);
  out_sfx      : звуковые сигналы аппарата (beeps).

env-переменные имеют приоритет: FAX_INCOMING_DIR, FAX_VOICE_ANSWER(0/1),
FAX_VOICE_RINGS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CFG_PATH = Path(os.environ.get("FAX_GUI_CONFIG",
                               str(Path.home() / ".config" / "fax-pdf" / "config.json")))

DEFAULTS = {
    "incoming_dir": str(Path.home()),          # /home/user — по умолчанию
    "voice_answer": True,
    "voice_rings": 4,
    "out_sfx": True,
    "modem": "",                               # выбранный модем (/dev/ttyUSB0 и т.п.)
    "hsf_voice": False,                        # голос через vpmcall на HSF-модеме
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        if CFG_PATH.is_file():
            data = json.loads(CFG_PATH.read_text(encoding="utf-8"))
            for k in DEFAULTS:
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    # env overrides
    d = os.environ.get("FAX_INCOMING_DIR")
    if d:
        cfg["incoming_dir"] = d
    v = os.environ.get("FAX_VOICE_ANSWER")
    if v is not None and v != "":
        cfg["voice_answer"] = v not in ("0", "false", "False", "no")
    r = os.environ.get("FAX_VOICE_RINGS")
    if r:
        try:
            cfg["voice_rings"] = max(1, min(9, int(r)))
        except ValueError:
            pass
    return cfg


def save(cfg: dict) -> None:
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                        encoding="utf-8")


def get(key: str):
    return load()[key]


def set_incoming_dir(path: str) -> str:
    cfg = load()
    cfg["incoming_dir"] = str(Path(path).expanduser())
    save(cfg)
    return cfg["incoming_dir"]


def set_voice(answer: bool, rings: int) -> None:
    cfg = load()
    cfg["voice_answer"] = bool(answer)
    cfg["voice_rings"] = max(1, min(9, int(rings)))
    save(cfg)


def set_modem(dev: str) -> str:
    """Запомнить выбранный модем (путь устройства, напр. /dev/ttyUSB0)."""
    cfg = load()
    cfg["modem"] = dev or ""
    save(cfg)
    return cfg["modem"]


def set_hsf_voice(enable: bool) -> bool:
    """Запомнить, что голос на линии обслуживает HSF-драйвер (vpmcall)."""
    cfg = load()
    cfg["hsf_voice"] = bool(enable)
    save(cfg)
    return cfg["hsf_voice"]
