#!/usr/bin/env bash
# fipt installer / установщик / встановлювач
#   bash install.sh [--lang en|ru|ua] [--prefix DIR] [--uninstall]
set -eu

APP=fipt
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LANG_CODE=""
PREFIX_DIR=""
UNINSTALL=0

usage() {
  cat <<USAGE
Usage: bash install.sh [--lang en|ru|ua] [--prefix DIR] [--uninstall]

  --lang       installer/interface language (otherwise asked interactively)
  --prefix     install under DIR/share/$APP and DIR/bin/$APP
               (default: \$PREFIX in Termux, /usr/local as root, ~/.local otherwise)
  --uninstall  remove the installed copy (settings in ~/.config/$APP are kept)
USAGE
}

die() { echo "error: $*" >&2; exit 1; }
need_arg() { [ "$#" -ge 2 ] && [ -n "$2" ] || die "option $1 needs a value"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --lang)      need_arg "$@"; LANG_CODE="$2"; shift 2 ;;
    --prefix)    need_arg "$@"; PREFIX_DIR="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage >&2; die "unknown option: $1" ;;
  esac
done

# ── язык ──────────────────────────────────────────────────────────────
if [ -z "$LANG_CODE" ]; then
  if [ -t 0 ]; then
    echo "Select language / Выберите язык / Оберіть мову:"
    echo "  1) English"
    echo "  2) Русский"
    echo "  3) Українська"
    printf "> "
    read -r choice || choice=1
    case "$choice" in 2) LANG_CODE=ru ;; 3) LANG_CODE=ua ;; *) LANG_CODE=en ;; esac
  else
    LANG_CODE=en
  fi
fi
case "$LANG_CODE" in en|ru|ua) ;; *) die "unknown language: $LANG_CODE (en, ru, ua)" ;; esac

# say "english" "русский" "українська"
say() { case "$LANG_CODE" in ru) printf '%s\n' "$2" ;; ua) printf '%s\n' "$3" ;; *) printf '%s\n' "$1" ;; esac; }

# ── куда ставим ───────────────────────────────────────────────────────
IS_TERMUX=0
case "${PREFIX:-}" in *com.termux*) IS_TERMUX=1 ;; esac

if [ -z "$PREFIX_DIR" ]; then
  if [ "$IS_TERMUX" -eq 1 ]; then
    PREFIX_DIR="$PREFIX"
  elif [ "$(id -u)" -eq 0 ]; then
    PREFIX_DIR=/usr/local
  else
    PREFIX_DIR="$HOME/.local"
  fi
fi
SHARE_DIR="$PREFIX_DIR/share/$APP"
BIN_DIR="$PREFIX_DIR/bin"
BIN="$BIN_DIR/$APP"
case "$SHARE_DIR" in */share/"$APP") ;; *) die "bad install path: $SHARE_DIR" ;; esac   # защита от rm -rf не того

# ── удаление ──────────────────────────────────────────────────────────
if [ "$UNINSTALL" -eq 1 ]; then
  if [ -f "$BIN" ] && grep -q "fipt wrapper" "$BIN" 2>/dev/null; then
    rm -f "$BIN"
  fi
  rm -rf "$SHARE_DIR"
  say "Removed: $SHARE_DIR and $BIN" \
      "Удалено: $SHARE_DIR и $BIN" \
      "Видалено: $SHARE_DIR і $BIN"
  say "Settings are kept in ~/.config/$APP (delete the folder manually if you want)." \
      "Настройки остались в ~/.config/$APP (удалите папку вручную, если нужно)." \
      "Налаштування залишились у ~/.config/$APP (видаліть теку вручну, якщо потрібно)."
  exit 0
fi

# ── проверка окружения ────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || {
  say "python3 not found. Install Python 3.10 or newer." \
      "python3 не найден. Установите Python 3.10 или новее." \
      "python3 не знайдено. Встановіть Python 3.10 або новіший."
  exit 1
}
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  say "Python is too old: 3.10+ is required." \
      "Python слишком старый: нужен 3.10+." \
      "Python застарий: потрібен 3.10+."
  exit 1
}

