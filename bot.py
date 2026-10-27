from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# Страница Дневника, где находится ссылка «ЗДЕСЬ».
WIKI_URL = (
    "https://dnevnik.ru/v2/soc/r/rsoa/wiki/view?network=102350&"
    "pageTitle=%u0418%u0437%u043c%u0435%u043d%u0435%u043d%u0438%u044f"
    "%20%u0432%20%u0440%u0430%u0441%u043f%u0438%u0441%u0430%u043d%u0438%u0438"
)

TIME_ZONE = ZoneInfo("Europe/Moscow")
STATE_FILE = Path("state.json")
NO_SCHEDULE_TEXT = "Расписания нет. Скорее всего без изменений."
DISPATCHER_URL = "https://cloud.mail.ru/api/v2/dispatcher"
MAIL_API_EMAIL = "mail@mail.ru"  # Публичный API-запрос, вход в аккаунт не выполняется.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_TELEGRAM_PHOTO_BYTES = 10 * 1024 * 1024
MAX_TELEGRAM_DOCUMENT_BYTES = 50 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("raspisanie_bot")


class BotError(RuntimeError):
    """Ошибка, которую можно безопасно показать владельцу бота."""


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "SchoolScheduleTelegramBot/1.0",
            "Accept": "*/*",
        }
    )
    return session


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BotError("Не удалось прочитать state.json. Проверь его содержимое.") from exc
    if not isinstance(value, dict):
        raise BotError("Файл state.json должен содержать JSON-объект, например {}.")
    return value


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def find_cloud_link(session: requests.Session) -> str | None:
    try:
        response = session.get(WIKI_URL, timeout=(15, 35))
    except requests.RequestException as exc:
        raise BotError(f"Не удалось открыть страницу Дневника ({type(exc).__name__}).") from None

    if response.status_code != 200:
        raise BotError(f"Страница Дневника вернула HTTP {response.status_code}.")
    if urlparse(response.url).hostname not in {"dnevnik.ru", "www.dnevnik.ru"}:
        raise BotError("Страница Дневника перенаправила на неожиданный адрес.")

    soup = BeautifulSoup(response.text, "html.parser")
    for anchor in soup.find_all("a", href=True):
        label = " ".join(anchor.get_text(" ", strip=True).split()).casefold()
        if label == "здесь":
            link = urljoin(response.url, anchor["href"].strip())
            parsed = urlparse(link)
            if parsed.scheme != "https" or parsed.hostname != "cloud.mail.ru":
                raise BotError("Ссылка «ЗДЕСЬ» ведёт не на публичный файл cloud.mail.ru.")
            if not parsed.path.startswith("/public/"):
                raise BotError("Ссылка «ЗДЕСЬ» не похожа на публичную ссылку Облака Mail.")
            return link

    # Отсутствие ссылки считаем обычным отсутствием нового расписания.
    return None


def get_public_file_id(public_link: str) -> str:
    parsed = urlparse(public_link)
    if parsed.scheme != "https" or parsed.hostname != "cloud.mail.ru":
        raise BotError("Неожиданный адрес публичной ссылки Облака Mail.")

    pieces = [unquote(piece) for piece in parsed.path.split("/") if piece]
    if len(pieces) < 3 or pieces[0] != "public":
        raise BotError("Не удалось распознать идентификатор публичного файла Облака Mail.")

    # Для ссылки вида /public/rdia/aStnjqiB5 это будет rdia/aStnjqiB5.
    return "/".join(pieces[-2:])


