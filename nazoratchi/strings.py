"""Bilingual (English / Uzbek-Latin) string catalog for every user-facing
message. Deliberately not an i18n framework: two dicts and a lookup.

Rules:
- the key sets of both languages are identical (enforced by a test);
- a missing key or unknown language falls back to English key-by-key, so a
  catalog gap can never crash a report;
- values may contain HTML (the bot sends with parse_mode=HTML globally) and
  str.format placeholders — dynamic values must be html-escaped by callers.
"""

from __future__ import annotations

LANGS = ("en", "uz")
DEFAULT_LANG = "en"

_EN = {
    # verdict badges (reports)
    "verdict.approve": "✅ APPROVED",
    "verdict.hold": "⏸ HELD FOR REVIEW",
    "verdict.decline": "⛔ DECLINED",

    # report layout — one field per line (long ·-joined lines wrap into soup
    # on phones, and RTL names scramble mixed lines)
    "report.dry_run": "🧪 DRY RUN — would be: ",
    "report.chat": "💬 Group: {chat}",
    "report.source": "📥 Source: {source}",
    "report.action": "⚖️ Result: {action}",
    "report.triggered": "🚩 <b>Triggered:</b>",
    "report.more_triggers": "… and {n} more (see logs)",
    "report.soft": "<i>Soft text signals:</i> ",
    "report.bio": "📝 <b>Bio:</b>",
    "report.approved_clean": "✅ approved",
    "report.kept_clean": "✅ kept (clean)",
    "report.rerouted": ("⚠️ owner unreachable for chat <code>{chat_id}</code>"
                        " — report rerouted:"),

    # dynamic tokens shown in reports/lists (looked up via label())
    "source.join_request": "join request",
    "source.chat_member": "new member",
    "source.first_message": "first message",
    "action.dry_run": "dry run (no action)",
    "action.approved": "approved",
    "action.declined": "declined",
    "action.resolved_externally": "handled elsewhere",
    "action.pending": "awaiting your decision",
    "action.banned": "banned",
    "action.ban_failed": "ban FAILED — check bot rights",
    "action.banned_pending": "banned — awaiting review",
    "action.kept_flagged": "kept in group — awaiting your decision",
    "action.kept": "kept",
    "action.overridden": "let back in",
    "action.override_failed": "unban failed",

    # report action buttons
    "btn.approve": "✅ Approve",
    "btn.decline": "⛔ Decline",
    "btn.override": "🔓 Override: let user in",
    "btn.unban": "🔓 Unban / let back in",
    "btn.ban": "🔨 Ban",
    "btn.keep": "✅ Keep",

    # callback toasts / outcomes
    "cb.malformed": "Malformed action.",
    "cb.unknown": "Unknown screening record.",
    "cb.not_authorized": "Not authorized.",
    "cb.already_handled": "Already handled by another admin.",
    "cb.action_failed": "Action '{action}' failed — try again.",
    "cb.approved": "✅ Approved by {admin} (allowlisted)",
    "cb.approve_gone": ("⚠️ Approve pressed by {admin}, but the request no longer"
                        " exists (withdrawn or handled elsewhere)"),
    "cb.declined": "⛔ Declined by {admin}",
    "cb.decline_gone": ("⚠️ Decline pressed by {admin}, but the request was"
                        " already gone"),
    "cb.unban_failed": ("⚠️ {admin}: unban FAILED — check the bot's ban rights"
                        " in the group, then press again"),
    "cb.no_invite": ("⚠️ Override — {admin}\n"
                     " ✅ unbanned\n"
                     " ✅ added to allowlist\n"
                     " ❌ invite link could not be created — issue one manually"),
    "cb.override_dm": ("🔓 Override — {admin}\n"
                       " ✅ unbanned\n"
                       " ✅ added to allowlist\n"
                       " ✅ invite sent by DM"),
    "cb.override_manual": ("🔓 Override — {admin}\n"
                           " ✅ unbanned\n"
                           " ✅ added to allowlist\n"
                           " ❌ DM failed\n"
                           "\n"
                           "🔗 Single-use link (forward it manually):\n{invite}"),
    "cb.banned": "🔨 Banned by {admin}",
    "cb.ban_failed": "⚠️ Ban by {admin} FAILED — check bot rights",
    "cb.kept": "✅ Kept by {admin} (allowlisted)",
    "cb.user_invite": "You have been approved. Join here: {invite}",

    # DM to the screened user while their join request is pending
    "user.pending": ("Your request to join is being reviewed. You'll be admitted"
                     " once it's approved."),

    # /enable, /disable
    "enable.cap": "You already run the bot in {n} groups — that's the limit.",
    "enable.dm_probe": ("NazoratchiAI enabled for “{title}”.\n"
                        "Screening reports for that group will arrive here. "
                        "Use /blocked and /held to review cases."),
    "enable.no_dm": ("I can't message you privately. Open @{bot}, press Start,"
                     " then run /enable here again."),
    "enable.on": "✅ NazoratchiAI is ON. New members are screened automatically.",
    "enable.off": "NazoratchiAI is OFF for this group.",

    # operator approval gate for new groups
    "req.sent": ("📨 Your request was sent to the bot owner."
                 " You'll get a message here once it's decided."),
    "req.pending_already": ("⏳ Your request is already waiting for the bot"
                            " owner's decision."),
    "req.rejected": "❌ Your request to use this bot was declined by the owner.",
    "req.operator_new": ("📨 <b>New group request</b>\n"
                         "💬 {title}\n"
                         "🆔 <code>{chat_id}</code>\n"
                         "👤 {requester} · <code>{requester_id}</code>"),
    "req.operator_approved": "✅ Approved — “{title}” is now screened.",
    "req.operator_rejected": "❌ Rejected — “{title}”.",
    "req.approved_user": ("✅ Your group “{title}” was approved — NazoratchiAI"
                          " is now active. New members are screened"
                          " automatically."),
    "req.rejected_user": ("❌ Your request for “{title}” was declined by the"
                          " bot owner."),
    "req.already_decided": "Already decided.",
    "btn.req_approve": "✅ Approve",
    "btn.req_reject": "❌ Reject",

    # group-rights problems
    "rights.not_admin": "bot is not an administrator in chat {chat}",
    "rights.no_ban": ("bot lacks 'Ban users' right in chat {chat}"
                      " — it cannot remove anyone"),
    "rights.no_invite": ("bot lacks 'Invite users' right in chat {chat}"
                         " — join-request mode and override invite links"
                         " will not work"),
    "rights.cannot_inspect": "cannot inspect chat {chat}: {error}",
    "main.problems_in": "⚠️ NazoratchiAI problems in “{title}”:",

    # language selection (chooser text is bilingual by design — shown pre-pick)
    "lang.choose": "🌐 “{title}” — Choose language / Tilni tanlang:",
    "lang.set": "✅ Language for “{title}”: {language}",
    "lang.name": "English",
    "lang.no_groups": ("You don't own any enabled groups."
                       " Run /enable in your group first."),

    # /blocked and /held lists
    "list.blocked_title": "⛔ Currently blocked",
    "list.blocked_empty": "No blocked users on record.",
    "list.held_title": "⏸ Awaiting review",
    "list.held_empty": "Nothing is awaiting review.",
    "list.latest": "(latest {n})",
    "btn.unban_short": "🔓 Unban",
    "btn.override_short": "🔓 Override",
    "btn.approve_short": "✅ Approve",
    "age.hours": "{n}h ago",
    "age.days": "{n}d ago",

    # command menu descriptions (short — phones truncate past ~35 chars)
    "cmd.enable": "Enable screening",
    "cmd.disable": "Disable screening",
    "cmd.language": "Change language",
    "cmd.blocked": "Blocked users",
    "cmd.held": "Cases awaiting review",

    # bot profile texts (plain text: description ≤512, short ≤120 chars)
    "bot.description": ("I protect your group: every new member's profile"
                        " photos, bio and first message are screened for adult"
                        " content and spam before they can do harm. To start:"
                        " add me to your group, make me an admin with the"
                        " 'Ban users' right, and send /enable there."),
    "bot.short_description": ("Screens new group members' photos, bio and"
                              " first message for adult content and spam."),

    # onboarding guide, DM'd after the language pick
    "onboarding": (
        "📖 <b>How NazoratchiAI works</b>\n"
        "\n"
        "The bot screens every new member of your group:\n"
        "• profile photos and bio — checked for adult content and spam signals\n"
        "• their first message (text, emoji, photos) — screened before it can"
        " do harm\n"
        "\n"
        "When something is caught, the account is banned (or held for review)"
        " and a report arrives here with the evidence and action buttons —"
        " you always have the final say.\n"
        "\n"
        "<b>Commands</b>\n"
        "/blocked — users currently kept out, with an Unban button\n"
        "/held — cases awaiting your decision\n"
        "/language — switch the bot's language for your group\n"
        "/disable — turn screening off for your group\n"
        "\n"
        "⚠️ The bot must be an <b>administrator</b> of the group with the"
        " <b>“Ban users”</b> right, otherwise it cannot remove anyone."
    ),
}