if [ "$IS_TERMUX" -eq 1 ]; then
  HINT="pkg install python mpv yt-dlp && pip install textual mutagen"
else
  HINT="sudo apt install python3 python3-pip mpv yt-dlp && pip3 install --user textual mutagen"
fi

python3 -c 'import textual' >/dev/null 2>&1 || {
  say "[!] Python package 'textual' is missing: pip install textual" \
      "[!] Не установлен пакет 'textual': pip install textual" \
      "[!] Не встановлено пакет 'textual': pip install textual"
  say "    Everything at once: $HINT" \
      "    Всё сразу: $HINT" \
      "    Все разом: $HINT"
}
command -v mpv >/dev/null 2>&1 || {
  say "[!] mpv not found in PATH — fipt can't play without it." \
      "[!] mpv не найден в PATH — без него fipt ничего не проиграет." \
      "[!] mpv не знайдено в PATH — без нього fipt нічого не зіграє."
}
python3 -c 'import mutagen' >/dev/null 2>&1 || {
  say "[i] optional: pip install mutagen — track names from tags in the queue." \
      "[i] по желанию: pip install mutagen — названия треков из тегов в очереди." \
      "[i] за бажанням: pip install mutagen — назви треків із тегів у черзі."
}
command -v yt-dlp >/dev/null 2>&1 || {
  say "[i] optional: yt-dlp — to play YouTube and other links." \
      "[i] по желанию: yt-dlp — для ссылок на YouTube и другие сайты." \
      "[i] за бажанням: yt-dlp — для посилань на YouTube та інші сайти."
}

# ── копирование ───────────────────────────────────────────────────────
mkdir -p "$BIN_DIR" || die "cannot create $BIN_DIR (try: sudo bash install.sh, or --prefix ~/.local)"
if [ "$SRC" != "$SHARE_DIR" ]; then
  rm -rf "$SHARE_DIR"
  mkdir -p "$SHARE_DIR" || die "cannot create $SHARE_DIR"
  for item in fipt fiptui locales themes data LICENSE README.md README_RU.md README_UA.md; do
    [ -e "$SRC/$item" ] && cp -R "$SRC/$item" "$SHARE_DIR/"
  done
  find "$SHARE_DIR" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
fi
chmod +x "$SHARE_DIR/fipt"

SH_PATH=/bin/sh
if [ -n "${PREFIX:-}" ] && [ -x "$PREFIX/bin/sh" ]; then SH_PATH="$PREFIX/bin/sh"; fi   # Termux: нет /bin
cat > "$BIN" <<WRAP
#!$SH_PATH
# fipt wrapper (generated by install.sh)
exec python3 "$SHARE_DIR/fipt" "\$@"
WRAP
chmod +x "$BIN"

# ── язык интерфейса по умолчанию (не трогаем уже существующие настройки) ─
CONF_DIR="$HOME/.config/$APP"
if [ ! -f "$CONF_DIR/settings.json" ]; then
  mkdir -p "$CONF_DIR" 2>/dev/null && printf '{\n  "language": "%s"\n}\n' "$LANG_CODE" > "$CONF_DIR/settings.json" || true
fi

echo
say "Done! Run it from anywhere with:  $APP" \
    "Готово! Запускайте из любой папки командой:  $APP" \
    "Готово! Запускайте з будь-якої теки командою:  $APP"
say "Settings: $CONF_DIR/settings.json" \
    "Настройки: $CONF_DIR/settings.json" \
    "Налаштування: $CONF_DIR/settings.json"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "[!] $BIN_DIR is not in PATH. Add to ~/.bashrc:  export PATH=\"$BIN_DIR:\$PATH\"" \
         "[!] $BIN_DIR нет в PATH. Добавьте в ~/.bashrc:  export PATH=\"$BIN_DIR:\$PATH\"" \
         "[!] $BIN_DIR немає в PATH. Додайте в ~/.bashrc:  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
