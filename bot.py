"""Bilingual Telegram group-call media player (Pyrogram + PyTgCalls)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Optional

from aiohttp import web
from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Message,
)

from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import AudioQuality, MediaStream, StreamEnded, VideoQuality
from commands import normalize_command
import yt_dlp


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("mediabot")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable is missing: {name}"
        )
    return value


API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")
SESSION_STRING = required_env("SESSION_STRING")

CLEAN_COMMANDS = os.getenv(
    "CLEAN_COMMANDS", "true"
).lower() in {"1", "true", "yes", "on"}

CLEAN_OLD_STATUS = os.getenv(
    "CLEAN_OLD_STATUS", "true"
).lower() in {"1", "true", "yes", "on"}

STATUS_TTL = max(10, int(os.getenv("STATUS_TTL", "60")))
MAX_QUEUE = max(1, min(100, int(os.getenv("MAX_QUEUE", "25"))))

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/tmp/mediabot_media"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

MAX_TELEGRAM_MEDIA_MB = max(
    1, int(os.getenv("MAX_TELEGRAM_MEDIA_MB", "500"))
)

bot = Client(
    "mediabot-controller",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

assistant = Client(
    "mediabot-assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

calls = PyTgCalls(assistant)


@dataclass
class Track:
    query: str
    title: str
    stream_url: str
    webpage_url: str
    duration: Optional[int]
    video: bool
    requested_by: str
    local_file: Optional[str] = None


@dataclass
class Room:
    queue: Deque[Track] = field(default_factory=deque)
    current: Optional[Track] = None
    paused: bool = False
    started_at: float = 0.0
    offset: int = 0
    status_message_id: Optional[int] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    suppress_stream_end_until: float = 0.0


rooms: dict[int, Room] = defaultdict(Room)


def fmt_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "مباشر / Live"

    seconds = max(0, int(seconds))

    if seconds >= 3600:
        return (
            f"{seconds // 3600}:"
            f"{(seconds % 3600) // 60:02}:"
            f"{seconds % 60:02}"
        )

    return f"{seconds // 60}:{seconds % 60:02}"


def get_track_info(query: str, video: bool, requested_by: str) -> Track:
    if not query:
        raise ValueError(
            "أرسل رابطًا أو اسم المقطع بعد الأمر.\n"
            "Send a URL or search phrase after the command."
        )

    target = (
        query
        if re.match(r"https?://", query, re.I)
        else f"ytsearch1:{query}"
    )

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "best[ext=mp4]/best" if video else "bestaudio/best",
        "skip_download": True,
        "default_search": "ytsearch1",
        "socket_timeout": 20,
        "extractor_retries": 2,
        "retries": 2,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)

        if info and "entries" in info:
            info = next(
                (entry for entry in info["entries"] if entry),
                None,
            )

        if not info:
            raise ValueError(
                "لم أجد مقطعًا صالحًا. / No playable media was found."
            )

        stream_url = info.get("url")

        if not stream_url:
            formats = info.get("formats") or []
            wanted = [
                item
                for item in formats
                if item.get("url")
                and (video or item.get("acodec") != "none")
            ]

            if not wanted:
                raise ValueError(
                    "تعذّر استخراج رابط البث. / "
                    "Could not resolve a stream URL."
                )

            stream_url = wanted[-1]["url"]

        webpage = info.get("webpage_url") or query

        return Track(
            query=query,
            title=(info.get("title") or query)[:180],
            stream_url=stream_url,
            webpage_url=webpage,
            duration=info.get("duration"),
            video=video,
            requested_by=requested_by[:80],
        )


def stream_for(track: Track, offset: int = 0) -> MediaStream:
    kwargs = {}

    if offset > 0:
        kwargs["ffmpeg_parameters"] = f"-ss {int(offset)}"

    if track.video:
        return MediaStream(
            track.stream_url,
            AudioQuality.HIGH,
            VideoQuality.HD_720p,
            **kwargs,
        )

    return MediaStream(
        track.stream_url,
        AudioQuality.HIGH,
        video_flags=MediaStream.Flags.IGNORE,
        **kwargs,
    )


def telegram_media_kind(
    message: Message,
) -> tuple[bool, object, str, Optional[int]]:
    """Identify supported audio and video media in a Telegram message."""

    for attr, is_video in (
        ("video", True),
        ("video_note", True),
        ("animation", True),
        ("audio", False),
        ("voice", False),
        ("document", False),
    ):
        media = getattr(message, attr, None)

        if media is None:
            continue

        mime = (getattr(media, "mime_type", None) or "").lower()

        if attr == "document":
            if mime.startswith("video/"):
                is_video = True
            elif mime.startswith("audio/"):
                is_video = False
            else:
                continue

        title = (
            getattr(media, "file_name", None)
            or getattr(media, "title", None)
            or getattr(message, "caption", None)
            or ("Telegram video" if is_video else "Telegram audio")
        )

        duration = getattr(media, "duration", None)

        return is_video, media, str(title)[:180], duration

    raise ValueError(
        "الرسالة التي رددت عليها لا تحتوي ملفًا صوتيًا أو فيديو مدعومًا.\n"
        "Reply to a Telegram audio or video message."
    )


async def resolve_telegram_media(
    source_message: Message,
    requested_by: str,
) -> Track:
    """Download a replied-to Telegram audio/video for local playback."""

    is_video, _media, title, duration = telegram_media_kind(
        source_message
    )

    media_message = await assistant.get_messages(
        source_message.chat.id,
        source_message.id,
    )

    supported_attrs = (
        "audio",
        "voice",
        "video",
        "video_note",
        "animation",
        "document",
    )

    if not media_message or not any(
        getattr(media_message, attr, None)
        for attr in supported_attrs
    ):
        raise ValueError(
            "تعذّر على الحساب المساعد الوصول إلى الملف. "
            "أضفه إلى المجموعة ثم أعد المحاولة.\n"
            "Assistant cannot access this media; add it to the group "
            "and retry."
        )

    media_obj = next(
        (
            getattr(media_message, attr, None)
            for attr in (
                "video",
                "video_note",
                "animation",
                "audio",
                "voice",
                "document",
            )
            if getattr(media_message, attr, None) is not None
        ),
        None,
    )

    size = getattr(media_obj, "file_size", 0) or 0

    if size > MAX_TELEGRAM_MEDIA_MB * 1024 * 1024:
        raise ValueError(
            f"حجم الملف يتجاوز الحد المسموح "
            f"({MAX_TELEGRAM_MEDIA_MB} MB).\n"
            "Telegram media exceeds the configured size limit."
        )

    local_path = await assistant.download_media(
        media_message,
        file_name=str(MEDIA_DIR) + os.sep,
    )

    if not local_path or not os.path.isfile(local_path):
        raise ValueError(
            "فشل تنزيل ملف تيليجرام. / Failed to download Telegram media."
        )

    link = getattr(source_message, "link", None) or "https://t.me/"

    return Track(
        query=link,
        title=title,
        stream_url=local_path,
        webpage_url=link,
        duration=duration,
        video=is_video,
        requested_by=requested_by[:80],
        local_file=local_path,
    )


async def resolve_track(
    query: str,
    video: bool,
    requested_by: str,
    source_message: Optional[Message] = None,
) -> Track:
    if source_message is not None:
        return await resolve_telegram_media(
            source_message,
            requested_by,
        )

    return await asyncio.to_thread(
        get_track_info,
        query,
        video,
        requested_by,
    )


def cleanup_track_file(track: Optional[Track]) -> None:
    if track and track.local_file:
        try:
            Path(track.local_file).unlink(missing_ok=True)
        except OSError as exc:
            log.warning(
                "Could not remove temporary media file: %s",
                exc,
            )


async def delete_safely(message: Message) -> None:
    try:
        await message.delete()
    except Exception as exc:
        log.debug(
            "Could not delete message %s: %s",
            getattr(message, "id", "?"),
            exc,
        )


async def send_status(
    chat_id: int,
    text: str,
    keyboard: bool = False,
) -> Optional[Message]:
    room = rooms[chat_id]

    if CLEAN_OLD_STATUS and room.status_message_id:
        try:
            await bot.delete_messages(
                chat_id,
                room.status_message_id,
            )
        except Exception:
            pass

    markup = None

    if keyboard:
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⏸ إيقاف مؤقت / Pause",
                        callback_data="pause",
                    ),
                    InlineKeyboardButton(
                        "▶️ استئناف / Resume",
                        callback_data="resume",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "⏭ التالي / Next",
                        callback_data="next",
                    ),
                    InlineKeyboardButton(
                        "⏹ إيقاف / Stop",
                        callback_data="stop",
                    ),
                ],
            ]
        )

    msg = await bot.send_message(
        chat_id,
        text,
        reply_markup=markup,
        disable_web_page_preview=True,
    )

    room.status_message_id = msg.id

    if STATUS_TTL:

        async def expire():
            await asyncio.sleep(STATUS_TTL)

            current = rooms.get(chat_id)

            if current and current.status_message_id == msg.id:
                try:
                    await bot.delete_messages(chat_id, msg.id)
                    current.status_message_id = None
                except Exception:
                    pass

        asyncio.create_task(expire())

    return msg


async def start_track(
    chat_id: int,
    track: Track,
    offset: int = 0,
) -> None:
    room = rooms[chat_id]
    previous = room.current

    if previous is not None:
        room.suppress_stream_end_until = time.monotonic() + 3.0

    await calls.play(chat_id, stream_for(track, offset))

    if previous is not None and previous.local_file != track.local_file:
        cleanup_track_file(previous)

    room.current = track
    room.paused = False
    room.offset = offset
    room.started_at = time.monotonic()

    mode = (
        "فيديو + صوت / Video + audio"
        if track.video
        else "صوت / Audio"
    )

    await send_status(
        chat_id,
        f"▶️ **يعمل الآن / Now playing**\n"
        f"[{track.title}]({track.webpage_url})\n"
        f"النوع / Mode: {mode}\n"
        f"المدة / Duration: {fmt_duration(track.duration)}\n"
        f"طلبه / Requested by: {track.requested_by}",
        keyboard=True,
    )


async def play_next(chat_id: int) -> None:
    room = rooms[chat_id]

    async with room.lock:
        if not room.queue:
            cleanup_track_file(room.current)
            room.current = None
            room.paused = False

            try:
                await calls.leave_call(chat_id)
            except Exception:
                try:
                    await calls.stop(chat_id)
                except Exception:
                    pass

            await send_status(
                chat_id,
                "انتهت قائمة الانتظار. / Queue finished.",
            )
            return

        track = room.queue.popleft()

        try:
            await start_track(chat_id, track)
        except Exception as exc:
            log.exception("Could not start queued track")

            cleanup_track_file(track)

            await send_status(
                chat_id,
                f"تعذّر تشغيل {track.title}: {str(exc)[:180]}\n"
                "Skipping to next item…",
            )

            asyncio.create_task(play_next(chat_id))


async def do_play(
    message: Message,
    query: str,
    video: bool,
) -> None:
    chat_id = message.chat.id
    room = rooms[chat_id]

    who = (
        message.from_user.first_name
        if message.from_user
        else "User"
    )

    status = await send_status(
        chat_id,
        "⏳ جارٍ تجهيز المقطع… / Resolving media…",
    )

    try:
        source_message = (
            message.reply_to_message
            if message.reply_to_message
            and any(
                getattr(message.reply_to_message, attr, None) is not None
                for attr in (
                    "audio",
                    "voice",
                    "video",
                    "video_note",
                    "animation",
                    "document",
                )
            )
            else None
        )

        if not query and source_message is None:
            raise ValueError(
                "أرسل رابطًا/اسمًا أو ردّ على رسالة صوت/فيديو.\n"
                "Provide a URL/search or reply to an audio/video message."
            )

        track = await resolve_track(
            query,
            video,
            who,
            source_message=source_message,
        )

        async with room.lock:
            if room.current is None:
                await start_track(chat_id, track)

                if status:
                    try:
                        await bot.delete_messages(chat_id, status.id)
                    except Exception:
                        pass

            elif len(room.queue) >= MAX_QUEUE:
                cleanup_track_file(track)

                await send_status(
                    chat_id,
                    f"قائمة الانتظار ممتلئة (الحد {MAX_QUEUE}).\n"
                    "Queue is full.",
                )

            else:
                room.queue.append(track)

                await send_status(
                    chat_id,
                    f"➕ أُضيف إلى القائمة / Added to queue: "
                    f"**{track.title}**\n"
                    f"المركز / Position: {len(room.queue)}",
                )

    except Exception as exc:
        log.info("Resolve/play error: %s", exc)

        await send_status(
            chat_id,
            f"❌ تعذّر تجهيز المقطع. / Could not prepare media.\n"
            f"{str(exc)[:350]}",
        )

    finally:
        if CLEAN_COMMANDS:
            await delete_safely(message)


HELP_TEXT = """🎵 **Mediabot — أوامر التشغيل / Playback commands**

