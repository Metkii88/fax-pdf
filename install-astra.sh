#!/usr/bin/env bash
# ============================================================================
# install-astra.sh — установка Fax-PDF (HylaFAX) на Astra Linux SE 1.7
# Собирает HylaFAX из исходников https://github.com/ifax/HylaFAX (v7.x),
# ставит зависимости, создаёт УЗ/каталоги, юниты systemd и PARSEC-политику.
# Запускать от root:  sudo ./install-astra.sh
# ============================================================================
set -euo pipefail

HYLAFAX_VERSION="${HYLAFAX_VERSION:-v7.0.11}"          # тег в ifax/HylaFAX
HYLAFAX_URL="https://github.com/ifax/HylaFAX.git"
BUILD_DIR="/usr/local/src/hylafax-build"
PREFIX="/opt/hylafax"                                  # бинарники HylaFAX
SPOOL="/var/spool/hylafax"                             # очередь (spool)
LOGDIR="/var/log/fax-pdf"
SRV="/srv/fax"

log() { echo -e "[\033[1;32m+\033[0m] $*"; }
err() { echo -e "[\033[1;31m!\033[0m] $*" >&2; }

[[ $EUID -eq 0 ]] || { err "Запустите с правами root (sudo ./install-astra.sh)"; exit 1; }

# Проверка, что мы действительно на Astra 1.7 (не блокируем, только предупреждаем)
if grep -q 'Astra' /etc/os-release 2>/dev/null; then
    log "Обнаружена Astra Linux: $(grep PRETTY /etc/os-release | cut -d'"' -f2)"
else
    err "Предупреждение: это не Astra Linux. Продолжаем, но PARSEC-шаги могут быть пропущены."
fi

# ----------------------------------------------------------------------------
# 1. Зависимости
# ----------------------------------------------------------------------------
log "Установка зависимостей (apt)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    build-essential devscripts equivs git pkg-config autoconf automake libtool \
    libtiff-dev libjpeg-dev libpng-dev libpcre3-dev \
    poppler-utils netpbm imagemagick ghostscript \
    inotify-tools mlock fax ttf-mscorefonts-installer || \
apt-get install -y build-essential git libtiff-dev libjpeg-dev libpng-dev \
    poppler-utils netpbm imagemagick ghostscript inotify-tools mlock

# pdftoppm входит в poppler-utils; tiff2pdf — в libtiff-tools
apt-get install -y libtiff-tools || true

# ----------------------------------------------------------------------------
# 2. Пользователи и группы
# ----------------------------------------------------------------------------
log "Создание служебных УЗ..."
getent group  fax   >/dev/null || groupadd -r fax
for u in faxq xferfaxd faxmon; do
    id -u "$u" >/dev/null 2>&1 || useradd -r -g fax -s /usr/sbin/nologin -d "$SPOOL" "$u"
done

# ----------------------------------------------------------------------------
# 3. Сборка HylaFAX из исходников ifax/HylaFAX
# ----------------------------------------------------------------------------
log "Клонирование HylaFAX ${HYLAFAX_VERSION} (${HYLAFAX_URL})..."
mkdir -p "$BUILD_DIR"
if [[ ! -d "$BUILD_DIR/HylaFAX/.git" ]]; then
    git clone --depth 1 --branch "$HYLAFAX_VERSION" "$HYLAFAX_URL" "$BUILD_DIR/HylaFAX"
else
    git -C "$BUILD_DIR/HylaFAX" fetch --depth 1 origin "$HYLAFAX_VERSION" \
        && git -C "$BUILD_DIR/HylaFAX" reset --hard FETCH_HEAD
fi

cd "$BUILD_DIR/HylaFAX"
log "Конфигурирование и сборка (autogen/configure/make)..."
./autogen.sh || ./configure --help >/dev/null || true
./configure \
    --prefix="$PREFIX" \
    --with-spool-dir="$SPOOL" \
    --with-log-dir="$LOGDIR" \
    --enable-pdf-send=no \
    --with-TIFFincdir=/usr/include/x86_64-linux-gnu

make -j"$(nproc)"
make install

# симлинки в PATH
for b in faxq faxgetty sendfax recvfax xferfaxd faxaddmodem faxrm faxstat faxwatch textfmt pdf2ps; do
    [[ -x "$PREFIX/bin/$b" || -x "$PREFIX/sbin/$b" ]] && \
        ln -sf "$PREFIX/$( [[ -x $PREFIX/sbin/$b ]] && echo sbin || echo bin )/$b" "/usr/local/bin/$b"
