"""Command aliases and parser, kept independent for unit testing."""
import re
from typing import Optional

ALIASES = {
    "play": "play",
    "p": "play",
    "تشغيل": "play",
    "شغل": "play",
    "شغّل": "play",

    "video": "video",
    "فيديو": "video",
    "فيديوهات": "video",

    "queue": "queue",
    "قائمة": "queue",
    "الانتظار": "queue",
    "قائمة_الانتظار": "queue",

    "now": "now",
    "الآن": "now",
    "الحالي": "now",
    "الان": "now",

    "next": "next",
    "التالي": "next",
    "تخطي": "next",

    "pause": "pause",
    "إيقاف_مؤقت": "pause",
    "ايقاف_مؤقت": "pause",
    "وقف": "pause",

    "resume": "resume",
    "استئناف": "resume",
    "اكمل": "resume",
    "أكمل": "resume",

    "back": "back",
    "رجوع": "back",
    "للخلف": "back",

    "forward": "forward",
    "تقديم": "forward",
    "للأمام": "forward",
    "للامام": "forward",

    "stop": "stop",
    "إيقاف": "stop",
    "ايقاف": "stop",
    "إنهاء": "stop",
    "انهاء": "stop",

    "leave": "leave",
    "مغادرة": "leave",
    "اخرج": "leave",

    "help": "help",
    "مساعدة": "help",
    "الاوامر": "help",
    "الأوامر": "help",

    "start": "help",
    "ابدأ": "help",
    "ابدا": "help",
}


def normalize_command(text: str) -> tuple[Optional[str], str]:
    """Accept /command, command, Arabic names and optional @botname."""
    text = text.strip()

    if not text:
        return None, ""

    if text.startswith("/"):
        text = text[1:]

    parts = text.split(maxsplit=1)

    token = (
        parts[0]
        .split("@", 1)[0]
        .strip()
        .lower()
        .replace("ـ", "")
    )

    token = token.replace(" ", "_")
    cmd = ALIASES.get(token)

    if cmd is None:
        token = re.sub(
            r"^[،,.!؟?]+|[،,.!؟?]+$",
            "",
            token,
        )
        cmd = ALIASES.get(token)

    return cmd, parts[1].strip() if len(parts) > 1 else ""
