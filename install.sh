#!/bin/bash
# fipt installer / установщик / встановлювач
#   Linux (десктоп): sudo bash install.sh
#   Termux:          bash install.sh          (root не нужен)
#   Удаление:        sudo bash install.sh --uninstall

APP="fipt"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LANG_CODE=""
UNINSTALL=0

# --- Аргументы ---
while [ $# -gt 0 ]; do
    case "$1" in
        --lang)      LANG_CODE="$2"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        *) echo "Usage: bash install.sh [--lang en|ru|ua] [--uninstall]"; exit 1 ;;
    esac
done

# --- Выбор языка ---
if [ -z "$LANG_CODE" ]; then
    if [ -t 0 ]; then
        echo "Select language / Выберите язык / Оберіть мову:"
        echo "1) English"
        echo "2) Русский"
        echo "3) Українська"
        read -r -p "> " LANG_CHOICE
        case "$LANG_CHOICE" in
            2) LANG_CODE="ru" ;;
            3) LANG_CODE="ua" ;;
            *) LANG_CODE="en" ;;
        esac
    else
        LANG_CODE="en"
    fi
fi
case "$LANG_CODE" in
    en|ru|ua) ;;
    *) echo "Invalid language / Неверный язык / Невірна мова: $LANG_CODE"; exit 1 ;;
esac

# say "english" "русский" "українська"
say() {
    case "$LANG_CODE" in
        ru) echo "$2" ;;
        ua) echo "$3" ;;
        *)  echo "$1" ;;
    esac
}

# --- Определение окружения ---
# Termux: свой $PREFIX, root не нужен. Обычный Linux: системные каталоги, нужен sudo.
case "$PREFIX" in
    *com.termux*)
        BIN_DIR="$PREFIX/bin"
        SHARE_DIR="$PREFIX/share/$APP"
        ;;
    *)
        BIN_DIR="/bin"
        SHARE_DIR="/usr/share/$APP"
        if [ "$(id -u)" -ne 0 ]; then
            say "Error: run the installer with sudo:  sudo bash install.sh" \
                "Ошибка: запустите установщик через sudo:  sudo bash install.sh" \
                "Помилка: запустіть встановлювач через sudo:  sudo bash install.sh"
            exit 1
        fi
        ;;
esac

# --- Прежняя раскладка (/usr/local): убираем, иначе старая ссылка в PATH перекроет новую ---
cleanup_legacy() {
    case "$PREFIX" in *com.termux*) return ;; esac
    if [ -L /usr/local/bin/$APP ] || [ -d /usr/local/share/$APP ]; then
        rm -f /usr/local/bin/$APP
        rm -rf /usr/local/share/$APP
        say "Removed the old installation from /usr/local." \
            "Старая установка из /usr/local удалена." \
            "Стару установку з /usr/local видалено."
    fi
}

# --- Удаление ---
if [ "$UNINSTALL" -eq 1 ]; then
    cleanup_legacy
    rm -f "$BIN_DIR/$APP"
    rm -rf "$SHARE_DIR"
    say "Removed: $BIN_DIR/$APP and $SHARE_DIR" \
        "Удалено: $BIN_DIR/$APP и $SHARE_DIR" \
        "Видалено: $BIN_DIR/$APP і $SHARE_DIR"
    say "Your settings are kept in ~/.config/$APP (delete the folder by hand if you want to)." \
        "Ваши настройки остались в ~/.config/$APP (удалите папку вручную, если нужно)." \
        "Ваші налаштування залишились у ~/.config/$APP (видаліть теку вручну, якщо потрібно)."
    exit 0
fi

echo ""
say "Installing $APP..." "Установка $APP..." "Встановлення $APP..."
echo ""

# --- Проверка исходных файлов ---
if [ ! -f "$SRC/$APP" ] || [ ! -d "$SRC/fiptui" ]; then
    say "Error: files '$APP' and 'fiptui' not found next to install.sh." \
        "Ошибка: файлы '$APP' и 'fiptui' не найдены рядом с install.sh." \
        "Помилка: файли '$APP' і 'fiptui' не знайдено поруч з install.sh."
    say "Run the script from the root of the fipt repository." \
        "Запустите скрипт из корня репозитория fipt." \
        "Запустіть скрипт з кореня репозиторію fipt."
    exit 1
fi