`/play <اسم أو رابط>` أو `تشغيل <اسم أو رابط>` — تشغيل الصوت / Play audio
`/video <اسم أو رابط>` أو `فيديو <اسم أو رابط>` — بث الفيديو / Play video
`/queue` أو `قائمة` — قائمة الانتظار / Queue
`/now` أو `الآن` — المقطع الحالي / Now playing
`/next` أو `التالي` — التالي / Next
`/pause` أو `إيقاف_مؤقت` — إيقاف مؤقت / Pause
`/resume` أو `استئناف` — استئناف / Resume
`/back` أو `رجوع` — الرجوع 10 ثوانٍ / Back 10 seconds
`/forward` أو `تقديم` — التقديم 10 ثوانٍ / Forward 10 seconds
`/stop` أو `إيقاف` — إيقاف التشغيل / Stop playback
`/leave` أو `مغادرة` — مغادرة المكالمة / Leave call

يمكن إرسال الأوامر بشرطة مائلة أو دونها.
Commands may be sent with or without `/`.

لتشغيل ملف من تيليجرام: ردّ على رسالة الصوت أو الفيديو بالأمر `تشغيل` أو `فيديو`.
To play Telegram media: reply to an audio/video message with `play` or `video`.

يجب أن تكون هناك محادثة صوتية/مرئية نشطة، وأن يكون الحساب المساعد قادرًا على الانضمام.
A group voice/video chat must be active and the assistant account must be allowed to join."""


async def command_handler(
    _: Client,
    message: Message,
) -> None:
    if (
        not message.text
        or message.chat.type
        not in (ChatType.GROUP, ChatType.SUPERGROUP)
    ):
        return

    cmd, arg = normalize_command(message.text)

    if not cmd:
        return

    chat_id = message.chat.id
    room = rooms[chat_id]

    if cmd == "play":
        await do_play(message, arg, False)
        return

    if cmd == "video":
        await do_play(message, arg, True)
        return

    try:
        if cmd == "help":
            await send_status(chat_id, HELP_TEXT)

        elif cmd == "queue":
            lines = [
                f"🎵 **قائمة الانتظار / Queue ({len(room.queue)})**"
            ]

            if room.current:
                lines.append(
                    f"▶️ الحالي / Current: {room.current.title}"
                )

            lines.extend(
                f"{i}. {track.title} ({fmt_duration(track.duration)})"
                for i, track in enumerate(list(room.queue)[:15], 1)
            )

            if not room.current and not room.queue:
                lines.append("القائمة فارغة / Queue is empty")

            await send_status(chat_id, "\n".join(lines))

        elif cmd == "now":
            if room.current:
                await send_status(
                    chat_id,
                    f"▶️ **الآن / Now playing**\n"
                    f"[{room.current.title}]"
                    f"({room.current.webpage_url})\n"
                    f"{fmt_duration(room.current.duration)}",
                    keyboard=True,
                )
            else:
                await send_status(
                    chat_id,
                    "لا يوجد مقطع يعمل الآن. / Nothing is playing.",
                )

        elif cmd == "pause":
            if room.current and not room.paused:
                await calls.pause(chat_id)
                room.paused = True
                room.offset += int(
                    time.monotonic() - room.started_at
                )

                await send_status(
                    chat_id,
                    "⏸ أُوقف مؤقتًا / Playback paused.",
                    keyboard=True,
                )
            else:
                await send_status(
                    chat_id,
                    "لا يوجد مقطع يعمل أو أنه متوقف مؤقتًا بالفعل.\n"
                    "Nothing to pause.",
                )

        elif cmd == "resume":
            if room.current and room.paused:
                await calls.resume(chat_id)
                room.paused = False
                room.started_at = time.monotonic()

                await send_status(
                    chat_id,
                    "▶️ استؤنف التشغيل / Playback resumed.",
                    keyboard=True,
                )
            else:
                await send_status(
                    chat_id,
                    "لا يوجد تشغيل متوقف مؤقتًا. / Nothing is paused.",
                )

        elif cmd in ("back", "forward"):
            if not room.current:
                await send_status(
                    chat_id,
                    "لا يوجد مقطع يعمل. / Nothing is playing.",
                )

            elif room.current.duration is None:
                await send_status(
                    chat_id,
                    "لا يدعم المقطع المباشر التقديم والرجوع.\n"
                    "Seeking is unavailable for live streams.",
                )

            else:
                elapsed = (
                    0
                    if room.paused
                    else int(time.monotonic() - room.started_at)
                )

                change = -10 if cmd == "back" else 10

                position = max(
                    0,
                    room.offset + elapsed + change,
                )

                if room.current.duration:
                    position = min(
                        position,
                        max(0, room.current.duration - 1),
                    )

                track = room.current
                await start_track(chat_id, track, position)

        elif cmd == "next":
            await play_next(chat_id)

        elif cmd == "stop":
            try:
                await calls.leave_call(chat_id)
            except Exception:
                try:
                    await calls.stop(chat_id)
                except Exception:
                    pass

            cleanup_track_file(room.current)

            for queued_track in room.queue:
                cleanup_track_file(queued_track)

            room.queue.clear()
            room.current = None
            room.paused = False
            room.offset = 0

            await send_status(
                chat_id,
                "⏹ أُوقف التشغيل ومُسحت القائمة.\n"
                "Playback stopped and queue cleared.",
            )

        elif cmd == "leave":
            try:
                await calls.leave_call(chat_id)
            except Exception:
                pass

            cleanup_track_file(room.current)

            for queued_track in room.queue:
                cleanup_track_file(queued_track)

            room.queue.clear()
            room.current = None
            room.paused = False

            await send_status(
                chat_id,
                "👋 غادر الحساب المساعد المكالمة إن كان متصلًا.\n"
                "Assistant left the call if connected.",
            )

    except Exception as exc:
        log.exception("Command failed: %s", cmd)

        await send_status(
            chat_id,
            f"❌ حدث خطأ أثناء تنفيذ الأمر. / Command failed.\n"
            f"{str(exc)[:250]}",
        )

    finally:
        if CLEAN_COMMANDS:
            await delete_safely(message)


@bot.on_message(filters.group & filters.text)
async def on_group_text(
    client: Client,
    message: Message,
) -> None:
    await command_handler(client, message)


@bot.on_callback_query()
async def on_button(
    _: Client,
    query: CallbackQuery,
) -> None:
    if not query.message or not query.message.chat:
        await query.answer()
        return

    chat_id = query.message.chat.id

    if query.message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        await query.answer("Groups only", show_alert=True)
        return

    action = query.data
    room = rooms[chat_id]

    try:
        if action == "pause" and room.current and not room.paused:
            await calls.pause(chat_id)
            room.paused = True
            room.offset += int(
                time.monotonic() - room.started_at
            )

        elif action == "resume" and room.current and room.paused:
            await calls.resume(chat_id)
            room.paused = False
            room.started_at = time.monotonic()

        elif action == "next":
            await play_next(chat_id)

        elif action == "stop":
            try:
                await calls.leave_call(chat_id)
            except Exception:
                try:
                    await calls.stop(chat_id)
                except Exception:
                    pass

            cleanup_track_file(room.current)

            for queued_track in room.queue:
                cleanup_track_file(queued_track)

            room.queue.clear()
            room.current = None
            room.paused = False

            await send_status(
                chat_id,
                "⏹ أُوقف التشغيل ومُسحت القائمة.\n"
                "Playback stopped and queue cleared.",
            )

        await query.answer("تم / Done")

    except Exception as exc:
        log.exception("Button action failed")

        await query.answer(
            f"تعذّر التنفيذ / Failed: {str(exc)[:120]}",
            show_alert=True,
        )


@calls.on_update(call_filters.stream_end())
async def on_stream_end(
    _: PyTgCalls,
    update: StreamEnded,
) -> None:
    chat_id = update.chat_id
    room = rooms.get(chat_id)

    if not room or not room.current:
        return

    if time.monotonic() < room.suppress_stream_end_until:
        return

    log.info(
        "Stream ended in chat %s; advancing queue",
        chat_id,
    )

    asyncio.create_task(play_next(chat_id))


async def health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "service": "mediabot",
            "rooms": len(rooms),
        }
    )


async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        int(os.getenv("PORT", "10000")),
    )

    await site.start()
    return runner


async def main() -> None:
    runner = None

    try:
        runner = await start_health_server()

        await assistant.start()
        log.info("Assistant account client started")

        await calls.start()
        log.info("PyTgCalls started")

        await bot.start()
        log.info("Controller bot started")

        await asyncio.Event().wait()

    finally:
        if runner:
            await runner.cleanup()

        try:
            await bot.stop()
        except Exception:
            pass

        try:
            await calls.stop()
        except Exception:
            pass

        try:
            await assistant.stop()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
