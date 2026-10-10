#!/usr/bin/env bash
# ============================================================================
# sendfax.sh — отправка PDF-документа по факсу через HylaFAX
#
# Использование:
#   ./sendfax.sh документ.pdf 74951234567 [ещё_номер ...]
#   ./sendfax.sh -d "Комментарий" doc.pdf +7... 
#
# PDF конвертируется в TIFF G4 (200x200 точек/дюйм, одноканальный),
# оригинал архивируется, статус задачи выводится в конце.
# ============================================================================
set -euo pipefail

SPOOL="${SPOOL:-/var/spool/hylafax}"
ARCHIVE="$SPOOL/archive/sent"
TMP="$(mktemp -d /tmp/faxpdf.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

DPI_X=200; DPI_Y=200            # стандартная развёртка Group3/Group4
COMMENT=""

usage() {
    echo "Использование: $0 [-d комментарий] файл.pdf номер [номер...]" >&2
    exit 1
}

[[ $# -ge 2 ]] || usage

while [[ $# -gt 0 ]]; do
    case "$1" in
        -d) COMMENT="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) break ;;
    esac
done

PDF="$1"; shift
NUMS=("$@")

[[ -f "$PDF" ]] || { echo "Ошибка: файл '$PDF' не найден." >&2; exit 2; }
file "$PDF" | grep -qi 'PDF document' || { echo "Ошибка: '$PDF' — не PDF." >&2; exit 2; }

for n in "${NUMS[@]}"; do
    [[ "$n" =~ ^[+0-9][0-9\-]*$ ]] || { echo "Некорректный номер: $n" >&2; exit 2; }
done

command -v sendfax >/dev/null || { echo "sendfax не найден в PATH — выполните install-astra.sh" >&2; exit 3; }

# ---------------------------------------------------------------------------
# 1. PDF -> многостраничный TIFF G4 (HylaFAX умеет отправлять растровые форматы)
# ---------------------------------------------------------------------------
echo "[1/3] Конвертация $(basename "$PDF") -> TIFF G4 ..."
BASE="$TMP/$(basename "${PDF%.*}")"

if command -v pdftoppm >/dev/null; then
    # poppler: page-00001.pbm (1-bit при -mono)
    pdftoppm -r "$DPI_X" -mono -raw -png "$PDF" "$TMP/pg" \
      || pdftoppm -r "$DPI_X" -gray "$PDF" "$TMP/pg"
    # склеиваем страницы в один многостраничный TIFF G4
    PAGES=("$TMP"/pg*.png "$TMP"/pg*.pgm "$TMP"/pg*.pbm)
    found=0; tiffargs=()
    for p in "${PAGES[@]}"; do [[ -f "$p" ]] && { tiffargs+=("$p"); found=1; }; done
    [[ $found -eq 1 ]] || { echo "Ошибка конвертации PDF (страницы не созданы)." >&2; exit 4; }
    if command -v img2tiff >/dev/null; then
        img2tiff -c g4 "${tiffargs[@]}" "$BASE.tif" 2>/dev/null \
          || img2tiff "${tiffargs[@]}" "$BASE.tif"
    else
        convert "${tiffargs[@]}" -compress fax "$BASE.tif"   # imagemagick
    fi
elif command -v gs >/dev/null; then
    gs -q -dBATCH -dNOPAUSE -sDEVICE=tiffg4 -r"$DPI_X" \
       -sOutputFile="$BASE.tif" "$PDF"
else
    echo "Не найдено ни pdftoppm, ни ghostscript. Установите poppler-utils." >&2
    exit 4
fi

[[ -f "$BASE.tif" ]] || { echo "Не удалось создать TIFF." >&2; exit 4; }

# ---------------------------------------------------------------------------
# 2. Постановка в очередь HylaFAX
# ---------------------------------------------------------------------------
echo "[2/3] Постановка в очередь (${NUMS[*]})..."
SEND_ARGS=(-t "$BASE.tif")
[[ -n "$COMMENT" ]] && SEND_ARGS+=(-D "$COMMENT")
JOBS="$(sendfax "${SEND_ARGS[@]}" "${NUMS[@]}" | grep -oE '[0-9]+' | head -1)"
[[ -n "$JOBS" ]] || { echo "sendfax не вернул id задачи." >&2; exit 5; }

# ---------------------------------------------------------------------------
# 3. Архивирование оригинала
# ---------------------------------------------------------------------------
mkdir -p "$ARCHIVE/$(date +%Y/%m)"
cp -a "$PDF" "$ARCHIVE/$(date +%Y/%m)/$(date +%Y%m%d-%H%M%S)-job${JOBS}-$(basename "$PDF")" 2>/dev/null || true

echo "[3/3] Готово. Задача #$JOBS в очереди."
echo "Статус: faxstat -j $JOBS   |   Лог: $SPOOL/log/send-${JOBS}.log"
