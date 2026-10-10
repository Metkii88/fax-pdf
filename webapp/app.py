#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webapp/app.py — минимальный веб-интерфейс Fax-PDF (Flask) для Astra Linux 1.7.

Возможности:
  * загрузка PDF в очередь отправки (запуск sendfax.sh как subprocess);
  * просмотр текущих задач (вывод faxstat -a -u all, разбор текста);
  * список принятых документов по папкам /srv/fax/incoming/<user>/...;
  * выдача PDF на скачивание + проверка SHA-256 sidecar.

Запуск (для разработки):   python3 app.py
В проде: uwsgi/gunicorn + nginx, либо systemd-юнит fax-webapp.service.
Доступ: http://<ip>:8088  (рекомендуется ограничить подсетью АРМ в PARSEC/iptables).
"""
import glob
import hashlib
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

try:
    from flask import (Flask, abort, flash, redirect, render_template_string,
                       request, send_file, url_for)
except ImportError:
    raise SystemExit("Установите Flask:  sudo apt install python3-flask")

SPOOL = os.environ.get("HYLASPOOL", "/var/spool/hylafax")
INCOMING = Path(os.environ.get("FAX_INCOMING", "/srv/fax/incoming"))
OUTBOX = Path(os.environ.get("FAX_OUTBOX", "/srv/fax/outbox"))
SENDFAX = os.environ.get("SENDFAX_SH", str(Path(__file__).resolve().parent.parent / "sendfax.sh"))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", datetime.now().isoformat())

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Fax-PDF :: панель оператора</title>
<style>
 body{font-family:sans-serif;margin:2em;background:#f4f6f8}
 .card{background:#fff;border-radius:8px;padding:1em 1.5em;margin-bottom:1.5em;
       box-shadow:0 1px 3px rgba(0,0,.15)}
 table{border-collapse:collapse;width:100%} td,th{padding:.35em .6em;border-bottom:1px solid #ddd;text-align:left}
 input[type=file],input[type=text]{margin:.3em 0}
 button{background:#2c7be5;color:#fff;border:0;padding:.5em 1.2em;border-radius:4px;cursor:pointer}
 .flash{background:#ffe9c7;padding:.6em;border-radius:4px}
 h2{margin-top:0} a{color:#2c7be5}
</style></head><body>
<h1>📠 Fax-PDF — приём и отправка факсов (HylaFAX / Astra 1.7)</h1>
{% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}

<div class="card"><h2>Отправить факс (PDF)</h2>
<form method=post enctype=multipart/form-data action="{{url_for('send')}}">
 <input type=file name=doc accept=application/pdf required>
 <br><input type=text name=number placeholder="+74951234567" pattern="[+0-9\\- ]{5,20}" required size=20>
 <br><button type=submit>Отправить</button>
</form></div>

<div class="card"><h2>Очередь отправки (faxstat)</h2>
<table><tr><th>JobID</th><th>Отправитель</th><th>Номер</th><th>Статус</th></tr>
{% for j in jobs %}<tr><td>{{j.id}}</td><td>{{j.user}}</td><td>{{j.dest}}</td><td>{{j.status}}</td></tr>{% endfor %}
{% if not jobs %}<tr><td colspan=4>Очередь пуста</td></tr>{% endif %}</table></div>

<div class="card"><h2>Принятые документы</h2>
<table><tr><th>Дата</th><th>Размер</th><th>Документ</th><th>SHA-256</th></tr>
{% for f in received %}<tr>
 <td>{{f.date}}</td><td>{{f.size}} КБ</td>
 <td><a href="{{url_for('get_file', path=f.rel)}}">{{f.name}}</a></td>
 <td><code>{{f.sha[:16]}}…</code></td></tr>{% endfor %}
{% if not received %}<tr><td colspan=4>Пока пусто</td></tr>{% endif %}</table></div>

<p style="color:#888">Сервер: {{host}} · Spool: {{spool}} · {{now}}</p>
</body></html>"""


def faxstat_jobs():
    """Разбор вывода `faxstat -a -u all` в список словарей."""
    try:
        out = subprocess.run(["faxstat", "-a", "-u", "all"], capture_output=True,
                             text=True, timeout=10).stdout
    except FileNotFoundError:
        return []
    jobs = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\S+)\s+\S*\s*(\S*)\s*.*?(SND|RCV|RUN|PEND)", line)
        if m:
            jobs.append(dict(id=m.group(1), user=m.group(2),
                             dest=m.group(3), status=m.group(4)))
    return jobs


def list_received(limit=50):
    files = []
    for p in sorted(INCOMING.rglob("*.pdf"), reverse=True)[:limit]:
        sha = ""
        side = p.with_suffix(".pdf.sha256")
        if side.exists():
            sha = side.read_text(errors="ignore").split()[0] if side.stat().st_size else ""
        files.append(dict(name=p.name, rel=str(p.relative_to(INCOMING)),
                          size=p.stat().st_size // 1024,
                          date=datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                          sha=sha))
    return files


@app.route("/")
def index():
    return render_template_string(PAGE, jobs=faxstat_jobs(), received=list_received(),
                                  host=os.uname().nodename, spool=SPOOL,
                                  now=datetime.now().strftime("%F %T"))


@app.route("/send", methods=["POST"])
def send():
    f = request.files.get("doc")
    number = (request.form.get("number") or "").strip()
    if not f or not number:
        flash("Нужны файл и номер."); return redirect(url_for("index"))
    if not f.filename.lower().endswith(".pdf"):
        flash("Принимаются только PDF."); return redirect(url_for("index"))
    tmp = Path("/tmp") / f"web-{datetime.now():%Y%m%d%H%M%S}-{f.filename}"
    f.save(tmp)
    r = subprocess.run([SENDFAX, str(tmp), number], capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        flash(f"OK: {r.stdout.strip().splitlines()[-2] if r.stdout else 'задача поставлена'}")
    else:
        flash(f"Ошибка отправки: {r.stderr.strip()[:300]}")
    return redirect(url_for("index"))


@app.route("/file/<path:path>")
def get_file(path):
    """Отдача принятого PDF с проверкой, что путь не выходит за INCOMING."""
    real = (INCOMING / path).resolve()
    if not str(real).startswith(str(INCOMING.resolve())) or not real.is_file():
        abort(404)
    return send_file(real, mimetype="application/pdf", as_attachment=True,
                     download_name=real.name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8088)))
