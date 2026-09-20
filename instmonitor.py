#!/usr/bin/env python3
"""
Instagram Direct -> Telegram монитор.

Логика:
  - логинимся в Instagram через instagrapi (неофициальная библиотека,
    эмулирует мобильное приложение) с сохранением сессии на диск,
    чтобы не логиниться заново каждый раз (это снижает риск блокировки
    и запроса challenge/2FA при каждом запуске);
  - раз в POLL_INTERVAL секунд запрашиваем тред с нужным контактом;
  - если пришло новое сообщение ОТ этого контакта, проверяем, не "прочитано"
    ли оно уже мной (last_seen_at) — если я только что был(а) в чате,
    Instagram обновит мою отметку "прочитано" раньше следующего опроса,
    и уведомление НЕ отправится;
  - если сообщение непрочитано — шлём в Telegram: ник контакта + тип контента.

ВАЖНО (риски):
  - Instagram не даёт официального API для мониторинга Direct личного
    аккаунта. instagrapi — неофициальное решение, нарушает ToS Instagram,
    есть риск временной блокировки/challenge. Снижаем риск так:
      * не опрашиваем чаще, чем раз в 45-90 сек (рандомный джиттер);
      * переиспользуем сессию (session.json), а не логинимся заново;
      * не запускаем несколько копий скрипта параллельно.
  - Если Instagram запросит challenge (подтверждение по email/SMS) —
    скрипт остановится с ошибкой, тогда нужно один раз вручную
    подтвердить вход через официальное приложение/сайт и перезапустить.

Установка зависимостей:
    pip install instagrapi requests

Перед первым запуском заполни блок CONFIG ниже (или вынеси в переменные
окружения / .env — см. комментарии).
"""

import json
import logging
import os
import random
import sys
import time
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import LoginRequired, TwoFactorRequired, ChallengeRequired
import requests

try:
    import pyotp
except ImportError:
    pyotp = None

# ============================== CONFIG ==============================

IG_USERNAME = os.environ.get("IG_USERNAME", "your_instagram_login")
IG_PASSWORD = os.environ.get("IG_PASSWORD", "your_instagram_password")

# Secret-ключ приложения-аутентификатора (TOTP), НЕ 6-значный код.
# Это та самая строка (обычно 16-32 символа, буквы+цифры), которую
# Instagram показывал при первой настройке 2FA через authenticator app —
# в виде текста под QR-кодом ("не можете отсканировать QR? введите код").
# Если ты её не сохранял(а), придётся отключить 2FA через authenticator
# и подключить заново, чтобы увидеть этот secret ещё раз.
IG_TOTP_SECRET = os.environ.get("IG_TOTP_SECRET", "")

# Ники (username, без @) людей, чьи сообщения нужно отслеживать.
# Через переменную окружения передавай через запятую: "nick1,nick2,nick3"
# Если хочешь зашить прямо в код (без переменных окружения) — раскомментируй
# строку ниже и впиши нужные ники СПИСКОМ строк, а не одной строкой:
# TARGET_USERNAMES = ["l.gold_ib", "another_nick"]
TARGET_USERNAMES = [
    u.strip()
    for u in os.environ.get("IG_TARGET_USERNAMES", "target_nickname_1,target_nickname_2").split(",")
    if u.strip()
]

TELEGRAM_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "123456:ABC-your-bot-token")
# Твой личный Telegram ID (админ) — единственный, кому бот шлёт уведомления.
# Узнать свой id можно, например, написав боту @userinfobot
TELEGRAM_ADMIN_ID = int(os.environ.get("TG_ADMIN_ID", "0"))

# Куда сохранять сессию Instagram (чтобы не логиниться каждый раз)
SESSION_FILE = Path("ig_session.json")

# Куда сохранять "последнее обработанное сообщение" (переживает рестарт)
STATE_FILE = Path("ig_monitor_state.json")

# Интервал опроса, сек. Реальный интервал = base +/- random jitter
POLL_INTERVAL_BASE = 60
POLL_INTERVAL_JITTER = 20  # итого 40-80 сек

# =====================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ig_monitor")

# Человекочитаемые названия типов контента Instagram Direct
CONTENT_TYPE_LABELS = {
    "text": "текст",
    "media_share": "репост поста",
    "photo": "фото",
    "video": "видео",
    "clip": "reels",
    "reel_share": "reels (поделились)",
    "story_share": "story (поделились)",
    "voice_media": "голосовое сообщение",
    "animated_media": "гифка/стикер",
    "like": "лайк (эмодзи)",
    "link": "ссылка",
    "video_call_event": "звонок",
    "raven_media": "исчезающее фото/видео",
}