_UZ = {
    "verdict.approve": "✅ TASDIQLANDI",
    "verdict.hold": "⏸ KOʻRIB CHIQISH KUTILMOQDA",
    "verdict.decline": "⛔ RAD ETILDI",

    "report.dry_run": "🧪 SINOV REJIMI — natija boʻlardi: ",
    "report.chat": "💬 Guruh: {chat}",
    "report.source": "📥 Manba: {source}",
    "report.action": "⚖️ Natija: {action}",
    "report.triggered": "🚩 <b>Aniqlangan belgilar:</b>",
    "report.more_triggers": "… va yana {n} ta (loglarga qarang)",
    "report.soft": "<i>Yumshoq matn belgilari:</i> ",
    "report.bio": "📝 <b>Bio:</b>",
    "report.approved_clean": "✅ tasdiqlandi",
    "report.kept_clean": "✅ qoldirildi (toza)",
    "report.rerouted": ("⚠️ <code>{chat_id}</code> guruh egasi bilan bogʻlanib"
                        " boʻlmadi — hisobot qayta yoʻnaltirildi:"),

    "source.join_request": "qoʻshilish soʻrovi",
    "source.chat_member": "yangi aʼzo",
    "source.first_message": "birinchi xabar",
    "action.dry_run": "sinov (amal qilinmadi)",
    "action.approved": "tasdiqlandi",
    "action.declined": "rad etildi",
    "action.resolved_externally": "boshqa joyda hal qilindi",
    "action.pending": "qaroringiz kutilmoqda",
    "action.banned": "bloklandi",
    "action.ban_failed": "bloklash AMALGA OSHMADI — bot huquqlarini tekshiring",
    "action.banned_pending": "bloklandi — koʻrib chiqish kutilmoqda",
    "action.kept_flagged": "guruhda qoldirildi — qaroringiz kutilmoqda",
    "action.kept": "qoldirildi",
    "action.overridden": "qayta kiritildi",
    "action.override_failed": "blokdan chiqarish amalga oshmadi",

    "btn.approve": "✅ Tasdiqlash",
    "btn.decline": "⛔ Rad etish",
    "btn.override": "🔓 Bekor qilish: kirishga ruxsat",
    "btn.unban": "🔓 Blokdan chiqarish",
    "btn.ban": "🔨 Bloklash",
    "btn.keep": "✅ Qoldirish",

    "cb.malformed": "Notoʻgʻri amal.",
    "cb.unknown": "Nomaʼlum tekshiruv yozuvi.",
    "cb.not_authorized": "Ruxsat yoʻq.",
    "cb.already_handled": "Boshqa admin allaqachon hal qildi.",
    "cb.action_failed": "'{action}' amali bajarilmadi — qayta urinib koʻring.",
    "cb.approved": "✅ {admin} tomonidan tasdiqlandi (oq roʻyxatga qoʻshildi)",
    "cb.approve_gone": ("⚠️ {admin} tasdiqlashni bosdi, lekin soʻrov endi mavjud"
                        " emas (qaytarib olingan yoki boshqa joyda hal qilingan)"),
    "cb.declined": "⛔ {admin} tomonidan rad etildi",
    "cb.decline_gone": ("⚠️ {admin} rad etishni bosdi, lekin soʻrov allaqachon"
                        " yoʻq edi"),
    "cb.unban_failed": ("⚠️ {admin}: blokdan chiqarish AMALGA OSHMADI — guruhda"
                        " botning bloklash huquqini tekshiring va qayta bosing"),
    "cb.no_invite": ("⚠️ Bekor qilindi — {admin}\n"
                     " ✅ blokdan chiqarildi\n"
                     " ✅ oq roʻyxatga qoʻshildi\n"
                     " ❌ taklif havolasi yaratilmadi — uni qoʻlda yuboring"),
    "cb.override_dm": ("🔓 Bekor qilindi — {admin}\n"
                       " ✅ blokdan chiqarildi\n"
                       " ✅ oq roʻyxatga qoʻshildi\n"
                       " ✅ taklif shaxsiy xabarda yuborildi"),
    "cb.override_manual": ("🔓 Bekor qilindi — {admin}\n"
                           " ✅ blokdan chiqarildi\n"
                           " ✅ oq roʻyxatga qoʻshildi\n"
                           " ❌ shaxsiy xabar yuborilmadi\n"
                           "\n"
                           "🔗 Bir martalik havola (qoʻlda yuboring):\n{invite}"),
    "cb.banned": "🔨 {admin} tomonidan bloklandi",
    "cb.ban_failed": ("⚠️ {admin} bloklashi AMALGA OSHMADI — bot huquqlarini"
                      " tekshiring"),
    "cb.kept": "✅ {admin} tomonidan qoldirildi (oq roʻyxatga qoʻshildi)",
    "cb.user_invite": "Sizga ruxsat berildi. Bu yerdan qoʻshiling: {invite}",

    "user.pending": ("Guruhga qoʻshilish soʻrovingiz koʻrib chiqilmoqda."
                     " Tasdiqlangach guruhga qabul qilinasiz."),

    "enable.cap": "Bot sizda allaqachon {n} ta guruhda ishlayapti — bu chegara.",
    "enable.dm_probe": ("NazoratchiAI “{title}” guruhi uchun yoqildi.\n"
                        "Oʻsha guruh boʻyicha tekshiruv hisobotlari shu yerga"
                        " keladi. Holatlarni koʻrish uchun /blocked va /held"
                        " buyruqlaridan foydalaning."),
    "enable.no_dm": ("Sizga shaxsiy xabar yubora olmayman. @{bot} botini oching,"
                     " Start tugmasini bosing, soʻng bu yerda /enable buyrugʻini"
                     " qayta ishga tushiring."),
    "enable.on": ("✅ NazoratchiAI YONIQ. Yangi aʼzolar avtomatik ravishda"
                  " tekshiriladi."),
    "enable.off": "NazoratchiAI bu guruh uchun OʻCHIRILGAN.",

    "req.sent": ("📨 Soʻrovingiz bot egasiga yuborildi. Qaror qabul qilingach"
                 " shu yerga xabar keladi."),
    "req.pending_already": ("⏳ Soʻrovingiz allaqachon bot egasining qarorini"
                            " kutmoqda."),
    "req.rejected": ("❌ Bu botdan foydalanish soʻrovingiz bot egasi tomonidan"
                     " rad etildi."),
    "req.operator_new": ("📨 <b>New group request</b>\n"
                         "💬 {title}\n"
                         "🆔 <code>{chat_id}</code>\n"
                         "👤 {requester} · <code>{requester_id}</code>"),
    "req.operator_approved": "✅ Approved — “{title}” is now screened.",
    "req.operator_rejected": "❌ Rejected — “{title}”.",
    "req.approved_user": ("✅ “{title}” guruhingiz tasdiqlandi — NazoratchiAI"
                          " endi faol. Yangi aʼzolar avtomatik tekshiriladi."),
    "req.rejected_user": ("❌ “{title}” uchun soʻrovingiz bot egasi tomonidan"
                          " rad etildi."),
    "req.already_decided": "Allaqachon hal qilingan.",
    "btn.req_approve": "✅ Approve",
    "btn.req_reject": "❌ Reject",

    "rights.not_admin": "bot {chat} chatida administrator emas",
    "rights.no_ban": ("botda {chat} chatida “Foydalanuvchilarni bloklash”"
                      " huquqi yoʻq — u hech kimni chiqara olmaydi"),
    "rights.no_invite": ("botda {chat} chatida “Foydalanuvchilarni taklif"
                         " qilish” huquqi yoʻq — qoʻshilish soʻrovi rejimi va"
                         " taklif havolalari ishlamaydi"),
    "rights.cannot_inspect": "{chat} chatini tekshirib boʻlmadi: {error}",
    "main.problems_in": "⚠️ “{title}” guruhida NazoratchiAI muammolari:",

    "lang.choose": "🌐 “{title}” — Choose language / Tilni tanlang:",
    "lang.set": "✅ “{title}” uchun til: {language}",
    "lang.name": "Oʻzbekcha",
    "lang.no_groups": ("Sizda yoqilgan guruhlar yoʻq. Avval guruhingizda"
                       " /enable buyrugʻini ishga tushiring."),

    "list.blocked_title": "⛔ Hozirda bloklanganlar",
    "list.blocked_empty": "Bloklangan foydalanuvchilar yoʻq.",
    "list.held_title": "⏸ Koʻrib chiqish kutilmoqda",
    "list.held_empty": "Koʻrib chiqishni kutayotgan hech narsa yoʻq.",
    "list.latest": "(oxirgi {n} ta)",
    "btn.unban_short": "🔓 Blokdan chiqarish",
    "btn.override_short": "🔓 Bekor qilish",
    "btn.approve_short": "✅ Tasdiqlash",
    "age.hours": "{n} soat oldin",
    "age.days": "{n} kun oldin",

    "cmd.enable": "Tekshiruvni yoqish",
    "cmd.disable": "Tekshiruvni oʻchirish",
    "cmd.language": "Tilni almashtirish",
    "cmd.blocked": "Bloklanganlar roʻyxati",
    "cmd.held": "Qaror kutayotgan holatlar",

    "bot.description": ("Men guruhingizni himoya qilaman: har bir yangi"
                        " aʼzoning profil rasmlari, bio va birinchi xabari"
                        " zarar yetkazishidan oldin kattalar kontenti va"
                        " spamga tekshiriladi. Boshlash uchun: meni"
                        " guruhingizga qoʻshing, “Foydalanuvchilarni bloklash”"
                        " huquqi bilan admin qiling va u yerda /enable"
                        " buyrugʻini yuboring."),
    "bot.short_description": ("Yangi aʼzolarning rasmlari, bio va birinchi"
                              " xabarini kattalar kontenti va spamga"
                              " tekshiradi."),

    "onboarding": (
        "📖 <b>NazoratchiAI qanday ishlaydi</b>\n"
        "\n"
        "Bot guruhingizga qoʻshilayotgan har bir yangi aʼzoni tekshiradi:\n"
        "• profil rasmlari va bio — kattalar kontenti va spam belgilariga"
        " tekshiriladi\n"
        "• birinchi xabari (matn, emoji, rasmlar) — zarar yetkazishidan oldin"
        " tekshiriladi\n"
        "\n"
        "Biror narsa aniqlansa, akkaunt bloklanadi (yoki koʻrib chiqish uchun"
        " ushlab turiladi) va shu yerga dalillar hamda amal tugmalari bilan"
        " hisobot keladi — yakuniy qaror doim sizniki.\n"
        "\n"
        "<b>Buyruqlar</b>\n"
        "/blocked — hozirda bloklangan foydalanuvchilar, blokdan chiqarish"
        " tugmasi bilan\n"
        "/held — sizning qaroringizni kutayotgan holatlar\n"
        "/language — guruh uchun bot tilini almashtirish\n"
        "/disable — guruh uchun tekshiruvni oʻchirish\n"
        "\n"
        "⚠️ Bot guruhda <b>administrator</b> boʻlishi va <b>“Foydalanuvchilarni"
        " bloklash”</b> huquqiga ega boʻlishi shart, aks holda u hech kimni"
        " chiqara olmaydi."
    ),
}

_STR = {"en": _EN, "uz": _UZ}


def t(lang: str, key: str, **kw) -> str:
    """Look up `key` in `lang`, falling back to English key-by-key."""
    table = _STR.get(lang, _STR[DEFAULT_LANG])
    s = table.get(key)
    if s is None:
        s = _STR[DEFAULT_LANG][key]
    return s.format(**kw) if kw else s


def label(lang: str, prefix: str, value: str) -> str:
    """Localize a dynamic token (e.g. label(lang, "action", "banned")).
    Unknown tokens come back verbatim so a new action never crashes a report."""
    table = _STR.get(lang, _STR[DEFAULT_LANG])
    key = f"{prefix}.{value}"
    s = table.get(key)
    if s is None:
        s = _STR[DEFAULT_LANG].get(key)
    return s if s is not None else value
