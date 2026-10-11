# Fax-PDF: приём и отправка факсов в PDF на Astra Linux 1.7 (HylaFAX)

Комплекс для работы с факсами в формате **PDF** на базе [HylaFAX](https://github.com/ifax/HylaFAX)
(форк ifax/HylaFAX) под **Astra Linux SE 1.7 «Генеральный»** (Debian 10 buster-совместимая,
с мандатным контролем доступа (МКД) **PARSEC**).

## Состав

| Файл | Назначение |
|---|---|
| `install-astra.sh` | Установка зависимостей, сборка HylaFAX **из вендорной копии** `vendor/hylafax` (без скачивания с github.com), настройка PARSEC |
| `vendor/hylafax/` | Зафиксированная копия исходников HylaFAX master (6.1-GIT, commit c6cac8d8) — офлайн-сборка |
| `sendfax.sh` | Отправка PDF-файла по факсу (`./sendfax.sh документ.pdf 74951234567`) |
| `recv-monitor.py` | Сервис мониторинга: принятые факсы конвертируются в PDF, раскладываются по папкам пользователей (папка сохранения настраивается GUI, по умолчанию — домашняя) |
| `voice-monitor.py` | Монитор голосовых вызовов: RING/VOICE CALL из syslog → очередь пропущенных для GUI, автоответ (AT-оффхук) при поддержке модема |
| `parsec-fax.policy` | Политика PARSEC (МКД) для безопасной работы faxq/faxgetty/xferfaxd |
| `gui/faxpdf/` | Графический интерфейс «Факс-приёмник» (PyQt5): live-мониторинг входящих, отправка PDF, вкладка **«Факсимильный аппарат»** — интерфейс в виде рисованного факса (клавиатура, LCD READY/RING/RX/TX/HOLD/ERROR, лоток выдачи, трубка) |
| `debian/` | Правила сборки deb-пакета (`dpkg-buildpackage`) |
| `hylafax/*` | Шаблоны конфигурации HylaFAX (`config.local`, правила приёма/отправки) |
| `systemd/*.service` | Юниты: hylafax (faxq+faxgetty), fax-xferfaxd, fax-pdf-monitor |
| `webapp/` | Лёгкий веб-интерфейс (Flask) для загрузки PDF в очередь и просмотра статуса |

## Быстрый старт (на Astra Linux 1.7, от root)

```bash
# 1. Подготовка системы: зависимости + сборка HylaFAX из вендорной копии vendor/hylafax
#    (git clone с github.com НЕ выполняется; для сборки из свежего upstream:
#     USE_BUNDLED=0 sudo ./install-astra.sh)
sudo ./install-astra.sh

# 2. Настройка модема (автоопрос портов, генерация etc/config для ttyS0)
sudo faxaddmodem ttyS0

# 3. Наложение наших конфигов
sudo cp hylafax/config.local /var/spool/hylafax/etc/config.local
sudo cp hylafax/FAXRules.recv /var/spool/hylafax/etc/FAXRules
sudo install -m 640 parsec-fax.policy /etc/security/parsec/policy.conf
sudo parsecd -p /etc/security/parsec/policy.conf -l   # загрузка политики БЭД/МЧВ

# 4. Запуск сервисов
sudo systemctl enable --now hylafax fax-xferfaxd fax-pdf-monitor

# 5. Отправить PDF
./sendfax.sh /path/to/документ.pdf +74951234567
```

## Как это работает

### Отправка (PDF → факс)
1. Оператор кладёт PDF в `/srv/fax/outbox/<user>/` или вызывает `sendfax.sh файл.pdf номер`.
2. Скрипт конвертирует PDF → TIFF G3/G4 (`pdftoppm` + `tiffcp`): HylaFAX умеет отправлять
   только растровые форматы; оригинальный PDF архивируется в `/var/spool/hylafax/archive/sent/`.
3. `sendfax` ставит задачу в очередь `faxq`; `faxgetty` через модем набирает номер,
   передаёт изображение (V.29/V.17/V.34).
4. После отправки скрипт `etc/notify` пишет отчёт в `/var/log/fax-pdf/report.log` и
   уведомляет оператора (письмо на localhost / zenity-сообщение).

### Приём (факс → PDF)
1. `faxgetty` отвечает на звонок, принимает страницу, сохраняет в `recvq/` (TIFF-F).
2. `recv-monitor.py` (watchdog по inotify) ловит новые файлы, конвертирует TIFF→PDF
   (`tiff2pdf`), вычисляет SHA-256, складывает в `/srv/fax/incoming/<user>/YYYY/MM/`,
   формирует PDF-квитанцию и шлёт уведомление.

### Мандатный контроль доступа (МКД) — Astra SE 1.7 / PARSEC
* Демоны работают под выделенными УЗ `faxq`, `xferfaxd`, `faxmon` с уровнями МЧВ.
* `parsec-fax.policy`:
  * запись принятого — только в `recvq` с меткой «ДСП» (для служебного пользования);
  * чтение исходящих — только владельцем документа;
  * TCP 4559/4560 (xfax) — доступ только из подсети АРМ оператора;
  * `fax-drop` — единственная утилита, которой разрешено понижение метки при
    раскладке документов пользователям.

## Требования к оборудованию
* Аналоговый модем Class 1 / Class 2.0 (Conexant, Lucent, USRobotics). **Не winmodem!**
* Порт RS-232 (`ttyS0`…`ttyS3`) или USB-модем (`ttyUSB0`).
* Проверка: `dmesg | grep tty`, затем `chat -v "" ATZ OK "" AT+FCQ OK`

## Структура каталогов после установки
```
/var/spool/hylafax/            — очередь HylaFAX (sendq, recvq, log, etc)
/srv/fax/incoming/<user>/      — принятые PDF
/srv/fax/outbox/<user>/        — папка исходящих PDF оператора
/var/spool/hylafax/archive/    — архив отправленного/принятого
/etc/security/parsec/policy.conf — политика PARSEC
/opt/hylafax/                  — бинарники, собранные из ifax/HylaFAX
```

## Графический интерфейс (v1.3.0)
```bash
# из исходников:
python3 -m faxpdf --demo        # демо-режим аппарата без модема (RING/RX/TX имитация)
fax-pdf-gui                     # после установки deb-пакета
```
Устанавливается вместе с пакетом `fax-pdf_*.deb` (`sudo apt install ./fax-pdf_1.3.2-1_all.deb`).

### Сборка HylaFAX из вендорной копии (v1.3.2)
HylaFAX (ifax/HylaFAX master, commit c6cac8d8) включён в репозиторий как `vendor/hylafax/` —
при установке **не выполняется git clone** с github.com, работает на изолированных узлах.
К вендорной копии применены патчи совместимости с современными системами:
- `configure`: определение libtiff 4.x расширено до `4.[0-9]*` (`tiff_runlen_t=uint32`,
  `TIFFHeaderClassic`) — устраняет ошибки компиляции/линковки с libtiff5/6 и gcc-12;
- `debian/clean` очищает артефакты `./configure` (`Makefile`, `config.h`, `port.h`, `defs`,
  `rules`, `config.cache/log/status`), в git/пакет не попадают `.o/.a/.so`.
Возврат к онлайн-клонированию: `USE_BUNDLED=0 HYLAFAX_VERSION=master sudo ./install-astra.sh`
(осторожно: тега `v7.0.11` в ifax/HylaFAX не существует — используйте `master` или `HYLAFAX-6_0_7`).

### Новое в v1.3.0
- **Голосовые вызовы**: `voice-monitor.py` (systemd-юнит `fax-pdf-voice.service`) читает
  события RING / «UNANSWERED VOICE CALL» из syslog HylaFAX, складывает их в JSONL-очередь
  пропущенных/входящих звонков; на аппарате появляется режим **VOICE** (LCD мигает RING →
  трубка поднята). Кнопка **VOICE** — снять/положить «трубку», **STOP** — завершить разговор.
  Автоответ включается чекбоксом «Отвечать на голосовые» (+ число колец до ответа);
  параметры пишутся в `~/.config/fax-pdf/config.json` и синхронизируются с
  `RingsVoiceAnswer` в конфигах модема HylaFAX (`$SPOOL/etc/config.tty*`).
  Для реального голоса нужен модем с голосовой поддержкой (Class 1.0 voice); без неё
  вызов регистрируется и «отвечает» на уровне линии.
- **Клавиатура набора номера**: цифры 0–9, `*`, `#`, `+`, `C` (сброс), **REDIAL** — повтор
  последнего номера; набор напрямую на LCD аппарата перед отправкой.
- **Выбор файлов для отправки**: кнопка **TX▲** открывает диалог с множественным выбором
  PDF/TIFF — пачка отправляется последовательно (прогресс TX i/n на экране); во вкладке
  «Отправка» тоже можно указать несколько файлов через `;`.
- **Путь сохранения принятых факсов**: по умолчанию — домашняя папка пользователя
  (`/home/user`); меняется кнопкой **SAVE…** на аппарате или полем «Папка для принятых
  факсов» (Выбрать…/Открыть). Выбор сохраняется в `~/.config/fax-pdf/config.json` и
  `$SPOOL/etc/recv-archive.conf`, который учитывает серверный `recv-monitor.py`.

## Веб-интерфейс (опционально)
```bash
sudo apt install python3-flask
cd webapp && python3 app.py   # http://<ip>:8088 — загрузка PDF, очередь, журнал
```

## Удаление
`sudo ./uninstall.sh` — останавливает сервисы, удаляет пакеты; данные остаются.