def download_cloud_png(session: requests.Session, public_link: str) -> tuple[bytes, str]:
    file_id = get_public_file_id(public_link)
    params = {"api": 2, "email": MAIL_API_EMAIL, "_": 86400}
    try:
        dispatch_response = session.get(DISPATCHER_URL, params=params, timeout=(15, 35))
    except requests.RequestException as exc:
        raise BotError(f"Не удалось получить адрес скачивания из Облака Mail ({type(exc).__name__}).") from None

    if dispatch_response.status_code != 200:
        raise BotError(f"Служба Облака Mail вернула HTTP {dispatch_response.status_code}.")
    try:
        dispatch = dispatch_response.json()
        base_url = dispatch["body"]["weblink_get"][0]["url"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise BotError("Облако Mail не вернуло адрес скачивания файла.") from None

    base = urlparse(base_url)
    if base.scheme != "https" or not (
        base.hostname == "cloud.mail.ru"
        or (base.hostname and base.hostname.endswith(".cloud.mail.ru"))
        or (base.hostname and base.hostname.endswith(".datacloudmail.ru"))
    ):
        raise BotError("Облако Mail вернуло неожиданный адрес скачивания.")

    download_url = base_url.rstrip("/") + "/" + quote(file_id, safe="/")
    try:
        file_response = session.get(download_url, timeout=(20, 90))
    except requests.RequestException as exc:
        raise BotError(f"Не удалось скачать файл из Облака Mail ({type(exc).__name__}).") from None

    if file_response.status_code != 200:
        raise BotError(f"Скачивание из Облака Mail завершилось с HTTP {file_response.status_code}.")
    image = file_response.content
    if not image.startswith(PNG_SIGNATURE):
        raise BotError("Ссылка Облака Mail вернула не PNG-файл. Проверь формат расписания.")
    if len(image) > MAX_TELEGRAM_DOCUMENT_BYTES:
        raise BotError("PNG больше ограничения Telegram в 50 МБ.")

    filename = "raspisanie.png"
    disposition = file_response.headers.get("Content-Disposition", "")
    if "filename=" in disposition:
        raw_name = disposition.split("filename=", 1)[1].strip().strip('"')
        if raw_name.lower().endswith(".png"):
            filename = raw_name.replace("/", "_").replace("\\", "_")
    return image, filename


def telegram_request(
    session: requests.Session,
    method: str,
    fields: dict,
    file_field: str | None = None,
    file_bytes: bytes | None = None,
    filename: str | None = None,
) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise BotError("Не задан секрет TELEGRAM_BOT_TOKEN в настройках GitHub.")

    url = f"https://api.telegram.org/bot{token}/{method}"
    files = None
    if file_field and file_bytes is not None:
        files = {file_field: (filename or "raspisanie.png", file_bytes, "image/png")}

    try:
        response = session.post(url, data=fields, files=files, timeout=(15, 60))
    except requests.RequestException as exc:
        # Не выводим исходную ошибку requests: URL Bot API содержит токен.
        raise BotError(f"Не удалось связаться с Telegram ({method}, {type(exc).__name__}).") from None

    try:
        result = response.json()
    except ValueError:
        raise BotError(f"Telegram {method} вернул неожиданный ответ HTTP {response.status_code}.") from None

    if not result.get("ok"):
        description = result.get("description", "неизвестная ошибка")
        raise BotError(f"Telegram {method}: {description}")
    return result


def send_message(session: requests.Session, chat_id: str, text: str) -> None:
    telegram_request(session, "sendMessage", {"chat_id": chat_id, "text": text})


def send_schedule(session: requests.Session, image: bytes, filename: str) -> None:
    group_id = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "").strip()
    if not group_id:
        raise BotError("Не задан секрет TELEGRAM_GROUP_CHAT_ID в настройках GitHub.")

    if len(image) <= MAX_TELEGRAM_PHOTO_BYTES:
        telegram_request(
            session,
            "sendPhoto",
            {"chat_id": group_id},
            file_field="photo",
            file_bytes=image,
            filename=filename,
        )
    else:
        telegram_request(
            session,
            "sendDocument",
            {"chat_id": group_id},
            file_field="document",
            file_bytes=image,
            filename=filename,
        )


def is_final_check() -> bool:
    return os.environ.get("IS_FINAL_CHECK", "").strip().lower() == "true"


def should_run(now: datetime, final_check: bool) -> tuple[bool, bool]:
    """Вернуть (пора ли проверять, финальная ли это проверка дня)."""
    if now.weekday() == 6:  # Воскресенье
        return False, False

    minute = now.hour * 60 + now.minute
    start = 16 * 60
    end = 21 * 60
    if start <= minute <= end:
        return True, minute >= end

    # GitHub иногда начинает запланированную задачу с задержкой.
    # Финальная задача ещё выполняется до 21:30, если задержалась.
    if final_check and end < minute <= end + 30:
        return True, True
    return False, False


