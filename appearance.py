ICONS = {
    "bot": ("🤖", "5258093637450866522"),
    "group": ("👥", "5258513401784573443"),
    "sections": ("📂", "5258514780469075716"),
    "lock": ("🔒", "5258476306152038031"),
    "check": ("✅", "5260726538302660868"),
    "refresh": ("🔄", "5258420634785947640"),
    "clock": ("⏲", "5258258882022612173"),
    "test": ("⚡️", "5258152182150077732"),
    "pause": ("✋", "5258362429389152256"),
    "resume": ("➡️", "5260450573768990626"),
    "sound": ("📣", "5260268501515377807"),
    "mute": ("🔇", "5258267368877989660"),
    "back": ("⬅️", "5258236805890710909"),
    "remove": ("🗑", "5258130763148172425"),
    "new": ("📝", "5257965174979042426"),
    "bump": ("⬆️", "5258043150110301407"),
    "price": ("🪙", "5258368777350816286"),
    "calendar": ("🗓", "5258105663359294787"),
    "link": ("⛓", "5260730055880876557"),
    "info": ("ℹ️", "5258503720928288433"),
}

FALLBACKS = {emoji_id: glyph for glyph, emoji_id in ICONS.values()}


def utf16_length(text):
    return len(text.encode("utf-16-le", errors="replace")) // 2


def compose(lines):
    fragments, entities = [], []
    offset = 0
    for line in lines:
        if not line:
            continue
        if fragments:
            offset += 1
        if isinstance(line, tuple):
            role, label = line
            glyph, emoji_id = ICONS[role]
            entities.append(
                {
                    "type": "custom_emoji",
                    "offset": offset,
                    "length": utf16_length(glyph),
                    "custom_emoji_id": emoji_id,
                }
            )
            line = f"{glyph} {label}"
        fragments.append(line)
        offset += utf16_length(line)
    return {"text": "\n".join(fragments), "entities": entities}
