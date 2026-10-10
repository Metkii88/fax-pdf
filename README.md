# Fax-PDF: приём и отправка факсов в PDF на Astra Linux 1.7 (HylaFAX)

Комплекс для работы с факсами в формате **PDF** на базе [HylaFAX](https://github.com/ifax/HylaFAX)
(форк ifax/HylaFAX) под **Astra Linux SE 1.7 «Генеральный»** (Debian 10 buster-совместимая,
с мандорным контролем доступа **PARSEC**).

## Состав

| Файл | Назначение |
|---|---|
| `install-astra.sh` | Установка зависимостей, сборка HylaFAX из исходников ifax/HylaFAX, настройка PARSEC |
| `sendfax.sh` | Отправка PDF-файла по факсу (`./sendfax.sh документ.pdf 74951234567`) |
| `recv-monitor.py` | Сервис мониторинга: принятые факсы конвертируются в PDF, раскладываются по папкам пользователей |
| `parsec-fax.policy` | Политика PARSEC для безопасной работы faxq/faxgetty/xferfaxd |
| `hylafax/*` | Шаблоны конфигурации HylaFAX (`config.local`, правила приёма/отправки) |
| `systemd/*.service` | Юниты: hylafax (faxq+faxgetty), fax-xferfaxd, fax-pdf-monitor |
| `webapp/` | Лёгкий веб-интерфейс (Flask) для загрузки PDF в очередь и просмотра статуса |

## Быстрый старт (на Astra Linux 1.7, от root)

```bash
# 1. Подготовка системы: зависимости + сборка HylaFAX v7 из исходников
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

### Безопасность (Astra SE 1.7 / PARSEC)
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

## Веб-интерфейс (опционально)
```bash
sudo apt install python3-flask
cd webapp && python3 app.py   # http://<ip>:8088 — загрузка PDF, очередь, журнал
```

## Удаление
`sudo ./uninstall.sh` — останавливает сервисы, удаляет пакеты; данные остаются.