def load_state() -> dict:
    """
    Формат: {"targets": {"username": {"last_item_id": "..."}}}
    """
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            data.setdefault("targets", {})
            return data
        except Exception:
            log.warning("Не удалось прочитать state-файл, начинаю с нуля")
    return {"targets": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_ADMIN_ID:
        log.error("TELEGRAM_ADMIN_ID не задан — некому слать уведомление")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_ADMIN_ID, "text": text},
            timeout=15,
        )
        if not resp.ok:
            log.error("Ошибка отправки в Telegram: %s", resp.text)
    except requests.RequestException as e:
        log.error("Сеть/Telegram недоступны: %s", e)


def get_totp_code() -> str:
    if not IG_TOTP_SECRET:
        return ""
    if pyotp is None:
        log.error("Задан IG_TOTP_SECRET, но не установлен пакет pyotp (pip install pyotp)")
        return ""
    # Secret часто копируют с пробелами/дефисами (его так показывают на экране
    # блоками по 4 символа) — чистим, иначе base32-декодирование упадёт с ошибкой
    cleaned_secret = "".join(ch for ch in IG_TOTP_SECRET if ch.isalnum()).upper()
    try:
        # Код действует ~30 сек, генерируем прямо перед использованием
        return pyotp.TOTP(cleaned_secret).now()
    except Exception as e:
        log.error(
            "IG_TOTP_SECRET похоже некорректен (не base32): %s. "
            "Проверь, что скопирован именно secret-ключ из настройки 2FA, "
            "а не 6-значный код из приложения.",
            e,
        )
        return ""


def _do_login(cl: Client) -> None:
    """Логин с поддержкой TOTP-2FA (код из приложения-аутентификатора)."""
    try:
        cl.login(IG_USERNAME, IG_PASSWORD)
    except TwoFactorRequired:
        code = get_totp_code()
        if not code:
            raise RuntimeError(
                "Instagram запросил код 2FA, а IG_TOTP_SECRET не задан или pyotp не установлен. "
                "Заполни IG_TOTP_SECRET (см. комментарий в CONFIG) и перезапусти."
            )
        log.info("Ввожу TOTP-код 2FA")
        cl.login(IG_USERNAME, IG_PASSWORD, verification_code=code)


def ig_login() -> Client:
    cl = Client()

    # Важно: если device fingerprint (settings) генерируется заново при
    # каждой попытке логина, для Instagram это выглядит как вход с кучи
    # разных "устройств" подряд — это резко повышает шанс блокировки/429.
    # Поэтому сохраняем settings СРАЗУ, ещё до успешного логина, и дальше
    # переиспользуем их при каждой попытке (в том числе при повторных
    # запусках скрипта после ошибки).
    if SESSION_FILE.exists():
        log.info("Загружаю сохранённые settings/сессию Instagram")
        cl.load_settings(SESSION_FILE)
    else:
        log.info("Первый запуск: генерирую и сохраняю device fingerprint")
        cl.dump_settings(SESSION_FILE)

    try:
        _do_login(cl)
        cl.get_timeline_feed()  # проверяем, что сессия реально рабочая
    except LoginRequired:
        log.info("Сессия протухла, логинюсь заново (fingerprint тот же)")
        cl.load_settings(SESSION_FILE)
        cl.settings["authorization_data"] = None
        _do_login(cl)
    except ChallengeRequired:
        log.error(
            "Instagram запросил challenge (подтверждение по email/SMS/приложению). "
            "Зайди в аккаунт вручную через официальное приложение Instagram, "
            "подтверди вход, затем перезапусти скрипт."
        )
        raise

    cl.dump_settings(SESSION_FILE)
    return cl


def get_content_label(message) -> str:
    item_type = getattr(message, "item_type", "text") or "text"
    return CONTENT_TYPE_LABELS.get(item_type, item_type)


def is_already_seen_by_me(thread, my_user_id: str, message_item_id: str) -> bool:
    """
    Проверяет, отмечено ли сообщение как прочитанное МНОЙ (то есть я
    был(а) в чате уже после его прихода). thread.last_seen_at — это
    Dict[str, LastSeenInfo], где LastSeenInfo.item_id — id последнего
    сообщения, которое я видел(а) в этом треде.
    """
    last_seen = getattr(thread, "last_seen_at", None) or {}
    my_seen = last_seen.get(str(my_user_id))
    if not my_seen:
        return False
    seen_item_id = getattr(my_seen, "item_id", None)
    if not seen_item_id:
        return False
    try:
        return int(seen_item_id) >= int(message_item_id)
    except (TypeError, ValueError):
        return False