# --- Предупреждения (установку не останавливают) ---
command -v python3 >/dev/null 2>&1 || {
    say "Error: python3 not found. Install Python 3.10 or newer." \
        "Ошибка: python3 не найден. Установите Python 3.10 или новее." \
        "Помилка: python3 не знайдено. Встановіть Python 3.10 або новіший."
    exit 1
}
python3 -c "import textual" >/dev/null 2>&1 || \
    say "[!] Python package 'textual' is missing:  pip install textual" \
        "[!] Не установлен пакет 'textual':  pip install textual" \
        "[!] Не встановлено пакет 'textual':  pip install textual"
command -v mpv >/dev/null 2>&1 || \
    say "[!] mpv not found — fipt can't play without it." \
        "[!] mpv не найден — без него fipt ничего не проиграет." \
        "[!] mpv не знайдено — без нього fipt нічого не зіграє."

# --- Копирование файлов ---
case "$SHARE_DIR" in
    */share/"$APP") ;;
    *) echo "Error: bad install path: $SHARE_DIR"; exit 1 ;;
esac
say "Copying files to $SHARE_DIR..." \
    "Копирование файлов в $SHARE_DIR..." \
    "Копіювання файлів до $SHARE_DIR..."

cleanup_legacy
mkdir -p "$BIN_DIR" || { echo "Error: cannot create $BIN_DIR"; exit 1; }
if [ "$SRC" != "$SHARE_DIR" ]; then
    rm -rf "$SHARE_DIR"        # старая копия удаляется целиком, чтобы не оставались устаревшие модули
    mkdir -p "$SHARE_DIR" || { echo "Error: cannot create $SHARE_DIR"; exit 1; }
    for item in "$APP" fiptui locales themes data LICENSE README.md README_RU.md README_UA.md; do
        [ -e "$SRC/$item" ] && cp -R "$SRC/$item" "$SHARE_DIR/"
    done
    find "$SHARE_DIR" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
fi
chmod -R a+rX "$SHARE_DIR"     # читать и запускать должен любой пользователь, а не только тот, кто ставил

if [ ! -f "$SHARE_DIR/$APP" ]; then
    say "Error: $APP was not copied!" "Ошибка: $APP не скопирован!" "Помилка: $APP не скопійовано!"
    exit 1
fi
chmod +x "$SHARE_DIR/$APP"

# --- Команда fipt: ссылка на сам лаунчер (никаких оберток) ---
say "Creating command $APP..." "Создание команды $APP..." "Створення команди $APP..."
ln -sf "$SHARE_DIR/$APP" "$BIN_DIR/$APP" || { echo "Error: cannot create $BIN_DIR/$APP"; exit 1; }

# --- Язык интерфейса по умолчанию (существующие настройки не трогаем) ---
# Под sudo $HOME указывает на root, поэтому берём домашнюю папку того, кто вызвал sudo.
CONF_HOME="$HOME"
CONF_OWNER=""
if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
    CONF_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    CONF_OWNER="$SUDO_USER"
fi
if [ -n "$CONF_HOME" ] && [ -d "$CONF_HOME" ] && [ ! -f "$CONF_HOME/.config/$APP/settings.json" ]; then
    mkdir -p "$CONF_HOME/.config/$APP" &&
        printf '{\n  "language": "%s"\n}\n' "$LANG_CODE" > "$CONF_HOME/.config/$APP/settings.json"
    [ -n "$CONF_OWNER" ] && chown -R "$CONF_OWNER": "$CONF_HOME/.config/$APP" 2>/dev/null
fi

# --- Проверка и финальное сообщение ---
if [ -x "$BIN_DIR/$APP" ]; then
    echo ""
    say "Installation completed successfully!" \
        "Установка успешно завершена!" \
        "Встановлення успішно завершено!"
    echo ""
    say "Now you can run the player with the command:" \
        "Теперь вы можете запустить плеер командой:" \
        "Тепер ви можете запустити плеєр командою:"
    echo "  $APP"
    echo ""
    say "All project files are located in:" \
        "Все файлы проекта находятся в:" \
        "Всі файли проєкту знаходяться в:"
    echo "  $SHARE_DIR"
    echo ""
    say "Settings:" "Настройки:" "Налаштування:"
    echo "  ~/.config/$APP/settings.json"
    echo ""
else
    say "Something went wrong. The executable file was not installed." \
        "Что-то пошло не так. Исполняемый файл не был установлен." \
        "Щось пішло не так. Виконуваний файл не було встановлено."
    exit 1
fi