def process_once(session: requests.Session, state: dict, now: datetime, final_check: bool) -> None:
    run, is_final = should_run(now, final_check)
    if not run:
        log.info("Сейчас проверка не запланирована; завершение без действий.")
        return

    cloud_link = find_cloud_link(session)
    if cloud_link is None:
        log.info("На странице Дневника пока нет ссылки «ЗДЕСЬ».")
        if is_final:
            send_no_schedule_if_needed(session, state, now)
        state["active_error"] = None
        return

    image, filename = download_cloud_png(session, cloud_link)
    current_hash = hashlib.sha256(image).hexdigest()

    if current_hash != state.get("last_sent_sha256"):
        send_schedule(session, image, filename)
        state["last_sent_sha256"] = current_hash
        state["last_sent_date"] = now.date().isoformat()
        state["no_schedule_date"] = None
        log.info("Новое изображение расписания отправлено в группу.")
    elif is_final:
        send_no_schedule_if_needed(session, state, now)
    else:
        log.info("Изображение не изменилось; повторная отправка не нужна.")

    state["active_error"] = None


def send_no_schedule_if_needed(session: requests.Session, state: dict, now: datetime) -> None:
    today = now.date().isoformat()
    if state.get("last_sent_date") == today:
        log.info("Расписание уже отправлялось сегодня; сообщение об отсутствии не нужно.")
        return
    if state.get("no_schedule_date") == today:
        log.info("Сообщение об отсутствии расписания уже отправлено сегодня.")
        return

    group_id = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "").strip()
    if not group_id:
        raise BotError("Не задан секрет TELEGRAM_GROUP_CHAT_ID в настройках GitHub.")
    send_message(session, group_id, NO_SCHEDULE_TEXT)
    state["no_schedule_date"] = today
    log.info("Отправлено сообщение об отсутствии нового расписания.")


def notify_owner_once(session: requests.Session, state: dict, error_text: str) -> None:
    owner_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID", "").strip()
    if not owner_id:
        log.error("Не задан TELEGRAM_OWNER_CHAT_ID; ошибка останется только в журнале GitHub Actions.")
        return
    if state.get("active_error") == error_text:
        log.info("Такая же ошибка уже сообщалась; повторное личное сообщение пропускаю.")
        return

    try:
        send_message(session, owner_id, f"Не удалось проверить расписание: {error_text}")
    except Exception as exc:  # Ошибка уведомления не должна раскрыть токен в журнале.
        log.error("Не удалось отправить личное сообщение об ошибке (%s).", type(exc).__name__)
    state["active_error"] = error_text


def print_source_only(session: requests.Session) -> int:
    link = find_cloud_link(session)
    if link is None:
        print("На странице Дневника пока нет ссылки с текстом «ЗДЕСЬ».")
        return 1
    image, filename = download_cloud_png(session, link)
    digest = hashlib.sha256(image).hexdigest()
    print(f"Ссылка из Дневника: {link}")
    print(f"Файл: {filename}")
    print(f"Размер: {len(image):,} байт")
    print(f"SHA-256: {digest}")
    print("Файл скачан. Сообщения в Telegram не отправлялись.")
    return 0


def main() -> int:
    session = make_session()
    if "--source-only" in sys.argv:
        try:
            return print_source_only(session)
        except Exception as exc:
            print(f"Ошибка чтения источника: {exc}", file=sys.stderr)
            return 1

    try:
        state = load_state()
    except Exception as exc:
        log.error("%s", exc)
        return 1

    now = datetime.now(TIME_ZONE)
    final_check = is_final_check()
    exit_code = 0

    try:
        process_once(session, state, now, final_check)
    except Exception as exc:
        error_text = str(exc) or type(exc).__name__
        log.error("%s", error_text)
        notify_owner_once(session, state, error_text)
        exit_code = 1
    finally:
        try:
            save_state(state)
        except Exception as exc:
            log.error("Не удалось сохранить state.json (%s).", type(exc).__name__)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
