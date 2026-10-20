"""Bilingual Telegram group-call media player (Pyrogram + PyTgCalls)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
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
    BotCommand,
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
PENDING_REQUEST_TTL = max(15, int(os.getenv("PENDING_REQUEST_TTL", "90")))

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/tmp/mediabot_media"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

MAX_TELEGRAM_MEDIA_MB = max(
    1, int(os.getenv("MAX_TELEGRAM_MEDIA_MB", "500"))
)
MAX_YTDLP_DOWNLOAD_MB = max(
    25, int(os.getenv("MAX_YTDLP_DOWNLOAD_MB", "500"))
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
# A bare play command can be completed by the same user's next message.
# The chat and user IDs together prevent capturing another member's request.
pending_requests: dict[tuple[int, int], tuple[bool, float]] = {}


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
    """Download media locally before playback to avoid fragile/expired stream URLs."""
    if not query:
        raise ValueError(
            "أرسل رابطًا أو اسم المقطع بعد الأمر.\n"
            "أرسل رابط يوتيوب أو اكتب اسم المقطع بعد الأمر.\n"
            "Send a YouTube URL or search phrase after the command."
        )

    target = (
        query
        if re.match(r"https?://", query, re.I)
        else f"ytsearch1:{query}"
    )
    download_dir = Path(tempfile.mkdtemp(prefix="youtube_", dir=str(MEDIA_DIR)))
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            if video else "bestaudio/best"
        ),
        "merge_output_format": "mp4" if video else None,
        "outtmpl": str(download_dir / "%(title).80s [%(id)s].%(ext)s"),
        "max_filesize": MAX_YTDLP_DOWNLOAD_MB * 1024 * 1024,
        "default_search": "ytsearch1",
        "socket_timeout": 30,
        "extractor_retries": 3,
        "retries": 3,
        "fragment_retries": 3,
    }
    if not video:
        # Telegram's send_audio expects a supported audio container; normalize to MP3.
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }]
    opts = {key: value for key, value in opts.items() if value is not None}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
            if info and "entries" in info:
                info = next((entry for entry in info["entries"] if entry), None)
            if not info:
                raise ValueError(
                    "لم أجد مقطعًا صالحًا. تحقق من الرابط أو جرّب اسمًا آخر.\n"
                    "No media found. Check the URL or try another search."
                )
            if info.get("_type") == "playlist":
                raise ValueError("أرسل رابط مقطع واحد، لا قائمة تشغيل. / Please send one video, not a playlist.")
            title = (info.get("title") or query)[:180]
            webpage = info.get("webpage_url") or query
            duration = info.get("duration")
            ydl.download([target])

        supported = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus", ".ogg", ".aac"}
        files = [p for p in download_dir.rglob("*") if p.is_file() and p.suffix.lower() in supported and not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise ValueError("انتهى التنزيل دون العثور على ملف وسائط صالح. / Download finished but no media file was found.")
        local_path = max(files, key=lambda p: p.stat().st_mtime)
        size_mb = local_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_YTDLP_DOWNLOAD_MB:
            raise ValueError(
                f"حجم الملف {size_mb:.1f} MB ويتجاوز الحد {MAX_YTDLP_DOWNLOAD_MB} MB. "
                "جرّب مقطعًا أقصر أو اضبط MAX_YTDLP_DOWNLOAD_MB. / Media exceeds the configured download limit."
            )
        return Track(
            query=query,
            title=title,
            stream_url=str(local_path),
            webpage_url=webpage,
            duration=duration,
            video=video,
            requested_by=requested_by[:80],
            local_file=str(local_path),
        )
    except Exception:
        shutil.rmtree(download_dir, ignore_errors=True)
        raise


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
        path = Path(track.local_file)
        try:
            path.unlink(missing_ok=True)
            # YouTube downloads get an isolated child directory; Telegram downloads
            # remain directly under MEDIA_DIR and must not remove that shared folder.
            if path.parent != MEDIA_DIR and path.parent.parent == MEDIA_DIR:
                path.parent.rmdir()
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


def has_supported_media_reply(message: Message) -> bool:
    """Return whether the replied-to message contains supported audio/video media."""
    if not message.reply_to_message:
        return False
    try:
        telegram_media_kind(message.reply_to_message)
        return True
    except ValueError:
        return False


def friendly_media_error(exc: Exception) -> str:
    """Turn common upstream extractor failures into actionable user guidance."""
    detail = str(exc)
    lowered = detail.lower()
    if "sign in to confirm" in lowered or "confirm you’re not a bot" in lowered or "confirm you're not a bot" in lowered:
        return (
            "رفض يوتيوب الطلب من خادم الاستضافة، لذلك تعذّر جلب هذا المقطع. "
            "جرّب رابطًا مباشرًا لملف صوت/فيديو من مصدر مسموح، أو أرسل الملف إلى تيليجرام "
            "ثم استخدم الأمر بالرد على رسالته. لا يمكن ضمان تشغيل كل روابط يوتيوب من الخادم.\n"
            "YouTube blocked this server request. Try a direct media URL or reply to an audio/video file uploaded to Telegram."
        )
    if "ffmpeg" in lowered and ("not found" in lowered or "no such file" in lowered or "not installed" in lowered):
        return "أداة FFmpeg غير متاحة لمعالجة الملف. / FFmpeg is unavailable for media processing."
    return detail[:350]


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
            f"{friendly_media_error(exc)}",
        )

    finally:
        if CLEAN_COMMANDS:
            await delete_safely(message)


HELP_TEXT = """🎵 **Mediabot — مساعدك للصوت والفيديو**

