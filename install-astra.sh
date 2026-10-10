#!/usr/bin/env bash
# ============================================================================
# install-astra.sh — установка Fax-PDF (HylaFAX) на Astra Linux SE 1.7
# Собирает HylaFAX из исходников https://github.com/ifax/HylaFAX (ветка master, 6.1-GIT),
# ставит зависимости, создаёт УЗ/каталоги, юниты systemd и PARSEC-политику.
# Запускать от root:  sudo ./install-astra.sh
# ============================================================================
set -euo pipefail

# Ветка/тег исходников HylaFAX. В https://github.com/ifax/HylaFAX НЕТ тега
# v7.0.11 (актуальные ветки: master = 6.1-GIT, теги до HYLAFAX-6_0_7).
# Значение проверяется перед клонированием; можно переопределить:
#   HYLAFAX_VERSION=HYLAFAX-6_0_7 sudo ./install-astra.sh
HYLAFAX_VERSION="${HYLAFAX_VERSION:-master}"           # ветка master (6.1-GIT); по умолчанию не используется (см. USE_BUNDLED)
HYLAFAX_URL="${HYLAFAX_URL:-https://github.com/ifax/HylaFAX.git}"
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
# 3. Сборка HylaFAX: по умолчанию из вендорной копии vendor/hylafax
#    (git clone на github.com НЕ выполняется — работает на изолированных узлах).
#    Переопределение: USE_BUNDLED=0 HYLAFAX_VERSION=<ветка/тег> sudo ./install-astra.sh
# ----------------------------------------------------------------------------
USE_BUNDLED="${USE_BUNDLED:-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLED_DIR="$SCRIPT_DIR/vendor/hylafax"
SRC_DIR="$BUILD_DIR/HylaFAX"

if [[ "$USE_BUNDLED" = "1" && -f "$BUNDLED_DIR/configure" ]]; then
    log "Использование вендорной копии HylaFAX ($BUNDLED_DIR, версия $(cat "$BUNDLED_DIR/VERSION" 2>/dev/null || echo '?'))..."
    mkdir -p "$BUILD_DIR"
    rm -rf "$SRC_DIR"
    cp -a "$BUNDLED_DIR" "$SRC_DIR"
    # очистка возможных артефактов предыдущей сборки внутри копии
    make -C "$SRC_DIR" distclean >/dev/null 2>&1 || true
elif [[ "$USE_BUNDLED" = "1" ]]; then
    err "Вендорная копия не найдена ($BUNDLED_DIR) — переключаюсь на git clone."
    USE_BUNDLED=0
fi

if [[ "$USE_BUNDLED" != "1" ]]; then
    log "Клонирование HylaFAX ${HYLAFAX_VERSION} (${HYLAFAX_URL})..."
    mkdir -p "$BUILD_DIR"

    # Проверка существования ветки/тега до клонирования (в репозитории
    # НЕТ тега v7.0.11; актуальны master и теги до HYLAFAX-6_0_7).
    if ! git ls-remote --exit-code "$HYLAFAX_URL" \
            "refs/heads/$HYLAFAX_VERSION" "refs/tags/$HYLAFAX_VERSION" >/dev/null 2>&1; then
        err "Реф '$HYLAFAX_VERSION' не найден в $HYLAFAX_URL."
        err "Доступные ветки:"
        git ls-remote --heads "$HYLAFAX_URL" | sed 's|.*refs/heads/|  - |' >&2 || true
        err "Последние теги:"
        git ls-remote --tags "$HYLAFAX_URL" | awk '{print $2}' | grep -v '\\^{}' \
            | sort -V | tail -8 | sed 's|refs/tags/|  - |' >&2 || true
        err "Укажите корректную версию:  HYLAFAX_VERSION=master sudo ./install-astra.sh"
        err "Или используйте вендорную копию: USE_BUNDLED=1 (по умолчанию)."
        exit 1
    fi

    if [[ ! -d "$SRC_DIR/.git" ]]; then
        rm -rf "$SRC_DIR"   # убрать остатки неудачной предыдущей попытки
        git clone --depth 1 --branch "$HYLAFAX_VERSION" "$HYLAFAX_URL" "$SRC_DIR"
    else
        git -C "$SRC_DIR" fetch --depth 1 origin "$HYLAFAX_VERSION" \
            && git -C "$SRC_DIR" reset --hard FETCH_HEAD
    fi
fi

cd "$SRC_DIR"
log "Конфигурирование и сборка (configure/make)..."
# В ifax/HylaFAX нет autogen.sh — используется собственный интерактивный
# configure. Ключи --with-DIR_* задаются напрямую (см. ./configure -help);
# --nointeractive отключает приглашения, --disable-pam обязателен на Astra SE.
./configure \
    --nointeractive \
    --disable-nls \
    --disable-pam \
    --with-DIR_BIN="$PREFIX/bin" \
    --with-DIR_SBIN="$PREFIX/sbin" \
    --with-DIR_LIBEXEC="$PREFIX/sbin" \
    --with-DIR_LIBDATA="$PREFIX/lib/fax" \
    --with-DIR_SPOOL="$SPOOL" \
    --with-DIR_MAN="$PREFIX/share/man" \
    --with-LOCKFILES=/var/lock

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