def extract_thread_id(thread) -> str:
    """
    direct_thread_by_participants() в разных версиях/ответах Instagram
    может вернуть: объект DirectThread (атрибут .id), плоский dict
    ({"thread_id": ...}), или dict с вложенным ключом "thread"
    ({"thread": {"thread_id": ...}}). Перебираем все варианты.
    """
    if not isinstance(thread, dict):
        return str(getattr(thread, "id", "")) or ""

    if thread.get("thread_id"):
        return str(thread["thread_id"])
    if thread.get("id"):
        return str(thread["id"])

    nested = thread.get("thread")
    if isinstance(nested, dict):
        if nested.get("thread_id"):
            return str(nested["thread_id"])
        if nested.get("id"):
            return str(nested["id"])

    return ""


def build_targets(cl: Client) -> dict:
    """
    Резолвит username -> {user_id, thread_id} для каждого отслеживаемого
    контакта. Делается один раз при старте (и заново при релогине).
    """
    targets = {}
    for username in TARGET_USERNAMES:
        try:
            user_id = cl.user_id_from_username(username)
            thread = cl.direct_thread_by_participants([user_id])
            thread_id = extract_thread_id(thread)
            if not thread_id:
                raise RuntimeError(f"Не нашёл thread_id в ответе Instagram: {thread}")
            targets[username] = {"user_id": str(user_id), "thread_id": thread_id}
            log.info("Слежу за @%s (thread_id=%s)", username, thread_id)
        except Exception as e:
            log.error("Не удалось найти/открыть тред с @%s: %s", username, e)
    return targets


def main():
    if TELEGRAM_ADMIN_ID == 0:
        log.error("Заполни TELEGRAM_ADMIN_ID перед запуском (см. CONFIG)")
        sys.exit(1)
    if not TARGET_USERNAMES:
        log.error("Список TARGET_USERNAMES пуст — некого отслеживать")
        sys.exit(1)

    cl = ig_login()
    my_user_id = str(cl.user_id)
    targets = build_targets(cl)

    state = load_state()

    while True:
        try:
            for username, info in targets.items():
                target_user_id = info["user_id"]
                thread_id = info["thread_id"]

                thread = cl.direct_thread(thread_id)
                messages = sorted(thread.messages, key=lambda m: int(m.id))

                target_state = state["targets"].setdefault(username, {"last_item_id": None})
                last_item_id = target_state["last_item_id"]

                # первый запуск для этого контакта — не спамим историей,
                # просто запоминаем текущее последнее сообщение
                if last_item_id is None and messages:
                    target_state["last_item_id"] = messages[-1].id
                    save_state(state)
                    log.info("[%s] первый запуск: беру за точку отсчёта последнее сообщение", username)
                    continue

                new_messages = [
                    m for m in messages
                    if last_item_id is None or int(m.id) > int(last_item_id)
                ]

                for message in new_messages:
                    target_state["last_item_id"] = message.id

                    # уведомляем только о сообщениях ОТ контакта, не о своих
                    if str(message.user_id) != target_user_id:
                        continue

                    if is_already_seen_by_me(thread, my_user_id, message.id):
                        log.info("[%s] сообщение %s уже прочитано мной, пропуск", username, message.id)
                        continue

                    content_label = get_content_label(message)
                    text = f"📩 {username}\nТип: {content_label}"
                    send_telegram_message(text)
                    log.info("[%s] отправлено уведомление: %s", username, content_label)

                save_state(state)
                time.sleep(random.uniform(2, 5))  # не долбим Instagram запросами пачкой

        except LoginRequired:
            log.warning("Сессия истекла, перелогиниваюсь")
            cl = ig_login()
            my_user_id = str(cl.user_id)
            targets = build_targets(cl)
        except Exception as e:
            log.exception("Ошибка в цикле опроса: %s", e)

        sleep_time = POLL_INTERVAL_BASE + random.uniform(-POLL_INTERVAL_JITTER, POLL_INTERVAL_JITTER)
        time.sleep(max(15, sleep_time))


if __name__ == "__main__":
    main()