**في الخاص / In private chat**
• `/play <اسم أو رابط>` — تنزيل الصوت وإرساله إليك هنا.
• `/video <رابط أو اسم>` — تنزيل الفيديو وإرساله داخل المحادثة.
• يمكنك أيضًا الرد على ملف صوتي أو فيديو أرسلته للبوت بكلمة `تشغيل` أو `فيديو`.

**في المجموعات / In groups**
• `/play <اسم أو رابط>` — تشغيل الصوت داخل المكالمة الجماعية.
• `/video <رابط أو اسم>` — تشغيل الفيديو والصوت داخل المكالمة.
• `/queue` و`/now` — عرض قائمة الانتظار والمقطع الحالي.
• `/pause` و`/resume` — إيقاف التشغيل مؤقتًا واستئنافه.
• `/back` و`/forward` — الرجوع أو التقديم 10 ثوانٍ.
• `/next` و`/stop` و`/leave` — تخطي المقطع أو إيقاف التشغيل أو مغادرة المكالمة.

**ملاحظات**
في الخاص، تُرسل الوسائط كرسائل تيليجرام؛ استخدم مشغّل تيليجرام للتحكم في تشغيلها. لا يستطيع البوت إيقاف تشغيل الوسائط بعد إرسالها.
في المجموعات، يلزم وجود مكالمة صوتية/مرئية نشطة والسماح للحساب المساعد بالانضمام.

You can send commands with or without a leading slash. Private-chat media is delivered as Telegram messages; use Telegram's media player controls. Group-call playback requires an active group call."""