done
ln -sf "$PREFIX/sbin/faxq" /usr/local/sbin/faxq 2>/dev/null || true

# ----------------------------------------------------------------------------
# 4. Каталоги spool и данных
# ----------------------------------------------------------------------------
log "Настройка каталогов..."
install -d -m 750 -o faxq -g fax "$SPOOL" \
    "$SPOOL/etc" "$SPOOL/bin" "$SPOOL/lib" "$SPOOL/log" \
    "$SPOOL/sendq" "$SPOOL/recvq" "$SPOOL/notify" "$SPOOL/archive/sent" "$SPOOL/archive/recv" \
    "$SPOOL/dev" "$SPOOL/tmp" "$SPOOL/doc" "$SPOOL/info"
chmod 1770 "$SPOOL/tmp" "$SPOOL/recvq"
chown -R faxq:fax "$SPOOL"

install -d -m 750 "$LOGDIR"; chown faxq:fax "$LOGDIR"
install -d -m 750 "$SRV/incoming" "$SRV/outbox"; chown -R faxmon:fax "$SRV"

# ----------------------------------------------------------------------------
# 5. Конфиги HylaFAX из репозитория
# ----------------------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log "Копирование конфигурационных шаблонов из $REPO_DIR..."
cp -n "$REPO_DIR/hylafax/config.local" "$SPOOL/etc/config.local.example" || true
cp -n "$REPO_DIR/hylafax/FAXRules"     "$SPOOL/etc/FAXRules.example"    || true
cp -n "$REPO_DIR/hylafax/DialRules"    "$SPOOL/etc/DialRules.example"   || true
cp -n "$REPO_DIR/hylafax/notify"       "$SPOOL/etc/notify"              || true
chmod 640 "$SPOOL/etc/"* 2>/dev/null || true
chown faxq:fax "$SPOOL/etc/"* 2>/dev/null || true

# базовый config (до faxaddmodem)
if [[ ! -f "$SPOOL/etc/config" ]]; then
cat > "$SPOOL/etc/config" <<EOF
# Глобальная конфигурация HylaFAX (сгенерирована install-astra.sh)
CountryCode:      7
InternationalCode:00
LongDistanceContext:
AreaCode:         495
FAXNumber:        +74950000000
LocalIdentifier:  "Fax-PDF Server"
AdminName:        Administrator
EMailAddress:     faxadmin@localhost
RingNoAnswerToFAX: 3
MaxSendRetries:   3
SessionTracing:   0x3FFF
LogFacility:      local3
NotifyFormat:     list
EOF
    chown faxq:fax "$SPOOL/etc/config"; chmod 640 "$SPOOL/etc/config"
fi

# ----------------------------------------------------------------------------
# 6. Systemd-юниты
# ----------------------------------------------------------------------------
log "Установка systemd-юнитов..."
cp "$REPO_DIR/systemd/"*.service /etc/systemd/system/
cp "$REPO_DIR/systemd/faxgetty@.service" /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload

# ----------------------------------------------------------------------------
# 7. PARSEC-политика (только если есть parsec)
# ----------------------------------------------------------------------------
if command -v parsecd >/dev/null 2>&1 || [[ -d /etc/security/parsec ]]; then
    log "Применение политики PARSEC..."
    mkdir -p /etc/security/parsec
    cp "$REPO_DIR/parsec-fax.policy" /etc/security/parsec/policy.conf
    parsecd -p /etc/security/parsec/policy.conf -l || err "Не удалось загрузить политику — проверьте вручную"
else
    err "PARSEC не обнаружен — пропускаю мандатную настройку (подходит для КИ-версии без ПДС)."
fi

# ----------------------------------------------------------------------------
# 8. Итог
# ----------------------------------------------------------------------------
cat <<EOF

============================================================
Установка завершена. Дальнейшие шаги:

 1. Подключите модем и выполните:  sudo faxaddmodem ttyS0
    (или ttyUSB0) — он создаст etc/config.ttyS0 и запустит getty.
 2. При необходимости отредактируйте:
      $SPOOL/etc/config            — номер страны/городской код
      $REPO_DIR/hylafax/config.local -> скопируйте в $SPOOL/etc/config.local
 3. Запустите сервисы:
      sudo systemctl enable --now hylafax fax-xferfaxd fax-pdf-monitor
 4. Отправка PDF:
      ./sendfax.sh документ.pdf +74951234567
 5. Принятые факсы появятся в: $SRV/incoming/<user>/YYYY/MM/*.pdf
============================================================
EOF
