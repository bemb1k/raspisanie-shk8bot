from __future__ import annotations

import requests


def main() -> int:
    token = input("Вставь токен бота (он будет виден в окне): ").strip()
    if not token:
        print("Токен пустой.")
        return 1

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        response = requests.get(url, timeout=(10, 30))
        data = response.json()
    except Exception as exc:
        # Не показываем исходную ошибку, чтобы токен не попал в текст ошибки.
        print(f"Не удалось получить обновления Telegram ({type(exc).__name__}).")
        return 1

    if not data.get("ok"):
        print(f"Telegram вернул ошибку: {data.get('description', 'неизвестная ошибка')}")
        print("Токен не выводился на экран.")
        return 1

    found = set()
    for update in data.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            item = update.get(key)
            if not item:
                continue
            chat = item.get("chat")
            if not chat:
                continue
            chat_id = str(chat.get("id", ""))
            if not chat_id or chat_id in found:
                continue
            found.add(chat_id)
            title = chat.get("title") or chat.get("first_name") or "(без названия)"
            chat_type = chat.get("type", "неизвестный тип")
            print(f"Название: {title}")
            print(f"Тип: {chat_type}")
            print(f"ID: {chat_id}")
            print()

    if not found:
        print("Telegram пока не передал боту сообщений.")
        print("Открой личный чат с ботом и нажми Start.")
        print("Для ID группы добавь бота в группу и отправь /start@raspisanie_shk8_bot.")
        print("Затем запусти эту программу ещё раз.")
        return 2

    print("Скопируй свой личный ID и ID школьной группы в секреты GitHub.")
    print("Если список старый, сначала отправь боту новое сообщение и запусти программу снова.")
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except KeyboardInterrupt:
        print("\nПрограмма остановлена.")
        exit_code = 130
    except Exception as exc:
        # Do not print the exception text; it could contain the bot token.
        print(f"Неожиданная ошибка ({type(exc).__name__}).")
        exit_code = 1
    input("\nНажми Enter, чтобы завершить программу...")
    raise SystemExit(exit_code)