async def do_play_private(
    message: Message,
    query: str,
    video: bool,
) -> None:
    """Resolve a media item and send it as a Telegram message in private chat."""
    chat_id = message.chat.id
    status = await bot.send_message(
        chat_id,
        "⏳ أجهّز الوسائط الآن؛ قد يستغرق تنزيل الفيديو بعض الوقت.\n"
        "Preparing your media; video downloads may take a little while.",
    )
    track: Optional[Track] = None
    try:
        source_message = (
            message.reply_to_message
            if message.reply_to_message
            and any(
                getattr(message.reply_to_message, attr, None) is not None
                for attr in ("audio", "voice", "video", "video_note", "animation", "document")
            )
            else None
        )
        if not query and source_message is None:
            raise ValueError(
                "أرسل اسم المقطع أو رابطه، أو ردّ على رسالة تحتوي صوتًا أو فيديو.\n"
                "Send a media name/URL or reply to an audio/video message."
            )

        requester = message.from_user.first_name if message.from_user else "User"
        if source_message is not None:
            is_video, media, title, duration = telegram_media_kind(source_message)
            size = getattr(media, "file_size", 0) or 0
            if size > MAX_TELEGRAM_MEDIA_MB * 1024 * 1024:
                raise ValueError(
                    f"حجم الملف يتجاوز الحد المضبوط ({MAX_TELEGRAM_MEDIA_MB} MB). "
                    "اختر ملفًا أصغر. / Telegram media exceeds the configured size limit."
                )
            local_path = await bot.download_media(
                source_message,
                file_name=str(MEDIA_DIR) + os.sep,
            )
            if not local_path or not os.path.isfile(local_path):
                raise ValueError(
                    "لم أتمكن من تنزيل الملف المرفق. تأكد أنه ما زال متاحًا ثم أعد المحاولة.\n"
                    "Could not download the attached file; please try again."
                )
            track = Track(
                query="Telegram attachment",
                title=title,
                stream_url=str(local_path),
                webpage_url=getattr(source_message, "link", None) or "https://t.me/",
                duration=duration,
                video=is_video,
                requested_by=requester[:80],
                local_file=str(local_path),
            )
        else:
            track = await asyncio.to_thread(
                get_track_info, query, video, requester
            )

        path = Path(track.local_file or track.stream_url)
        if not path.is_file():
            raise ValueError(
                "لم يُعثر على ملف الوسائط بعد التجهيز. أعد المحاولة برابط آخر.\n"
                "The prepared media file is missing; please try another URL."
            )

        size_mb = path.stat().st_size / (1024 * 1024)
        # Cloud Bot API upload limit is commonly 50 MB; stay below it for reliability.
        private_limit_mb = max(1, min(49, int(os.getenv("MAX_PRIVATE_UPLOAD_MB", "45"))))
        if size_mb > private_limit_mb:
            raise ValueError(
                f"حجم الملف {size_mb:.1f} MB، والحد الآمن للإرسال في الخاص "
                f"{private_limit_mb} MB. اختر مقطعًا أقصر أو اضبط MAX_PRIVATE_UPLOAD_MB.\n"
                "The file exceeds the configured private-upload limit."
            )

        caption = (
            f"{'🎬' if track.video else '🎵'} {track.title[:900]}\n"
            f"المدة: {fmt_duration(track.duration)}\n"
            "أُرسل إليك الملف؛ استخدم أدوات مشغّل تيليجرام للتحكم في تشغيله."
        )
        if track.video:
            await bot.send_video(
                chat_id,
                video=str(path),
                caption=caption,
                supports_streaming=True,
            )
        else:
            await bot.send_audio(
                chat_id,
                audio=str(path),
                title=track.title[:64],
                caption=caption,
            )
        log.info("Private media sent: type=%s size_mb=%.1f", "video" if track.video else "audio", size_mb)
        try:
            await status.delete()
        except Exception:
            pass
        await bot.send_message(
            chat_id,
            "✅ تم إرسال الملف بنجاح. يمكنك تشغيله أو تنزيله من الرسالة السابقة.\n"
            "Media sent successfully. Use the Telegram player in the previous message.",
        )
    except Exception as exc:
        log.exception("Private media delivery failed")
        friendly = str(exc)
        if "ffmpeg" in friendly.lower():
            friendly = "يبدو أن FFmpeg غير متاح لمعالجة هذا المقطع. تحقق من تثبيته في بيئة التشغيل. / FFmpeg may be unavailable."
        elif "max_filesize" in friendly.lower() or "file is larger" in friendly.lower():
            friendly = "المقطع أكبر من الحد المسموح للتنزيل. جرّب مقطعًا أقصر. / The media exceeds the download limit."
        elif "sign in to confirm" in friendly.lower() or "confirm you’re not a bot" in friendly.lower() or "confirm you're not a bot" in friendly.lower():
            friendly = friendly_media_error(exc)
        await bot.send_message(
            chat_id,
            "❌ لم أتمكن من إرسال الوسائط.\n"
            f"السبب: {friendly[:500]}\n\n"
            "جرّب رابطًا مباشرًا لمقطع واحد، أو ملفًا أصغر، ثم أعد المحاولة.\n"
            "Try a single media URL or a smaller file, then retry.",
        )
        try:
            await status.delete()
        except Exception:
            pass
    finally:
        cleanup_track_file(track)
        if CLEAN_COMMANDS:
            await delete_safely(message)


async def consume_pending_query(message: Message) -> bool:
    """Consume the next ordinary text message for a pending play request."""
    if not message.text or message.text.lstrip().startswith("/") or not message.from_user:
        return False

    key = (message.chat.id, message.from_user.id)
    pending = pending_requests.pop(key, None)
    if not pending:
        return False

    video, expires_at = pending
    query_text = message.text.strip()
    if time.monotonic() > expires_at or not query_text:
        return False

    if message.chat.type == ChatType.PRIVATE:
        await do_play_private(message, query_text, video)
        return True
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await do_play(message, query_text, video)
        return True
    return False


async def command_handler(
    _: Client,
    message: Message,
) -> None:
    if not message.text:
        return

    cmd, arg = normalize_command(message.text)
    user_id = message.from_user.id if message.from_user else None
    pending_key = (message.chat.id, user_id) if user_id is not None else None

    if not cmd:
        await consume_pending_query(message)
        return

    if pending_key is not None and cmd not in ("play", "video"):
        pending_requests.pop(pending_key, None)

    log.info(
        "Recognized command=%s | chat_type=%s | chat_id=%s | user_id=%s",
        cmd,
        message.chat.type,
        message.chat.id,
        message.from_user.id if message.from_user else None,
    )

    if cmd in ("play", "video") and not arg and not has_supported_media_reply(message):
        if pending_key is not None:
            pending_requests[pending_key] = (cmd == "video", time.monotonic() + PENDING_REQUEST_TTL)
            await bot.send_message(
                message.chat.id,
                "أرسل الآن اسم المقطع أو رابطه في رسالة تالية خلال "
                f"{PENDING_REQUEST_TTL} ثانية. / Send the media name or URL in your next message "
                f"within {PENDING_REQUEST_TTL} seconds.",
                disable_web_page_preview=True,
            )
            if CLEAN_COMMANDS:
                await delete_safely(message)
            return

    if message.chat.type == ChatType.PRIVATE:
        if cmd == "help":
            await send_status(message.chat.id, HELP_TEXT)
        elif cmd in ("play", "video"):
            await do_play_private(message, arg, cmd == "video")
        elif cmd in ("pause", "resume", "back", "forward", "next", "stop", "leave", "queue", "now"):
            await send_status(
                message.chat.id,
                "ℹ️ في المحادثة الخاصة أرسل إليك البوت ملف الوسائط، ولا يشغّله داخل مكالمة. "
                "لذلك لا تنطبق أزرار الإيقاف المؤقت والتقديم وقائمة تشغيل المكالمات هنا؛ "
                "استخدم أدوات مشغّل تيليجرام للتحكم في الملف المرسل.\n\n"
                "Private chat sends media as a Telegram message, not a live call. "
                "Use Telegram's media player controls; group-call controls apply in groups.",
            )
        return

    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
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


@bot.on_message((filters.group | filters.private) & filters.text)
async def on_group_text(
    client: Client,
    message: Message,
) -> None:
    # Log private-chat messages and slash commands without recording message contents.
    if (
        message.chat.type == ChatType.PRIVATE
        or (message.text and message.text.lstrip().startswith("/"))
    ):
        log.info(
            "Telegram text update received | chat_type=%s | chat_id=%s | user_id=%s | message_id=%s",
            message.chat.type,
            message.chat.id,
            message.from_user.id if message.from_user else None,
            message.id,
        )

    try:
        await command_handler(client, message)
    except Exception:
        log.exception(
            "Unhandled Telegram message error | chat_type=%s | chat_id=%s | message_id=%s",
            message.chat.type,
            message.chat.id,
            message.id,
        )
        try:
            await bot.send_message(
                message.chat.id,
                "⚠️ حدث خطأ أثناء معالجة رسالتك. سُجّل السبب في سجلات التشغيل؛ أعد المحاولة بعد قليل.\n"
                "An internal error occurred while processing your message. The error was logged.",
            )
        except Exception:
            log.exception("Failed to send message-handler error notice")


@assistant.on_message((filters.group | filters.private) & filters.text)
async def on_assistant_pending_text(
    _: Client,
    message: Message,
) -> None:
    # Userbot receives ordinary group text even when the controller bot's privacy mode hides it.
    await consume_pending_query(message)


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
        bot_identity = await bot.get_me()
        log.info(
            "Controller bot started as @%s (id=%s)",
            bot_identity.username or "unknown",
            bot_identity.id,
        )

        try:
            await bot.set_bot_commands(
                [
                    BotCommand("start", "بدء البوت / Start"),
                    BotCommand("help", "عرض المساعدة والأوامر / Help"),
                    BotCommand("play", "صوت: خاص إرسال ملف، مجموعة تشغيل / Play audio"),
                    BotCommand("video", "فيديو: خاص إرسال ملف، مجموعة بث / Play video"),
                    BotCommand("queue", "عرض قائمة الانتظار / Show queue"),
                    BotCommand("now", "المقطع الحالي / Now playing"),
                    BotCommand("pause", "إيقاف مؤقت / Pause"),
                    BotCommand("resume", "استئناف التشغيل / Resume"),
                    BotCommand("next", "المقطع التالي / Next"),
                    BotCommand("stop", "إيقاف التشغيل / Stop"),
                    BotCommand("leave", "مغادرة المكالمة / Leave call"),
                ]
            )
            log.info("Telegram bot command menu registered")
        except Exception:
            log.exception("Could not register Telegram bot command menu")

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
