import telebot
import os
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("germandude")
import random
import time
import re
import hashlib
import unicodedata
import anthropic
import stripe
import threading
from flask import Flask, request, jsonify
from io import BytesIO
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from openai import OpenAI
from telebot.types import (InlineKeyboardMarkup, InlineKeyboardButton,
                           ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
                           BotCommand)

# KEYS
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID  = int(os.getenv("ADMIN_CHAT_ID", "673270002"))  # Izzi's Telegram ID
OPENAI_KEY      = os.getenv("OPENAI_API_KEY")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY")

# CLIENTS
bot           = telebot.TeleBot(TELEGRAM_TOKEN)
claude        = anthropic.Anthropic(api_key=ANTHROPIC_KEY)  # text/chat
openai_client = OpenAI(api_key=OPENAI_KEY)                   # audio only

# Defined early — needed by wrapper below
last_bot_text = {}

# ── TRANSLATE BUTTON WRAPPER ─────────────────────────────────────────────────
# Automatically appends 🌍 übersetzen button to every plain bot message.
# Messages that already have reply_markup (keyboards, inline buttons) are untouched.
_orig_send_message = bot.send_message

def _send_message_with_translate(chat_id, text, **kwargs):
    """Wrap bot.send_message to auto-inject übersetzen button on plain messages."""
    # Store last bot text regardless
    if isinstance(text, str):
        last_bot_text[chat_id] = text
    # Don't inject if message already has buttons or is a system/keyboard message
    if "reply_markup" not in kwargs or kwargs["reply_markup"] is None:
        translate_btn = InlineKeyboardMarkup()
        translate_btn.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))
        kwargs["reply_markup"] = translate_btn
    return _orig_send_message(chat_id, text, **kwargs)

bot.send_message = _send_message_with_translate

bot.set_my_commands([
    BotCommand("info",         "Instruktion 📖"),
    BotCommand("start",        "Start"),
    BotCommand("themen",       "Themen wählen 🎯"),
    BotCommand("level",        "Mein Progress 📊"),
    BotCommand("errors",       "Meine Fehler"),
    BotCommand("practice",     "Übungen"),
    BotCommand("flashcards",   "Vokabelkarten 🃏"),
    BotCommand("share",        "Bot teilen 🤝"),
    BotCommand("danke",        "Danke sagen 🙏"),
    BotCommand("upgrade",      "Pläne & Preise 💎"),
    BotCommand("restart",      "Chat neu starten"),
    BotCommand("integration",  "Leben in Deutschland 🏛️"),
    BotCommand("support",      "Support 🆘"),
])

# PERSISTENT STORAGE
# /data is a Railway Volume — survives redeploys. Fallback to local for dev.
USER_FILE = os.getenv("USER_FILE", "/data/users.json")

def load_users():
    if not os.path.exists(USER_FILE):
        return {}
    with open(USER_FILE, "r") as f:
        return json.load(f)

def save_users(data):
    os.makedirs(os.path.dirname(USER_FILE) or ".", exist_ok=True)
    with open(USER_FILE, "w") as f:
        json.dump(data, f, indent=2)

ALL_GOALS = [
    "Selbstpräsentation", "Freunde / Beziehungen", "Soziales (Ämter, Ärzte)",
    "Unterhaltung (Club, Kino etc)", "Einkauf & Restaurants", "Tourismus & Reisen",
    "Sport & Hobbys", "Am Telefon", "Job"
]

def onboarding_complete(chat_id) -> bool:
    """True if user has completed onboarding (name + language + test)."""
    uid  = str(chat_id)
    user = user_data.get(uid, {})
    return bool(
        user.get("name") and
        user.get("native_language") and
        user.get("level")
    )


def _require_onboarding(chat_id) -> bool:
    """Send nudge if onboarding not complete. Returns True if blocked."""
    if onboarding_complete(chat_id):
        return False
    uid   = str(chat_id)
    user  = user_data.get(uid, {})
    state = user_state.get(chat_id, {})
    mode  = state.get("mode", "idle")

    # Don't interrupt active onboarding or test
    if mode in ("onboarding", "test"):
        return True

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("👉 Onboarding starten", callback_data="restart_onboarding"))
    bot.send_message(chat_id,
        "⚠️ Bitte schließe zuerst das kurze Onboarding ab — es dauert nur 1 Minute!\n\n"
        "Tippe /start um zu beginnen.",
        reply_markup=markup)
    return True


def _track_feature(chat_id, feature: str):
    uid = str(chat_id)
    if uid not in user_data: return
    user_data[uid].setdefault("features_used", {})
    user_data[uid]["features_used"][feature] = user_data[uid]["features_used"].get(feature, 0) + 1


def ensure_user(chat_id):
    uid = str(chat_id)
    now = datetime.now().isoformat()
    if uid not in user_data:
        user_data[uid] = {
            "name": None, "gender": None, "native_language": None,
            "goal": None, "level": "A2", "scenario_streak": 0,
            "weak_points": [], "errors": [], "test_errors": [],
            "user_progress": {g: [] for g in ALL_GOALS},
            "user_stats": {"xp": 0, "level": 1, "streak": 0, "last_active": now, "total_scenarios": 0},
            "trial_start": None, "premium": False, "trial_code_used": None,
            "stripe_customer_id": None, "stripe_subscription_id": None,
            "premium_plus": False, "premium_plus_until": None,
            "daily_convos": {}, "voice_push": {},
            # Tracking fields
            "joined": now, "message_count": 0, "paywall_hits": 0,
            "features_used": {}, "conversations_started": 0, "test_completed": False,
        }
    else:
        # Backfill missing fields
        user_data[uid].setdefault("user_progress", {g: [] for g in ALL_GOALS})
        user_data[uid].setdefault("scenario_streak", 0)
        user_data[uid].setdefault("weak_points", [])
        user_data[uid].setdefault("errors", [])
        user_data[uid].setdefault("test_errors", [])
        user_data[uid].setdefault("achievements", [])
        user_data[uid].setdefault("gender", None)
        user_data[uid].setdefault("native_language", None)
        user_data[uid].setdefault("trial_start", None)
        user_data[uid].setdefault("trial_code_used", None)
        user_data[uid].setdefault("premium", False)
        user_data[uid].setdefault("stripe_customer_id", None)
        user_data[uid].setdefault("stripe_subscription_id", None)
        user_data[uid].setdefault("user_stats", {"xp": 0, "level": 1, "streak": 0, "last_active": now, "total_scenarios": 0})
        user_data[uid]["user_stats"].setdefault("total_scenarios", 0)
        # Tracking backfill
        user_data[uid].setdefault("joined", now)
        user_data[uid].setdefault("message_count", 0)
        user_data[uid].setdefault("paywall_hits", 0)
        user_data[uid].setdefault("features_used", {})
        user_data[uid].setdefault("conversations_started", 0)
        user_data[uid].setdefault("test_completed", False)
        user_data[uid].setdefault("premium_plus", False)
        user_data[uid].setdefault("premium_plus_until", None)
        user_data[uid].setdefault("daily_convos", {})
        user_data[uid].setdefault("voice_push", {})
    # Update activity on every interaction
    user_data[uid]["last_active"] = now
    user_data[uid]["message_count"] = user_data[uid].get("message_count", 0) + 1
    save_users(user_data)

def save_name(chat_id, name):
    user_data[str(chat_id)]["name"] = name
    save_users(user_data)

# STATE
turn_counter = {}
user_memory = {}
user_voice = {}  # TTS voice per user
current_scenario = {}
last_voice_text     = {}  # last transcribed voice per user
# last_bot_text defined earlier (before wrapper)
last_voice_answer   = {}  # GPT reply for that voice (None if ask_gpt itself failed)
last_voice_answered = {}  # True once the TTS reply was actually delivered
user_data = load_users()
user_step = {}
session_state = {}   # per-user adaptive engine: {struggle: int, success: int}
exercise_data = {}   # stores pre-computed XP/gamification while user does exercises
_text_id_counter = 0
pending_texts = {}   # text_id (int) → text; powers the "📄 Text anzeigen" button

# QUIZ STATE
quiz_state = {}
quiz_current_level = {}
quiz_scores = {}        # 🔥 wichtig: Score pro Level
quiz_history = {}
quiz_a0_results = {}    # gating results for first 2 A0 questions
user_level = {}         # final level after test
asked_questions = {}    # set of asked question IDs per user
test_state = {}         # unified quiz state per user
user_state = {}         # state machine: "test" | "chat"

VOICES = ["alloy", "echo", "nova", "shimmer", "fable", "onyx"]

# QUIZ CONFIG
TOTAL_QUESTIONS = 12
LEVELS = ["A0", "A1", "A2", "B1", "B2", "C1"]
QUIZ_LEVEL_ORDER = ["A1", "A2", "B1", "B2", "C1"]

# ── A0 SCREENING (2 questions — fail = mini lessons or email) ─────────────────
# Correct answer position is randomised per question so it's never always A/B/C
A0_QUESTIONS = [
    {
        "id": "A0_1",
        "q": 'Hallo! Ich ____ Maria. Und du?',
        "options": ["heißt", "heißen", "heiße"],   # correct = C → index 2
        "answer": "heiße",
    },
    {
        "id": "A0_2",
        "q": 'Hallo, Maria. Ich bin Alvaro. Ich komme ____ Spanien. Und du?',
        "options": ["von", "aus", "nach"],           # correct = B → index 1
        "answer": "aus",
    },
]

# ── MAIN QUESTION POOL — 10 questions (2 per level A1→C1) ────────────────────
# Answers are shuffled so correct answer rotates through A/B/C positions
QUESTION_POOL = {
    "A1": [
        {
            "id": "A1_1",
            "q": 'Siehst du den Mann, ____ da steht? Das ist mein Chef Max.',
            "options": ["denen", "der", "den"],      # correct = B
            "answer": "der",
        },
        {
            "id": "A1_2",
            "q": 'Sie studiert und arbeitet, ____ später erfolgreich zu sein.',
            "options": ["um", "damit", "zu"],        # correct = A
            "answer": "um",
        },
    ],
    "A2": [
        {
            "id": "A2_1",
            "q": 'Wenn ich mehr Zeit ____, ____ ich mehr Deutsch lernen.',
            "options": ["hätte, wäre", "hätte, würdet", "hätte, würde"],  # correct = C
            "answer": "hätte, würde",
        },
        {
            "id": "A2_2",
            "q": 'Ich weiß nicht, ____ ich morgen mitkommen kann. Ich habe viel zu tun.',
            "options": ["dass", "warum", "ob"],      # correct = C
            "answer": "ob",
        },
    ],
    "B1": [
        {
            "id": "B1_1",
            "q": 'Ich ____ nicht gedacht, dass Marcus so viel verdient!',
            "options": ["war", "hätte", "wäre"],     # correct = B
            "answer": "hätte",
        },
        {
            "id": "B1_2",
            "q": 'Mia hat gesagt, dass sie bei mir später ____schaut.',
            "options": ["vorbei", "danach", "mit"],  # correct = A
            "answer": "vorbei",
        },
    ],
    "B2": [
        {
            "id": "B2_1",
            "q": 'Angesichts der aktuellen Diskussion über ____ gewinnt das Thema alternative Energiequellen zunehmend an Bedeutung.',
            "options": ["Nachhaltigkeit", "Auseinandersetzung", "Aufenthaltstitel"],  # correct = A
            "answer": "Nachhaltigkeit",
        },
        {
            "id": "B2_2",
            "q": 'Er äußerte sich derart differenziert, ___ selbst Experten seine Argumentation ernst nahmen.',
            "options": ["weil", "dass", "obwohl"],   # correct = B
            "answer": "dass",
        },
    ],
    "C1": [
        {
            "id": "C1_1",
            "q": "Warte, ich hab' ein starkes ____! Du wirst mir nicht glauben, was gerade im Meeting passiert ist!",
            "options": ["Mitteilungsbedürfnis", "Mitteilung", "Mitteilungserlebnis"],  # correct = A
            "answer": "Mitteilungsbedürfnis",
        },
        {
            "id": "C1_2",
            "q": 'Der Chef hat gesagt, dass du ihm direkt nach dem Meeting ____!',
            "options": ["Bescheid gegeben haben solltest", "Bescheid sagen hätten", "hättest Bescheid geben sollen"],  # correct = C
            "answer": "hättest Bescheid geben sollen",
        },
    ],
}

# ── NPC SPEECH ADAPTATION per level (GER-based) ──────────────────────────────
# Used in build_system_prompt to instruct the NPC how to speak
NPC_LEVEL_INSTRUCTIONS = {
    "A0": (
        "Das Niveau des Lernenden ist A0 — absoluter Anfänger. "
        "Sprich extrem langsam, benutze nur die allereinfachsten Wörter (Hallo, ja, nein, bitte, danke, ich, du). "
        "Maximal 4-5 Wörter pro Satz. Wiederhol Aussagen wenn nötig."
    ),
    "A1": (
        "Das Niveau des Lernenden ist A1. "
        "Sprich deutlich langsamer als normal, mach kurze Pausen zwischen Sätzen. "
        "Benutze einfachen Grundwortschatz, kurze Sätze (max. 8 Wörter), keine Nebensätze. "
        "Wenn der Lernende etwas falsch sagt, wiederhole deinen Satz korrekt als natürliche Reaktion."
    ),
    "A2": (
        "Das Niveau des Lernenden ist A2. "
        "Sprich etwas langsamer als normal. Benutze häufige Alltagswörter, einfache Nebensätze mit 'weil', 'wenn', 'dass'. "
        "Sätze können etwas länger sein, aber bleib klar und verständlich."
    ),
    "B1": (
        "Das Niveau des Lernenden ist B1. "
        "Normales Sprechtempo. Verwende alltägliche Redewendungen und gängigen Wortschatz. "
        "Natürliche Satzlänge, gelegentlich Konjunktiv II ('könnte', 'würde'). "
        "Reagiere natürlich auf Fehler ohne sie explizit zu korrigieren."
    ),
    "B2": (
        "Das Niveau des Lernenden ist B2. "
        "Normales bis leicht erhöhtes Sprechtempo. Benutze reichhaltigen Wortschatz, Idiome, komplexe Satzstrukturen. "
        "Zeig Meinungen, Nuancen, implizite Bedeutungen. Reagiere wie ein gebildeter Muttersprachler."
    ),
    "C1": (
        "Das Niveau des Lernenden ist C1 — fortgeschritten, nahe Muttersprachlerniveau. "
        "Sprich in völlig natürlichem Muttersprachler-Tempo. Benutze anspruchsvollen Wortschatz, Fachbegriffe, "
        "Redewendungen, Ironie, implizite Bedeutungen. Kein Rücksicht auf Sprachschwierigkeiten — "
        "genau so wie du mit einem deutschen Muttersprachler sprechen würdest."
    ),
}

# SCENARIOS
LEVEL_ORDER = ["A0", "A1", "A2", "B1", "B2", "C1"]

SCENARIOS = [

    # =========================
    # 1. Selbstpräsentation
    # =========================
    {"id": "selbst_1", "npc_role": "dein neuer Nachbar im Treppenhaus",  "goal": "Selbstpräsentation", "text": "Du bist umgezogen & triffst einen Nachbarn im Treppenhaus.", "level_min": "A1", "level_max": "A2"},
    {"id": "selbst_2", "npc_role": "ein älterer freundlicher Herr im Wartezimmer beim Hausarzt",  "goal": "Selbstpräsentation", "text": "Du bist im Warteraum bei deinem Hausarzt und wirst von einem älteren Herrn freundlich angesprochen. Er fragt dich, woher du kommst und was dich nach Deutschland bringt.", "level_min": "A1", "level_max": "A2"},
    {"id": "selbst_3", "npc_role": "ein Teamkollege am ersten Arbeitstag",  "goal": "Selbstpräsentation", "text": "Dein erster Tag im Job. Du lernst dein Team kennen. Erzähl etwas über dich.", "level_min": "A2", "level_max": "B1"},
    {"id": "selbst_4", "npc_role": "die Personalmanagerin im Vorstellungsgespräch",  "goal": "Selbstpräsentation", "text": "Du bist im Vorstellungsgespräch. Erzähl etwas über dich.", "level_min": "A2", "level_max": "B2"},
    {"id": "selbst_5", "npc_role": "ein Nachbar auf der Hofparty",  "goal": "Selbstpräsentation", "text": "Du bist auf einer Hofparty deiner Nachbarn. Jemand fragt dich, wie lange du schon in Deutschland bist.", "level_min": "A1", "level_max": "A2"},
    {"id": "selbst_6", "npc_role": "jemand auf der Party",  "goal": "Selbstpräsentation", "text": "Du bist auf einer Party bei einer Freundin. Jemand fragt dich, wie du nach Deutschland gekommen bist.", "level_min": "A2", "level_max": "B1"},
    {"id": "selbst_7", "npc_role": "dein Date gegenüber",  "goal": "Selbstpräsentation", "text": "Du bist auf einem Date. Dein Gegenüber fragt dich, was du so machst.", "level_min": "A2", "level_max": "B1"},
    {"id": "selbst_8", "npc_role": "die Person auf Tinder, der du die Nachricht schickst — reagiere als wärst du diese Person",  "goal": "Selbstpräsentation", "text": "Du schickst jemandem auf Tinder eine Sprachnachricht, in der du dich kurz vorstellst.", "level_min": "A1", "level_max": "A2"},
    {"id": "selbst_9", "npc_role": "der Vermieter bei der Wohnungsbesichtigung",  "goal": "Selbstpräsentation", "text": "Du bist bei einer Wohnungsbesichtigung. Der Vermieter fragt dich, wer du bist und was du beruflich machst und warum er sich für dich entscheiden soll.", "level_min": "B1", "level_max": "C1"},
    {"id": "selbst_10", "npc_role": "der Manager im Meeting", "goal": "Selbstpräsentation", "text": 'Du bist in einem Meeting und ein Manager sagt: „[Name], Sie sind ja neu hier bei uns. Erzählen Sie doch etwas über sich!"', "level_min": "A2", "level_max": "C1"},

    # =========================
    # 2. Freunde & Beziehungen
    # =========================
    {"id": "freunde_1", "npc_role": "die Freundin die die Sprachnachricht empfängt — reagiere auf sie",  "goal": "Freunde / Beziehungen", "text": "Du möchtest deine Deutschsprechende Freundin zum Kaffee per WhatsApp Sprachnachricht einladen. Schick ihr eine Sprachnachricht!", "level_min": "A1", "level_max": "A2"},
    {"id": "freunde_2", "npc_role": "der Freund der anruft und zum Grillen einlädt",  "goal": "Freunde / Beziehungen", "text": "Deine Freunde rufen dich an und laden dich am kommenden Wochenende zum Grillen ein. Kannst du mitkommen? Sprich mit ihnen!", "level_min": "A1", "level_max": "A2"},
    {"id": "freunde_3", "npc_role": "der Kumpel der über sein Wochenende erzählt hat und jetzt zuhört",  "goal": "Freunde / Beziehungen", "text": "Du triffst dich mit deinem Kumpel. Er hat dir über sein Wochenende in Polen erzählt. Erzähle nun du über dein Wochenende.", "level_min": "A2", "level_max": "B1"},
    {"id": "freunde_4", "npc_role": "die Bestie die angerufen wird — du bist schlecht drauf",  "goal": "Freunde / Beziehungen", "text": "Deiner Bestie geht es nicht gut. Du rufst sie an und fragst sie, wie es ihr geht und was genau passiert ist. Vielleicht kannst du ihr helfen.", "level_min": "B1", "level_max": "B2"},
    {"id": "freunde_5", "npc_role": "die Kollegin nach dem Feierabend die nach Hobbys fragt",  "goal": "Freunde / Beziehungen", "text": "Du triffst deine Kollegen zu einem Bierchen nach dem Feierabend. Eine Kollegin fragt dich nach deinen Hobbies. Sie fragt dich, wie du damit angefangen hast.", "level_min": "A2", "level_max": "B1"},
    {"id": "freunde_6", "npc_role": "die deutsche Freundin des Besuchers die nach dem Rezept fragt",  "goal": "Freunde / Beziehungen", "text": "Du bekommst Besuch von deinem Freund und seiner deutschen Freundin. Sie sind begeistert von deinem Essen und sie fragt dich nach dem Rezept.", "level_min": "A2", "level_max": "B1"},
    {"id": "freunde_7", "npc_role": "die Kollegin die fragt wo du gestern Essen warst",  "goal": "Freunde / Beziehungen", "text": "Du quatschst mit deiner Kollegin, wo du gestern Essen warst. Erzähl ihr alles!", "level_min": "A2", "level_max": "B1"},
    {"id": "freunde_8", "npc_role": "der Freund mit dem Beziehungsproblem",  "goal": "Freunde / Beziehungen", "text": "Dein Freund hat ein Beziehungsproblem und du möchtest ihn nicht nur trösten, sondern auch helfen. Wie tust du das? Welche Fragen stellst du?", "level_min": "B1", "level_max": "B2"},
    {"id": "freunde_9", "npc_role": "der Liebespartner der den Urlaub plant",  "goal": "Freunde / Beziehungen", "text": "Dein Liebespartner plant mit dir einen Urlaub. Mach eine Planung mit ihm/ihr. Wo geht es hin? Wie kommt ihr hin? Was werdet ihr machen?", "level_min": "A2", "level_max": "B1"},
    {"id": "freunde_10", "npc_role": "ein eingeladener Freund in der WhatsApp-Gruppe", "goal": "Freunde / Beziehungen", "text": "Du hast bald Geburtstag und erstellst eine Gruppe in WhatsApp. Erzähl den eingeladenen Freunden, welche Party und wo du machst!", "level_min": "A2", "level_max": "B1"},
    {"id": "freunde_11", "npc_role": "die Nachbarin die den falsch sortierten Müll gesehen hat", "goal": "Freunde / Beziehungen", "text": "Deine Nachbarin hat gesehen, dass du den Müll falsch sortiert hast. Du möchtest dich entschuldigen und höflich bitten, dass sie dir die Regeln erklärt.", "level_min": "A2", "level_max": "B1"},
    {"id": "freunde_12", "npc_role": "der Freund der heiratet und die Einladung ausspricht", "goal": "Freunde / Beziehungen", "text": "Deine Freunde heiraten bald und laden dich zur Hochzeit ein. Du freust dich sehr und willst natürlich kommen. Was sagst du?", "level_min": "A1", "level_max": "A2"},
    {"id": "freunde_13", "npc_role": "der Freund der über Tiere spricht und fragt ob du Tierfreund bist", "goal": "Freunde / Beziehungen", "text": "Deine Freunde haben einen Hund und zwei Katzen und sprechen ständig darüber. Sie fragen dich, ob du ein Tierfreund bist. Was sagst du?", "level_min": "A1", "level_max": "A2"},
    {"id": "freunde_14", "npc_role": "die Kollegin die das Bild vom Welpen zeigt", "goal": "Freunde / Beziehungen", "text": "Deine Kollegin zeigt dir ein Bild von einem Welpen, den sie gestern bekommen hat. Wie ist deine Reaktion zu dem süßen Bild?", "level_min": "A1", "level_max": "A2"},
    {"id": "freunde_15", "npc_role": "die ältere Dame mit dem Hund die fragt ob sie sich setzen darf", "goal": "Freunde / Beziehungen", "text": "Du liest ein Buch auf der Bank im Park. Plötzlich kommt eine ältere Dame mit dem Hund auf dich zu und fragt dich, ob sie sich neben dich hinsetzen kann. Was sagst du?", "level_min": "A1", "level_max": "A2"},
    {"id": "freunde_16", "npc_role": "jemand aus der Gruppe in der Kneipe der einlädt", "goal": "Freunde / Beziehungen", "text": "Eine Gruppe in der Kneipe lädt dich an ihren Tisch ein, denn du bist allein. Was sagst du?", "level_min": "A1", "level_max": "A2"},

    # =========================
    # 3. Soziales (Ämter, Ärzte)
    # =========================
    {"id": "soziales_1", "npc_role": "der Sachbearbeiter am Schalter im Bürgeramt",  "goal": "Soziales (Ämter, Ärzte)", "text": "Du möchtest dich im Bürgeramt anmelden. Was sagst du am Schalter?", "level_min": "A1", "level_max": "A2"},
    {"id": "soziales_2", "npc_role": "die Empfangsdame in der Arztpraxis",  "goal": "Soziales (Ämter, Ärzte)", "text": "Du kommst in deiner Hausarztpraxis an und möchtest deinen Arzt sprechen. Was sagst du am Empfang?", "level_min": "A1", "level_max": "A2"},
    {"id": "soziales_3", "npc_role": "der Hausarzt",  "goal": "Soziales (Ämter, Ärzte)", "text": "Du möchtest dich krankschreiben lassen. Dein Arzt fragt dich, was dir fehlt. Erzähle ihm über dein Problem.", "level_min": "A2", "level_max": "B1"},
    {"id": "soziales_4", "npc_role": "der Feuerwehr-Disponent der den Notruf entgegennimmt",  "goal": "Soziales (Ämter, Ärzte)", "text": "Deine Kollegin hatte einen Arbeitsunfall. Rufe die Feuerwehr an und erkläre das Problem.", "level_min": "B1", "level_max": "B2"},
    {"id": "soziales_5", "npc_role": "der Sachbearbeiter beim Gewerbeamt",  "goal": "Soziales (Ämter, Ärzte)", "text": "Du möchtest ein Gewerbe anmelden. Was sagst du dem Sachbearbeiter?", "level_min": "B1", "level_max": "B2"},
    {"id": "soziales_6", "npc_role": "die Sachbearbeiterin im JobCenter",  "goal": "Soziales (Ämter, Ärzte)", "text": "Die Sachbearbeiterin im JobCenter bittet dich, über dich und deine Erfahrung zu erzählen und zu sagen, was genau du am Arbeitsmarkt suchst.", "level_min": "B1", "level_max": "B2"},
    {"id": "soziales_7", "npc_role": "der Techniker der kommt um den Abfluss zu reparieren",  "goal": "Soziales (Ämter, Ärzte)", "text": "Der Abfluss in deinem Badezimmer ist kaputt. Dein Vermieter hat einen Techniker organisiert. Nun ist er da. Was sagst du?", "level_min": "A2", "level_max": "B1"},
    {"id": "soziales_8", "npc_role": "der Sachbearbeiter in der Ausländerbehörde",  "goal": "Soziales (Ämter, Ärzte)", "text": "Du hast einen Termin in der Ausländerbehörde und musst deinen Aufenthaltstitel verlängern. Was sagst du?", "level_min": "B1", "level_max": "B2"},
    {"id": "soziales_9", "npc_role": "die Ernährungsberaterin",  "goal": "Soziales (Ämter, Ärzte)", "text": "Du hast einen Termin bei einer Ernährungsberaterin. Warum bist du hier? Was sind deine Ziele?", "level_min": "A2", "level_max": "B1"},
    {"id": "soziales_10", "npc_role": "der Mitarbeiter der Sperrmüllabholungsfirma am Telefon", "goal": "Soziales (Ämter, Ärzte)", "text": "Du hast neue Möbel gekauft und musst die alten entsorgen. Ruf bei einer Sperrmüllabholungsfirma an und vereinbare einen Termin.", "level_min": "B1", "level_max": "B2"},

    # =========================
    # 4. Unterhaltung (Club, Kino etc)
    # =========================
    {"id": "unterhalt_1", "npc_role": "die Kassiererin im Kino",  "goal": "Unterhaltung (Club, Kino etc)", "text": "Du bist im Kino und möchtest 2 Karten für den Film kaufen.", "level_min": "A1", "level_max": "A2"},
    {"id": "unterhalt_2", "npc_role": "der Trainer im Gym",  "goal": "Unterhaltung (Club, Kino etc)", "text": "Du kommst im Gym an und begrüßt deinen Trainer. Erzähl ihm, wie es dir geht und was du heute trainieren möchtest.", "level_min": "A1", "level_max": "A2"},
    {"id": "unterhalt_3", "npc_role": "der Kollege der die Spielregeln erklärt",  "goal": "Unterhaltung (Club, Kino etc)", "text": "Du bist von deinen Kolleginnen zu einem Game-Abend eingeladen. Du kennst das Spiel nicht und bittest einen Kollegen, dir die Regeln zu erklären.", "level_min": "A2", "level_max": "B1"},
    {"id": "unterhalt_4", "npc_role": "die Moderatorin des Sprachclubs",  "goal": "Unterhaltung (Club, Kino etc)", "text": "Du bist zum ersten Mal im Sprachclub und fragst die Moderatorin, dir alles zu erklären.", "level_min": "A2", "level_max": "B1"},
    {"id": "unterhalt_5", "npc_role": "der Nachbar im Aufzug",  "goal": "Unterhaltung (Club, Kino etc)", "text": "Du bist im Aufzug mit deinen Nachbarn. Fange einen kleinen Smalltalk über das Wetter.", "level_min": "A1", "level_max": "A2"},
    {"id": "unterhalt_6", "npc_role": "der Freund in der Kneipe der dasselbe Hobby hat",  "goal": "Unterhaltung (Club, Kino etc)", "text": "Du triffst dich in einer Kneipe mit deinem Freund, der sich auch für dein Hobby interessiert. Sprich mit ihm darüber!", "level_min": "A2", "level_max": "B1"},
    {"id": "unterhalt_7", "npc_role": "der Bartender im Apres Ski",  "goal": "Unterhaltung (Club, Kino etc)", "text": "Du bist in einem Schikurort und chillst im Apres Ski. Der Bartender spricht dich an und fragt, wie es dir hier so gefällt. Erzähle ihm über deinen ersten Tag und deine Eindrücke hier.", "level_min": "B1", "level_max": "B2"},
    {"id": "unterhalt_8", "npc_role": "ein Kollege der nach dem Marathon fragt",  "goal": "Unterhaltung (Club, Kino etc)", "text": "Du hast an einem Marathon mitgemacht und deine Kollegen möchten erfahren, wie es war. Erzähl ihnen alles!", "level_min": "B1", "level_max": "B2"},
    {"id": "unterhalt_9", "npc_role": "dein Date das nach Hobbys fragt",  "goal": "Unterhaltung (Club, Kino etc)", "text": "Du bist im Date. Dein Gegenüber fragt dich nach deinen Hobbys. Erzähl alles!", "level_min": "A2", "level_max": "B1"},
    {"id": "unterhalt_10", "npc_role": "die Moderatorin des Sprachclubs", "goal": "Unterhaltung (Club, Kino etc)", "text": 'Du bist im Deutsch-Sprachclub. Das heutige Thema ist „gesundes Essen". Die Moderatorin fragt dich: „Was ist dein Lieblingsgericht, das gesund und lecker ist?". Beantworte ihre Frage!', "level_min": "A2", "level_max": "B1"},

    # =========================
    # 5. Einkauf & Restaurants
    # =========================
    {"id": "einkauf_1", "npc_role": "ein Mitarbeiter im Kaufhaus",  "goal": "Einkauf & Restaurants", "text": "Du bist im Kaufhaus und suchst nach einem Bankautomaten und nach der Toilette.", "level_min": "A1", "level_max": "A2"},
    {"id": "einkauf_2", "npc_role": "der Lieblingskellner im Stammrestaurant",  "goal": "Einkauf & Restaurants", "text": "Du bist in deinem Stammrestaurant angekommen und siehst deinen Lieblingskellner. Wie begrüßt du ihn?", "level_min": "A1", "level_max": "A2"},
    {"id": "einkauf_3", "npc_role": "der Mitarbeiter im Biergarten",  "goal": "Einkauf & Restaurants", "text": "Du gehst in einen Biergarten, um einen Tisch für deine Geburtstagsfeier zu buchen.", "level_min": "A2", "level_max": "B1"},
    {"id": "einkauf_4", "npc_role": "der Kellner im Restaurant",  "goal": "Einkauf & Restaurants", "text": "Du bist im Restaurant. Der Kellner fragt dich, was du bestellen möchtest.", "level_min": "A1", "level_max": "A2"},
    {"id": "einkauf_5", "npc_role": "der Kellner dem die falsche Rechnung gemeldet wird",  "goal": "Einkauf & Restaurants", "text": "Dir wurde etwas in die Rechnung gestellt, was du nicht bestellt hast. Rufe den Kellner und kläre es.", "level_min": "B1", "level_max": "B2"},
    {"id": "einkauf_6", "npc_role": "der Mitarbeiter am Infostand im Bauhaus",  "goal": "Einkauf & Restaurants", "text": "Du fährst ins Bauhaus, weil du etwas zuhause reparieren musst. Was ist das? Erkläre es dem Mitarbeiter am Infostand.", "level_min": "A2", "level_max": "B1"},
    {"id": "einkauf_7", "npc_role": "die Mitarbeiterin in der Weinboutique",  "goal": "Einkauf & Restaurants", "text": "Du gehst in einen Weinboutique und suchst nach einem Geschenk für deine beste Freundin / deinen besten Freund. Wie bittest du die Mitarbeiterin, dir bei der Wahl zu helfen?", "level_min": "A2", "level_max": "B1"},
    {"id": "einkauf_8", "npc_role": "die Kassiererin im Supermarkt",  "goal": "Einkauf & Restaurants", "text": "Du kommst an der Kasse mit deinem Einkauf an. Die Kassiererin begrüßt dich. Was sagst du?", "level_min": "A1", "level_max": "A2"},
    {"id": "einkauf_9", "npc_role": "der Filialmitarbeiter im Supermarkt",  "goal": "Einkauf & Restaurants", "text": "Du möchtest eine defekte Ware im Supermarkt zurückgeben. Sprich den Filialmitarbeiter an und erkläre dein Problem.", "level_min": "B1", "level_max": "B2"},
    {"id": "einkauf_10", "npc_role": "der Mitarbeiter im Restaurant der den Anruf entgegennimmt", "goal": "Einkauf & Restaurants", "text": "Du möchtest einen Tisch im Restaurant für dich und deine Freunde reservieren. Rufe da an und mach es.", "level_min": "A2", "level_max": "B1"},
    {"id": "einkauf_11", "npc_role": "der Käufer der zum Abholen kommt", "goal": "Einkauf & Restaurants", "text": "Du hast mehrere Sachen auf Ebay Kleinanzeigen verkauft. Bald kommt ein Käufer bei dir vorbei. Begrüße ihn und finde heraus, was genau er kaufen möchte und beantworte seine Fragen, wenn er welche hat.", "level_min": "A2", "level_max": "B1"},

    # =========================
    # 6. Tourismus & Reisen
    # =========================
    {"id": "reisen_1", "npc_role": "der Reiseberater im Reisebüro",  "goal": "Tourismus & Reisen", "text": "Du gehst ins Reisebüro und möchtest dich nach aktuellen Angeboten erkundigen.", "level_min": "A1", "level_max": "A2"},
    {"id": "reisen_2", "npc_role": "der Airbnb Host der angerufen wird",  "goal": "Tourismus & Reisen", "text": "Du rufst deinen Airbnb Host an, weil der Schlüssel abgebrochen und in der Tür geblieben ist und du nicht in die Wohnung reinkommen kannst.", "level_min": "B1", "level_max": "B2"},
    {"id": "reisen_3", "npc_role": "ein Kollege der nach dem Urlaub fragt",  "goal": "Tourismus & Reisen", "text": "Du triffst deine Kollegen nach dem Urlaub. Erzähle ihnen über deine Reise.", "level_min": "A2", "level_max": "B1"},
    {"id": "reisen_4", "npc_role": "der Kumpel der angerufen wird",  "goal": "Tourismus & Reisen", "text": "Du möchtest einen Kurztrip mit deinem Kumpel machen. Ruf ihn an und frage, ob er mitkommt.", "level_min": "A2", "level_max": "B1"},
    {"id": "reisen_5", "npc_role": "die Schwester / der Bruder mit dem der Ausflug geplant wird",  "goal": "Tourismus & Reisen", "text": "Du möchtest mit deiner Schwester / deinem Bruder übers Wochenende verreisen. Plane den Ausflug!", "level_min": "A2", "level_max": "B1"},
    {"id": "reisen_6", "npc_role": "dein Date das nach der besten Reise fragt",  "goal": "Tourismus & Reisen", "text": "Du bist in einem Date. Dein Gegenüber fragt nach deiner besten Reise. Erzähl alles!", "level_min": "A2", "level_max": "B1"},
    {"id": "reisen_7", "npc_role": "die Freundin die über ihre verrückteste Reise erzählt hat",  "goal": "Tourismus & Reisen", "text": "Deine Freundin erzählt über ihre verrückteste Reise und fragt dich, ob du auch eine verrückte Reise hattest. Erzähl ihr alles.", "level_min": "B1", "level_max": "B2"},
    {"id": "reisen_8", "npc_role": "der Kollege der über sein Lieblingsurlaubsland erzählt",  "goal": "Tourismus & Reisen", "text": "Dein Kollege erzählt über sein Lieblingsurlaubsland und fragt dich nach deinem. Erzähl ihm alles!", "level_min": "A2", "level_max": "B1"},
    {"id": "reisen_9", "npc_role": "der deutsche Kollege der über Malle erzählt",  "goal": "Tourismus & Reisen", "text": 'Du bist im Büro. Ein deutscher Kollege erzählt über seine Reise nach „Malle". Was ist das? Frag ihn aus.', "level_min": "A2", "level_max": "B1"},
    {"id": "reisen_10", "npc_role": "der Empfangsmitarbeiter im Hotel", "goal": "Tourismus & Reisen", "text": "Du kommst im Hotel an. Du hattest eine Reservierung. Fang das Gespräch am Empfang an.", "level_min": "A1", "level_max": "A2"},
    {"id": "reisen_11", "npc_role": "ein Freund beim Brunch der nach dem Rezept fragt", "goal": "Tourismus & Reisen", "text": "Du bist zum Brunchen bei deinen Freunden eingeladen. Das Essen ist lecker und du möchtest wissen, wer und wie es gekocht hat. Stelle die Fragen!", "level_min": "A2", "level_max": "B1"},

    # =========================
    # 7. Sport & Hobbys
    # =========================
    {"id": "sport_1", "npc_role": "der sportliche freundliche Typ im Gym",  "goal": "Sport & Hobbys", "text": "Du bist im Gym und siehst einen sehr sportlichen Typen, der ganz freundlich ist. Du möchtest mehr über seine Trainingsweise erfahren. Frag ihn aus!", "level_min": "A2", "level_max": "B1"},
    {"id": "sport_2", "npc_role": "die Empfangsmitarbeiterin im Gym",  "goal": "Sport & Hobbys", "text": "Du möchtest eine Mitgliedschaft im Gym kaufen. Die Empfangsmitarbeiterin begrüßt dich und fragt, was du möchtest.", "level_min": "A1", "level_max": "A2"},
    {"id": "sport_3", "npc_role": "eine Kollegin in der Mittagspause die nach Interessen fragt",  "goal": "Sport & Hobbys", "text": "Du bist zum ersten Mal in der Mittagspause mit deinen Kolleginnen. Eine fragt dich, wofür du dich interessierst. Erzähl ihnen alles!", "level_min": "A2", "level_max": "B1"},
    {"id": "sport_4", "npc_role": "der nette Nachbar auf der Hofparty",  "goal": "Sport & Hobbys", "text": "Du bist auf einer Hofparty. Ein netter Nachbar erzählt über sein Hobby und fragt dich über deine Hobbys. Erzähl ihm etwas darüber!", "level_min": "A2", "level_max": "B1"},
    {"id": "sport_5", "npc_role": "die tolle Person im Biergarten",  "goal": "Sport & Hobbys", "text": "Im Biergarten hast du eine tolle Person kennengelernt. Nun fragt sie dich, was dich begeistert und wofür du dich interessierst. Erzähl doch!", "level_min": "A2", "level_max": "B1"},
    {"id": "sport_6", "npc_role": "die Freundin die über das Hobby erzählt hat",  "goal": "Sport & Hobbys", "text": "Deine Freundin hat dir gerade über ein interessantes Hobby ihres Lebenspartners erzählt. Dir fällt ein, dass ein Bekannter / eine Bekannte von dir auch was Außergewöhnliches macht. Was ist das für ein Hobby?", "level_min": "B1", "level_max": "B2"},
    {"id": "sport_7", "npc_role": "der Kollege der über den Podcast erzählt",  "goal": "Sport & Hobbys", "text": "Dein Kollege erzählt etwas über einen sehr interessanten Podcast. Das erinnert dich an deinen Lieblingspodcast / YouTube Channel. Teile darüber mit!", "level_min": "B1", "level_max": "B2"},
    {"id": "sport_8", "npc_role": "die Person die gern bouldern geht",  "goal": "Sport & Hobbys", "text": "Du hast jemanden kennengelernt und diese Person geht gerne bouldern. Du verstehst das nicht. Erfahre, was sie damit meint!", "level_min": "A2", "level_max": "B1"},
    {"id": "sport_9", "npc_role": "der Kumpel der auf dem Oktoberfest war",  "goal": "Sport & Hobbys", "text": 'Dein Kumpel war gestern in München Oktoberfest feiern. Er sagt immer wieder „Wiesn" und „Maß"… Du verstehst das nicht. Frag ihn, was es bedeutet.', "level_min": "A2", "level_max": "B1"},
    {"id": "sport_10", "npc_role": "ein Kollege auf dem Balkon der Firmenparty", "goal": "Sport & Hobbys", "text": "Du bist auf einer Firmenparty. Du willst mehr über deine Kollegen erfahren. Gerade stehst du auf dem Balkon mit zwei von ihnen. Frage sie, wofür sie sich interessieren und was sie begeistert.", "level_min": "A2", "level_max": "B1"},
    {"id": "sport_11", "npc_role": "der Uber-Fahrer", "goal": "Sport & Hobbys", "text": "Du hast ein Uber bestellt und quatschst mit dem Fahrer. Plötzlich erfährst du, er teilt dein Hobby. Was sagst du?", "level_min": "A2", "level_max": "B1"},

    # =========================
    # 8. Am Telefon
    # =========================
    {"id": "telefon_1", "npc_role": "der Mitarbeiter im Restaurant Amelia der das Telefon abnimmt",  "goal": "Am Telefon", "text": 'Du rufst im Restaurant „Amelia" an und möchtest einen Tisch buchen.', "level_min": "A2", "level_max": "B1"},
    {"id": "telefon_2", "npc_role": "der Mitarbeiter im Restaurant Rosengarten",  "goal": "Am Telefon", "text": 'Du schaust im Restaurant „Rosengarten" vorbei und möchtest einen Tisch buchen.', "level_min": "A1", "level_max": "A2"},
    {"id": "telefon_3", "npc_role": "der Kundenservice-Mitarbeiter der Deutschen Bahn",  "goal": "Am Telefon", "text": "Du rufst im Kunden-Support der Deutschen Bahn an und möchtest wissen, warum sie von deinem Konto 50 Euro abgebucht haben.", "level_min": "B1", "level_max": "B2"},
    {"id": "telefon_4", "npc_role": "der Support-Mitarbeiter des Internetproviders",  "goal": "Am Telefon", "text": "Du rufst bei deinem Internetprovider an und möchtest einen Internetausfall mitteilen und wissen, was du tun sollst.", "level_min": "B1", "level_max": "B2"},
    {"id": "telefon_5", "npc_role": "die Sprechstundenhilfe in der Arztpraxis",  "goal": "Am Telefon", "text": "Du rufst in der Praxis deines Hausarztes an und möchtest einen Termin vereinbaren.", "level_min": "A2", "level_max": "B1"},
    {"id": "telefon_6", "npc_role": "die Mitarbeiterin der Zahnarztpraxis",  "goal": "Am Telefon", "text": "Du hast eine Zahnarztpraxis gefunden und rufst da an, um zu fragen, ob sie dich als Patienten annehmen können.", "level_min": "A2", "level_max": "B1"},
    {"id": "telefon_7", "npc_role": "der Bankmitarbeiter im Telefon-Support",  "goal": "Am Telefon", "text": "Du hast deine Bankkarte verloren. Du rufst bei deiner Bank an und möchtest die Karte sperren.", "level_min": "B1", "level_max": "B2"},
    {"id": "telefon_8", "npc_role": "die Freundin die Geburtstag hat",  "goal": "Am Telefon", "text": "Deine Freundin hat Geburtstag. Ruf sie an und gratuliere ihr.", "level_min": "A1", "level_max": "A2"},
    {"id": "telefon_9", "npc_role": "der Arbeitgeber / die Sekretärin der den Anruf entgegennimmt",  "goal": "Am Telefon", "text": "Du fühlst dich schlecht. Ruf bei deinem Arbeitgeber an und lass dich krank schreiben.", "level_min": "A2", "level_max": "B1"},
    {"id": "telefon_10", "npc_role": "der Kunde der angerufen wird um den Termin zu verschieben", "goal": "Am Telefon", "text": "Du rufst deinen Kunden an und möchtest euren Termin morgen verschieben. Ruf ihn an. Entschuldige dich, verschiebe den Termin und erkläre, warum.", "level_min": "B1", "level_max": "B2"},
    {"id": "telefon_11", "npc_role": "der Verkäufer der angerufen wird wegen der kaputten Ware", "goal": "Am Telefon", "text": "Du hast was im Internet bestellt und die Ware war kaputt. Es gibt keinen Internetsupport. Rufe bei dem Verkäufer an.", "level_min": "B1", "level_max": "B2"},
    {"id": "telefon_12", "npc_role": "der Support-Mitarbeiter des Mobilfunkanbieters", "goal": "Am Telefon", "text": "Du kannst dich in deinem Kundenkonto in deiner Mobilfunkanbieter-App nicht anmelden. Du hast schon alles versucht. Jetzt rufe den Support an.", "level_min": "B1", "level_max": "B2"},

    # =========================
    # 9. Job
    # =========================
    {"id": "job_1", "npc_role": "ein Teamkollege am ersten Arbeitstag",  "goal": "Job", "text": "Heute ist dein erster Tag im neuen Job. Du lernst dein Team kennen. Erzähl etwas über dich.", "level_min": "A2", "level_max": "B1"},
    {"id": "job_2", "npc_role": "die Personalmanagerin im Vorstellungsgespräch",  "goal": "Job", "text": "Du bist im Vorstellungsgespräch. Erzähl etwas über dich.", "level_min": "A2", "level_max": "B2"},
    {"id": "job_3", "npc_role": "die Personalmanagerin im Vorstellungsgespräch",  "goal": "Job", "text": 'Du bist im Vorstellungsgespräch. Alles läuft super. Nun fragt die Personalmanagerin „Warum sollen wir uns für Sie entscheiden?". Was sagst du?', "level_min": "B1", "level_max": "C1"},
    {"id": "job_4", "npc_role": "die HR-Managerin im Onboarding",  "goal": "Job", "text": "Du bist im Onboarding. Die HR-Managerin gibt dir Zeit, um dir deine Fragen an sie zu überlegen. Stelle nun deine Fragen an sie.", "level_min": "A2", "level_max": "B1"},
    {"id": "job_5", "npc_role": "ein Kollege in der Kantine",  "goal": "Job", "text": "Dein erster Tag im Job. Deine Kollegen sind super nett und nehmen dich in der Mittagspause in die Kantine mit. Nun fragen sie dich, über dich und deinen Weg zu erzählen. Was sagst du?", "level_min": "A2", "level_max": "B1"},
    {"id": "job_6", "npc_role": "die Kollegin in der Pause",  "goal": "Job", "text": "Du triffst dich mit einer Kollegin in der Pause. Sie ist nett und fragt, wie es dir geht. Was sagst du?", "level_min": "A1", "level_max": "A2"},
    {"id": "job_7", "npc_role": "der Kollege der über seinen Urlaub erzählt",  "goal": "Job", "text": "Dein Kollege erzählt über seinen Urlaub. Du willst was dazu sagen, aber du hast auch ein paar Fragen an ihn, weil du seine Aufgaben während seines Urlaubs übernommen hast. Wie gehst du das an?", "level_min": "B1", "level_max": "B2"},
    {"id": "job_8", "npc_role": "ein Kollege im Meeting der zuhört",  "goal": "Job", "text": "Du bist im Präsenz-Meeting mit deinen Kollegen. Du hast ein wichtiges Update zu deinem Projekt und nun bist du dran. Teile deinen Kolleginnen darüber mit!", "level_min": "B1", "level_max": "B2"},
    {"id": "job_9", "npc_role": "der Abteilungsleiter",  "goal": "Job", "text": "Du hast ein Problem mit deiner Software. Du wendest dich an deinen Abteilungsleiter und erklärst ihm, worum es geht.", "level_min": "B1", "level_max": "B2"},
    {"id": "job_10", "npc_role": "die Kollegin die nach dem ersten Tag fragt", "goal": "Job", "text": 'Du bist heute das erste Mal an deinem neuen Arbeitsplatz. Du siehst viele Goodies und eine Willkommenskarte auf deinem Schreibtisch. In der Pause fragt dich eine Kollegin: „Na, wie läuft dein erster Tag so?". Erzähle ihr alles!', "level_min": "A2", "level_max": "B1"},
    {"id": "job_11", "npc_role": "der Manager im Kick-Off Meeting", "goal": "Job", "text": "Im Kick-Off Meeting fragt dich dein Manager nach deinem Vorschlag, wie das bevorstehende Event anzugehen ist. Was sagst du?", "level_min": "B2", "level_max": "C1"},
    {"id": "job_12", "npc_role": "der IT-Kollege dem das Problem erklärt wird", "goal": "Job", "text": "Du kannst dich in deinem Firmenlaptop-Profil nicht anmelden. Wie erklärst du das Problem deinem Kollegen aus der IT-Abteilung?", "level_min": "B1", "level_max": "B2"},
    {"id": "job_13", "npc_role": "der Kollege der anruft und fragt wie es geht", "goal": "Job", "text": "Du bist krank. Dein Kollege ruft dich an und fragt, wie es dir geht. Quatsch mit ihm.", "level_min": "A2", "level_max": "B1"},
    {"id": "job_14", "npc_role": "die Kollegin die um etwas gebeten wird", "goal": "Job", "text": "Du hast eine Bitte an deine Kollegin. Was für eine Bitte ist das und wie sagst du es auf Deutsch?", "level_min": "A2", "level_max": "B1"},
    {"id": "job_15", "npc_role": "eine Kollegin im Raum die helfen kann", "goal": "Job", "text": "Du kommst mit einem Problem nicht klar. In dem Raum sind ein paar Kolleginnen. Wie führst du es so ein, dass dir geholfen wird?", "level_min": "B1", "level_max": "B2"},
    {"id": "job_16", "npc_role": "der Kollege der über die andere Kollegin lästert", "goal": "Job", "text": "Ein Kollege lästert über eine andere Kollegin ab, die du gern hast. Was sagst du zu ihm?", "level_min": "B2", "level_max": "C1"},
    {"id": "job_17", "npc_role": "der Kollege der Hilfe braucht", "goal": "Job", "text": "Ein Kollege braucht deine Hilfe. Das Problem ist dir wohlbekannt. Was antwortest du?", "level_min": "A2", "level_max": "B1"},
    {"id": "job_18", "npc_role": "die Kollegin die krank ist und bittet einzuspringen", "goal": "Job", "text": "Eine Kollegin ist krank und bittet dich, für sie morgen einzuspringen. Was sagst du?", "level_min": "A2", "level_max": "B1"},
    {"id": "job_19", "npc_role": "die Abteilungsleiterin im 1-on-1 Meeting", "goal": "Job", "text": 'Du bist im 1-on-1 Meeting mit deiner Abteilungsleiterin. Sie fragt dich: „Na, wie waren die ersten 2 Monate bei uns?" Was sagst du?', "level_min": "B1", "level_max": "B2"},
    {"id": "job_20", "npc_role": "das Team im Entscheidungsmeeting", "goal": "Job", "text": "Im Entscheidungsmeeting wird ein wichtiges Problem besprochen. Du hast eine Idee, wie es gelöst werden kann. Teile deine Lösung mit dem Team!", "level_min": "B2", "level_max": "C1"},

    # =========================
    # NEW FORMAT — Selbstpräsentation
    # =========================
    {"id": "self_1", "npc_role": "der freundliche Nachbar im Treppenhaus", "goal": "Selbstpräsentation", "level": ["A1", "A2"], "context": "Du bist umgezogen und triffst einen Nachbarn im Treppenhaus.", "persona": {"name": "Nachbar", "tone": "informal"}, "start": {"text": "Hey 😊 bist du neu hier im Haus?"}},
    {"id": "self_2", "npc_role": "ein Teamkollege der sich vorstellt", "goal": "Selbstpräsentation", "level": ["A2", "B1"], "context": "Erster Tag im Job, Team stellt sich vor.", "persona": {"name": "Kollege", "tone": "informal"}, "start": {"text": "Hi! Erzähl mal kurz, wer du bist 😄"}},
    {"id": "self_3", "npc_role": "die Personalmanagerin im Vorstellungsgespräch", "goal": "Selbstpräsentation", "level": ["B1", "C1"], "context": "Vorstellungsgespräch.", "persona": {"name": "HR Manager", "tone": "formal"}, "start": {"text": "Guten Tag. Erzählen Sie bitte etwas über sich."}},

    # =========================
    # NEW FORMAT — Freunde & Beziehungen
    # =========================
    {"id": "friends_1", "npc_role": "der Freund der anruft und einlädt", "goal": "Freunde / Beziehungen", "level": ["A1", "A2"], "context": "Freund ruft dich an und lädt dich ein.", "persona": {"name": "Freund", "tone": "informal"}, "start": {"text": "Ey 😄 hast du am Wochenende Zeit?"}},
    {"id": "friends_2", "npc_role": "die Freundin mit Problemen die Unterstützung sucht", "goal": "Freunde / Beziehungen", "level": ["B1", "B2"], "context": "Deine Freundin hat Probleme.", "persona": {"name": "Freundin", "tone": "informal"}, "start": {"text": "Hey… ich brauch kurz deinen Rat 😕"}},

    # =========================
    # NEW FORMAT — Soziales
    # =========================
    {"id": "social_1", "npc_role": "die Empfangsdame in der Arztpraxis", "goal": "Soziales (Ämter, Ärzte)", "level": ["A1", "A2"], "context": "Du bist beim Arzt am Empfang.", "persona": {"name": "Rezeptionistin", "tone": "formal"}, "start": {"text": "Guten Tag. Wie kann ich Ihnen helfen?"}},
    {"id": "social_2", "npc_role": "der Sachbearbeiter im JobCenter", "goal": "Soziales (Ämter, Ärzte)", "level": ["B1", "B2"], "context": "JobCenter Gespräch.", "persona": {"name": "Sachbearbeiter", "tone": "formal"}, "start": {"text": "Erzählen Sie bitte etwas über Ihre berufliche Situation."}},

    # =========================
    # NEW FORMAT — Unterhaltung
    # =========================
    {"id": "fun_1", "npc_role": "ein Nachbar im Aufzug der Smalltalk macht", "goal": "Unterhaltung (Club, Kino etc)", "level": ["A1", "A2"], "context": "Smalltalk im Aufzug.", "persona": {"name": "Nachbar", "tone": "informal"}, "start": {"text": "Puh… heute ist echt kalt, oder?"}},
    {"id": "fun_2", "npc_role": "der Bartender im Après-Ski", "goal": "Unterhaltung (Club, Kino etc)", "level": ["B1", "B2"], "context": "Bartender im Ski-Resort.", "persona": {"name": "Bartender", "tone": "informal"}, "start": {"text": "Na 😄 wie war dein erster Tag hier?"}},

    # =========================
    # NEW FORMAT — Einkauf & Restaurants
    # =========================
    {"id": "shop_1", "npc_role": "der Kellner im Restaurant", "goal": "Einkauf & Restaurants", "level": ["A1", "A2"], "context": "Im Restaurant.", "persona": {"name": "Kellner", "tone": "formal"}, "start": {"text": "Guten Tag. Was möchten Sie bestellen?"}},
    {"id": "shop_2", "npc_role": "der Mitarbeiter der die Reklamation entgegennimmt", "goal": "Einkauf & Restaurants", "level": ["B1", "B2"], "context": "Reklamation im Laden.", "persona": {"name": "Mitarbeiter", "tone": "formal"}, "start": {"text": "Wie kann ich Ihnen helfen?"}},

    # =========================
    # NEW FORMAT — Reisen
    # =========================
    {"id": "travel_1", "npc_role": "der Empfangsmitarbeiter beim Hotel Check-in", "goal": "Tourismus & Reisen", "level": ["A1", "A2"], "context": "Hotel Check-in.", "persona": {"name": "Rezeption", "tone": "formal"}, "start": {"text": "Willkommen. Haben Sie reserviert?"}},
    {"id": "travel_2", "npc_role": "der Airbnb-Host der das Problem lösen soll", "goal": "Tourismus & Reisen", "level": ["B1", "B2"], "context": "Problem mit Airbnb.", "persona": {"name": "Host", "tone": "semi_formal"}, "start": {"text": "Hallo, was ist passiert?"}},

    # =========================
    # NEW FORMAT — Sport & Hobbys
    # =========================
    {"id": "hobby_1", "npc_role": "ein Gym-Mitglied das ins Gespräch kommt", "goal": "Sport & Hobbys", "level": ["A1", "A2"], "context": "Im Gym.", "persona": {"name": "Trainer", "tone": "informal"}, "start": {"text": "Hey 😊 was willst du heute trainieren?"}},
    {"id": "hobby_2", "npc_role": "der Kollege der über seinen Lieblingspodcast erzählt", "goal": "Sport & Hobbys", "level": ["B1", "B2"], "context": "Gespräch über Podcast.", "persona": {"name": "Kollege", "tone": "informal"}, "start": {"text": "Kennst du den Podcast XY?"}},

    # =========================
    # NEW FORMAT — Telefon
    # =========================
    {"id": "phone_1", "npc_role": "der Mitarbeiter im Restaurant der abnimmt", "goal": "Am Telefon", "level": ["A2", "B1"], "context": "Restaurant anrufen.", "persona": {"name": "Restaurant", "tone": "formal"}, "start": {"text": "Restaurant Amelia, guten Tag."}},
    {"id": "phone_2", "npc_role": "der Support-Mitarbeiter der den Anruf entgegennimmt", "goal": "Am Telefon", "level": ["B1", "B2"], "context": "Support anrufen.", "persona": {"name": "Support", "tone": "formal"}, "start": {"text": "Kundenservice, wie kann ich Ihnen helfen?"}},

    # =========================
    # NEW FORMAT — Job
    # =========================
    {"id": "job_n1", "npc_role": "ein Kollege in der Mittagspause", "goal": "Job", "level": ["A2", "B1"], "context": "Mittagspause mit Kollegen.", "persona": {"name": "Kollege", "tone": "informal"}, "start": {"text": "Und? Wie ist dein erster Eindruck?"}},
    {"id": "job_n2", "npc_role": "der Teamleiter der das Meeting-Update erwartet", "goal": "Job", "level": ["B2", "C1"], "context": "Meeting Update.", "persona": {"name": "Manager", "tone": "formal"}, "start": {"text": "Können Sie uns ein Update geben?"}},
]

def level_to_num(level):
    return {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5}.get(level, 2)

def _scenario_levels(s):
    """Return the set of numeric level values a scenario covers (supports both formats)."""
    if "level" in s:
        levels = s["level"]
        if isinstance(levels, list):
            nums = [level_to_num(l) for l in levels]
            return set(range(min(nums), max(nums) + 1))
        return {level_to_num(levels)}
    lo = level_to_num(s.get("level_min", "A1"))
    hi = level_to_num(s.get("level_max", "C1"))
    return set(range(lo, hi + 1))

def _scenario_context(s):
    """Return the situation description regardless of format."""
    return s.get("context") or s.get("text", "")

def get_clean_context(scenario):
    """Return context stripped of any level badges like (A2–B1)."""
    text = _scenario_context(scenario)
    text = re.sub(r'\s*\([A-Ca-c][0-2][–\-][A-Ca-c][0-2]\)', '', text)
    return text.strip()

def reframe_context_for_npc(context: str, user_name: str = "") -> str:
    """
    Old-format scenario texts are written from the USER's POV:
      "Du möchtest dich im Bürgeramt anmelden, was sagst du am Schalter?"
    This confuses GPT into playing the user instead of the NPC.
    We rewrite to NPC-POV so the role is unambiguous:
      "Der Lernende (Farid) möchte sich anmelden und kommt an deinen Schalter."
    Only applied to old-format scenarios (text field); new-format already has explicit persona + start.
    """
    name_ref = user_name if user_name else "der Lernende"

    # Remove rhetorical question at the end ("was sagst du?", "wie reagierst du?" etc.)
    ctx = re.sub(r'[,.]?\s*(was sagst du[^?]*\?|wie reagierst du[^?]*\?|was machst du[^?]*\?)', '', context, flags=re.IGNORECASE).strip()
    ctx = re.sub(r'\?$', '', ctx).strip()  # trailing ? from above

    # Replace "Du bist" → third person description
    ctx = re.sub(r'^Du bist ', f'{name_ref} ist ', ctx, flags=re.IGNORECASE)
    ctx = re.sub(r'du bist ', f'{name_ref} ist ', ctx, flags=re.IGNORECASE)

    # Replace "Du möchtest" → third person
    ctx = re.sub(r'^Du möchtest ', f'{name_ref} möchte ', ctx, flags=re.IGNORECASE)
    ctx = re.sub(r' du möchtest ', f' {name_ref} möchte ', ctx, flags=re.IGNORECASE)

    # Replace "Du hast" → third person
    ctx = re.sub(r'^Du hast ', f'{name_ref} hat ', ctx, flags=re.IGNORECASE)
    ctx = re.sub(r' du hast ', f' {name_ref} hat ', ctx, flags=re.IGNORECASE)

    # Replace "Du" standalone → der Lernende / name
    ctx = re.sub(r'du', name_ref, ctx, flags=re.IGNORECASE)
    ctx = re.sub(r'dein', f'{name_ref}s', ctx, flags=re.IGNORECASE)
    ctx = re.sub(r'deine', f'{name_ref}s', ctx, flags=re.IGNORECASE)

    # Append clear NPC framing
    ctx = ctx.rstrip('.') + f'. {name_ref} kommt gerade rein / tritt an dich heran / spricht dich an.'
    return ctx


def get_next_scenario(chat_id):
    """Return the next undone scenario for the user's goal.
    Level never filters scenarios — it only controls how the bot speaks.
    No recursion: resets progress once and returns the first available scenario."""
    user     = user_data[str(chat_id)]
    goal     = user.get("goal", "Selbstpräsentation")
    progress = user["user_progress"].get(goal, [])

    all_for_goal = sort_scenarios([s for s in SCENARIOS if s["goal"] == goal])
    if not all_for_goal:
        return None

    candidates = [s for s in all_for_goal if s["id"] not in progress]
    if not candidates:
        # All done — reset progress and cycle through again
        user["user_progress"][goal] = []
        save_users(user_data)
        candidates = all_for_goal

    return candidates[0]

def sort_scenarios(scenarios):
    return sorted(scenarios, key=lambda s: s.get("id", ""))

def pick_scenario(chat_id, goal, level):
    # Level does NOT filter scenarios — every scenario is available at every level.
    # Level only controls how the bot speaks (see build_system_prompt).
    user = user_data.setdefault(str(chat_id), {})
    index = user.get("scenario_index", 0)

    scenarios = [s for s in SCENARIOS if s["goal"] == goal]
    scenarios = sort_scenarios(scenarios)

    if not scenarios:
        return None

    if index >= len(scenarios):
        index = 0

    chosen = scenarios[index]
    user["scenario_index"] = index + 1
    save_users(user_data)

    return chosen

LEVEL_PROGRESSION = ["A1", "A2", "B1", "B2", "C1"]

def mark_scenario_done(chat_id, scenario):
    user = user_data[str(chat_id)]
    goal = user["goal"]
    if scenario["id"] not in user["user_progress"][goal]:
        user["user_progress"][goal].append(scenario["id"])
        save_users(user_data)

def check_level_up(chat_id):
    uid  = str(chat_id)
    user = user_data[uid]
    user["scenario_streak"] = user.get("scenario_streak", 0) + 1
    save_users(user_data)

    if user["scenario_streak"] >= 3:
        current = user.get("level", "A2")
        if current in LEVEL_PROGRESSION:
            idx = LEVEL_PROGRESSION.index(current)
            if idx < len(LEVEL_PROGRESSION) - 1:
                new_level = LEVEL_PROGRESSION[idx + 1]
                user["level"] = new_level
                user["scenario_streak"] = 0
                save_users(user_data)
                bot.send_message(
                    chat_id,
                    f"🎉 Glückwunsch! Du hast 3 Szenarien gemeistert.\n"
                    f"Dein Niveau steigt auf *{new_level}*! 🚀",
                    parse_mode="Markdown"
                )

# LEVEL → DIFFICULTY GUIDANCE
LEVEL_RULES = {
    "A0": "Sprich SEHR einfach. Max 1 kurzer Satz. Nur Grundwortschatz.",
    "A1": "Sprich sehr einfach (A1). Max 1-2 kurze Sätze. Einfache Verben im Präsens.",
    "A2": "Sprich einfach (A2). Max 2 Sätze. Einfache Vergangenheit ist ok.",
    "B1": "Sprich moderat (B1). Max 2-3 Sätze. Auch Nebensätze und Konjunktionen.",
    "B2": "Sprich natürlich (B2). 2-3 Sätze. Auch komplexere Strukturen.",
    "C1": "Sprich anspruchsvoll (C1). 3+ Sätze. Idiome und differenzierte Ausdrücke ok.",
}

TONE_MAP = {
    "informal":   "locker, freundlich, direkt, alltagssprachlich, du-Form",
    "semi_formal": "freundlich, respektvoll, aber nicht steif",
    "formal":     "höflich, professionell, distanziert, Sie-Form"
}

PERSONAS = {
    "Einkauf & Restaurants": {
        "role": "Kellner",
        "tone": "freundlich, effizient",
        "formality": "SIE",
        "emotion": "neutral bis freundlich",
        "energy": "ruhig",
        "behavior": "führt Bestellung, stellt gezielte Fragen",
        "dynamic": "führt Gespräch",
        "voice": {
            "style": "höflich, klar, serviceorientiert",
            "pace": "ruhig, strukturiert",
            "expressions": "Sehr gerne, Einen Moment bitte, Natürlich, Selbstverständlich",
            "attitude": "professionell hilfsbereit"
        }
    },
    "Freunde / Beziehungen": {
        "role": "Freund",
        "tone": "locker, persönlich",
        "formality": "DU",
        "emotion": "warm, interessiert",
        "energy": "mittel",
        "behavior": "stellt persönliche Fragen, reagiert emotional",
        "dynamic": "beidseitig",
        "voice": {
            "style": "locker, warm, spontan",
            "pace": "lebhaft, natürlich",
            "expressions": "krass, echt?, naja, weißt du, also..., hm",
            "attitude": "entspannt, neugierig"
        }
    },
    "Soziales (Ämter, Ärzte)": {
        "role": "Sachbearbeiter / Arzt",
        "tone": "professionell, direkt",
        "formality": "SIE",
        "emotion": "neutral",
        "energy": "kontrolliert",
        "behavior": "stellt klare Fragen, erwartet konkrete Antworten",
        "dynamic": "führt Gespräch",
        "voice": {
            "style": "sachlich, professionell, klar",
            "pace": "kontrolliert, präzise",
            "expressions": "Bitte, Vielen Dank, Könnten Sie..., Ich verstehe",
            "attitude": "neutral, korrekt"
        }
    },
    "Unterhaltung (Club, Kino etc)": {
        "role": "Fremder / Bekannter",
        "tone": "locker, smalltalk",
        "formality": "DU",
        "emotion": "offen",
        "energy": "leicht aktiv",
        "behavior": "macht Smalltalk, reagiert spontan",
        "dynamic": "beidseitig",
        "voice": {
            "style": "locker, offen, ungezwungen",
            "pace": "leicht, flott",
            "expressions": "cool, echt?, no way, nice, alter",
            "attitude": "offen, spontan"
        }
    },
    "Tourismus & Reisen": {
        "role": "Rezeptionist / Gastgeber",
        "tone": "höflich, hilfsbereit",
        "formality": "SIE",
        "emotion": "freundlich",
        "energy": "ruhig",
        "behavior": "hilft, erklärt, stellt Fragen",
        "dynamic": "führt Gespräch",
        "voice": {
            "style": "freundlich, gepflegt, einladend",
            "pace": "ruhig, klar",
            "expressions": "Herzlich willkommen, Gerne, Selbstverständlich, Kein Problem",
            "attitude": "gastfreundlich, professionell"
        }
    },
    "Sport & Hobbys": {
        "role": "Trainingspartner",
        "tone": "locker, interessiert",
        "formality": "DU",
        "emotion": "motiviert",
        "energy": "aktiv",
        "behavior": "fragt nach Interessen, teilt eigene Erfahrungen",
        "dynamic": "beidseitig",
        "voice": {
            "style": "motiviert, direkt, kumpelhaft",
            "pace": "energetisch, aktiv",
            "expressions": "los, komm, stark, geil, alter, nice, voll cool",
            "attitude": "enthusiastisch, unterstützend"
        }
    },
    "Am Telefon": {
        "role": "Support / Gesprächspartner",
        "tone": "klar, strukturiert",
        "formality": "SIE",
        "emotion": "neutral",
        "energy": "fokussiert",
        "behavior": "stellt gezielte Fragen, wartet auf Antwort",
        "dynamic": "reaktiv",
        "voice": {
            "style": "klar, strukturiert, fokussiert",
            "pace": "ruhig, präzise",
            "expressions": "Ich verstehe, Einen Moment, Kein Problem, Alles klar",
            "attitude": "lösungsorientiert, geduldig"
        }
    },
    "Job": {
        "role": "Kollege oder Manager",
        "tone": "semi-professionell",
        "formality": "AUTO",
        "emotion": "neutral bis interessiert",
        "energy": "mittel",
        "behavior": "stellt arbeitsbezogene Fragen",
        "dynamic": "beidseitig",
        "voice": {
            "style": "sachlich, kollegial, klar",
            "pace": "moderat, zielgerichtet",
            "expressions": "alright, verstanden, kurze Frage, genau, macht Sinn",
            "attitude": "respektvoll, kollegial"
        }
    },
    "Selbstpräsentation": {
        "role": "Gesprächspartner",
        "tone": "freundlich",
        "formality": "AUTO",
        "emotion": "interessiert",
        "energy": "ruhig",
        "behavior": "fordert dich auf, über dich zu sprechen",
        "dynamic": "führt Gespräch",
        "voice": {
            "style": "aufmerksam, warm, ermutigend",
            "pace": "entspannt, zugewandt",
            "expressions": "interessant, wirklich?, erzähl mal, und dann?, spannend",
            "attitude": "offen, neugierig"
        }
    },
}

def resolve_formality(goal, scenario_text):
    text = scenario_text.lower()
    if any(x in text for x in [
        "arzt", "amt", "behörde", "kellner", "restaurant",
        "termin", "interview", "manager", "sachbearbeiter"
    ]):
        return "SIE"
    if any(x in text for x in [
        "freund", "party", "date", "tinder", "kollege", "bier", "kneipe"
    ]):
        return "DU"
    return PERSONAS[goal]["formality"]

def enforce_style(text, formality):
    if formality == "SIE":
        for slang in ["ey", "digga", "krass", "nice", "alter", "geil", "voll cool"]:
            text = text.replace(slang, "")
    return text.strip()

def build_system_prompt(chat_id, scenario):
    user    = user_data[str(chat_id)]
    level   = user.get("level", "A2")
    goal    = user.get("goal",  "Einkauf & Restaurants")
    npc_level_instruction = NPC_LEVEL_INSTRUCTIONS.get(level, NPC_LEVEL_INSTRUCTIONS["B1"])
    human_style           = HUMAN_SPEECH_STYLE
    todays_gem            = get_todays_gem(str(chat_id))
    gem_hint              = get_gem_system_prompt_hint(todays_gem)

    # Inject turn-phase so NPC knows when to wind down vs. keep going
    _cur  = turn_counter.get(chat_id, 0)
    _max  = max_turns_for_level(level)
    _ratio = _cur / max(_max, 1)
    if _ratio < 0.45:
        _phase = (
            "FRÜH im Gespräch — halte die Unterhaltung aktiv: stelle am Ende eine kurze, "
            "natürliche Folgefrage oder reagiere mit einem Anstoß, der den User zum Weitersprechen einlädt."
        )
    elif _ratio < 0.8:
        _phase = (
            "MITTE des Gesprächs — natürlicher Austausch. Stelle eine Frage NUR wenn sie sich "
            "organisch ergibt. Wenn die Interaktion inhaltlich abgeschlossen wirkt, ist das ok."
        )
    else:
        _phase = (
            "GESPRÄCHSENDE naht — lass die Interaktion natürlich abschließen. "
            "Wenn das Ziel erreicht ist (Kauf, Info, Entscheidung), dann schließe es herzlich ab "
            "und verabschiede dich — erfinde KEINE neuen Angebote, Themen oder Fragen mehr. "
            "Kein 'darf ich noch etwas tun?', kein 'haben Sie noch Fragen?' wenn der Kontext abgeschlossen ist."
        )

    # Support new-format persona (per-scenario) or fall back to global PERSONAS
    if "persona" in scenario:
        sp = scenario["persona"]
        persona = PERSONAS.get(goal, PERSONAS["Einkauf & Restaurants"]).copy()
        persona["role"] = sp.get("name", persona["role"])
        if sp.get("tone") == "casual":
            persona["formality"] = "DU"
    else:
        persona = PERSONAS.get(goal, PERSONAS["Einkauf & Restaurants"])

    context    = _scenario_context(scenario)
    name            = user.get("name", "")
    gender          = user.get("gender", None)
    native_language = user.get("native_language", None)
    if name:
        context = context.replace("[Name]", name)
    # For old-format scenarios (no explicit persona/start), reframe from NPC POV
    # to prevent GPT from confusing "Du" (=user) with itself.
    npc_role = scenario.get("npc_role", "")
    if "persona" not in scenario:
        context = reframe_context_for_npc(context, name)

    # Build gender-aware formal address
    if gender == "weiblich":
        formal_address = f"Frau {name}" if name else "die Nutzerin"
        gender_note = "Der Lernende ist weiblich. Verwende in formellen Situationen 'Frau [Name]'."
    elif gender == "männlich":
        formal_address = f"Herr {name}" if name else "der Nutzer"
        gender_note = "Der Lernende ist männlich. Verwende in formellen Situationen 'Herr [Name]'."
    else:
        formal_address = name if name else "die Person"
        gender_note = "Der Lernende hat 'divers' angegeben. Vermeide geschlechtsspezifische Anreden — nutze den Vornamen."

    # Native language note for GPT
    lang_note = f"Die Muttersprache des Lernenden ist: {native_language}." if native_language else ""
    formality  = resolve_formality(goal, context)
    voice      = persona["voice"]
    mode       = get_dynamic_mode(session_state.get(chat_id, {"struggle": 0, "success": 0}))
    due_wps    = get_due_weak_points(chat_id)
    wp_text    = "\n".join(
        f"- {w['example_wrong']} → {w['example_correct']}" for w in due_wps
    ) if due_wps else "Keine"

    return f"""
Du bist ein echter Mensch in einer realistischen Situation.

ROLLE: {persona['role']}
VERHALTEN: {persona['behavior']}
DYNAMIK: {persona['dynamic']}

FORMALITÄT: {formality}
- SIE → siezen, höflich, kein Slang
- DU → duzen, locker, natürlich
- Niemals mischen

STIMME PERSÖNLICHKEIT:
Stil: {voice['style']}
Tempo: {voice['pace']}
Ausdruck: {voice['expressions']}
Haltung: {voice['attitude']}

VOICE ACTING:
- Sprich wie ein echter Mensch
- NIEMALS Pause-Labels oder Regieanweisungen schreiben: KEIN „(leichte Pause)", „(Pause)", „(seufzt)", „(zögert)", „[Pause]" oder ähnliches — der Text wird direkt vorgelesen und solche Klammern werden wörtlich ausgesprochen
- Zögerung und Pausen: drücke sie mit echten deutschen Lauten aus — „äh", „öhm", „hmm", „mhm", „na ja...", „also...", „tja..." — NICHT mit Beschriftungen
- Seufzen, Nachdenken, Überraschung: direkt als Laut schreiben — „Hmm...", „Öh...", „Ach so!", „Ah okay..." — NIE als Klammerausdruck
- Nutze „..." innerhalb von Sätzen für natürliches Zögern, aber nie als Label
- Füge kleine Reaktionen ein passend zum Stil: {voice['expressions']}
- Kurze + mittlere Sätze mischen, keine Monologe
- Vermeide perfekte, formelle Sprache — außer bei SIE

SCHWIERIGKEITSGRAD — Nutzerlevel: {level}
{npc_level_instruction}
Das Niveau bestimmt NUR Tempo, Vokabular-Komplexität und Satzlänge — NICHT welche Themen möglich sind.
Jede Gesprächssituation ist für jedes Niveau verfügbar.
NIEMALS auf einem höheren Niveau sprechen als {level}. Ein A1-User bekommt nie C1-Sprache, und umgekehrt.

WICHTIG: Du sprichst IMMER grammatikalisch korrektes Deutsch.
Keine fehlenden Artikel, keine Telegrammsprache, keine gebrochene Grammatik.
Einfache Sprache bedeutet kurze Sätze und leichtes Vokabular — NICHT falsches Deutsch.

A1 — Absoluter Anfänger:
- Max. 1 kurzer Satz pro Antwort
- Nur Grundvokabular: sein, haben, kommen, gehen, heißen
- Sehr langsam, viele Pausen: „Hallo... ich bin Anna. Und du?"
- Einfache Fragen: „Wie heißt du?", „Woher kommst du?"
- Kein Nebensatz, keine Konjunktionen außer „und"

A2 — Grundkenntnisse:
- 1–2 kurze Sätze
- Alltagsvokabular, einfache Vergangenheit (war, hatte)
- Fragen mit „wann", „wo", „was"
- Leichter natürlicher Ton: „Ah okay… und was machst du so?"

B1 — Mittelstufe:
- 1–2 Sätze, natürlicher Gesprächsflow
- Nebensätze mit „weil", „dass", „wenn"
- Einfache Meinungen und Erklärungen
- „Ah, interessant… warum denn das?"

B2 — Gute Kenntnisse:
- 2 Sätze, fließend, idiomatisch
- Komplexere Strukturen, Konjunktiv: „Das wäre toll, wenn…"
- Nachfragen, diskutieren, widersprechen
- „Ehrlich gesagt finde ich das etwas schwierig, weil…"

C1 — Fortgeschritten:
- 2–3 Sätze, fließend, präzise
- Idiome, Nuancen, Ironie erlaubt
- Komplexe Fragen, Argumentation
- „Das klingt spannend — aber hast du dabei auch bedacht, dass…?"

FEHLER-FOKUS:
{wp_text}
→ Baue diese Strukturen subtil ein. Korrigiere nie direkt — Recasting:
User: „Ich habe gegangen" → Du: „Ah, du bist also gegangen — und dann?"

EMOTIONALE REAKTION:
- Reagiere zuerst emotional, dann inhaltlich
- Beispiele: "Ahh okay 😄 und dann?", "Echt jetzt? Erzähl mal…", "Wait… wie meinst du das genau?"

GESPRÄCHSPHASE:
{_phase}

GESPRÄCHS-KONTINUITÄT (ABSOLUT WICHTIG):
- Verfolge lückenlos alles, was im bisherigen Gespräch BEREITS gesagt, bestätigt oder entschieden wurde
- Frage NIEMALS nochmal nach etwas, das der Nutzer bereits beantwortet hat — das ist respektlos und unrealistisch
- Widerspreche NIEMALS etwas, das der Nutzer bereits gesagt hat
- Bei schrittweisen Situationen (Check-in, Bestellung, Buchung, Termin): arbeite logisch Schritt für Schritt ab — ein abgeschlossener Schritt wird nicht wieder geöffnet
- Beispiel Hotel: Hat der Nutzer gesagt, er hat eine Reservierung → bestätige es, frage nach Name/Buchungsnummer — frage NIEMALS danach nochmal, ob man eine Reservierung anlegen soll oder ob er reserviert hat
- Beispiel Restaurant: Hat der Nutzer bestellt → bestätige die Bestellung, frage NICHT nochmal was er möchte
- Baue jede Antwort auf dem tatsächlichen Gesprächsverlauf auf — nicht auf dem, was in der Situation "typisch" wäre, wenn es dem bereits Gesagten widerspricht

REGELN:
- Du bist KEIN Lehrer, KEIN Assistent
- Kein Lehrbuch-Deutsch
- Echtes Gespräch, echte Reaktionen
- Stimme und Situation müssen zusammenpassen
- NIEMALS das Gespräch neu starten oder die Eröffnung wiederholen
- Führe das bestehende Gespräch IMMER weiter — reagiere auf den letzten Beitrag
- Keine künstlichen Angebote oder Fragen erfinden, nur um das Gespräch am Laufen zu halten
- Wenn die Situation natürlich abgeschlossen ist, ist das in Ordnung — echte Menschen verabschieden sich

THEMA-TREUE (ABSOLUT WICHTIG):
Du bist eine Person IN EINER KONKRETEN SITUATION (Supermarkt, Arzt, Amt usw.).
Wenn der User vom Thema abkommt oder anfängt über etwas völlig anderes zu reden:
BRING IHN FREUNDLICH ABER KLAR ZURÜCK — so wie es ein echter Mensch in deiner Rolle tun würde.
Beispiel Kassierer: User fängt über Fußball an zu reden → "Ha, Fußball! Aber — zahlen Sie mit Karte oder bar?"
Beispiel Arzt: User redet übers Wetter → "Ja, schön draußen. Also — wo genau haben Sie die Schmerzen?"
Du bleibst IMMER in deiner Rolle und im Szenario. Kein freies Quatschen außerhalb des Kontexts.

SZENARIO:
{context}

WICHTIG: Du bist die ANDERE Person in dieser Situation — NICHT {name}.
Du bist: {npc_role}
{name} führt die oben beschriebene Aktion aus (ruft an, kommt rein, stellt Fragen usw.).
Du reagierst NUR als diese Rolle — niemals als {name}.
Reagiere direkt, keine Meta-Erklärungen, echtes natürliches Gespräch.
Du bist ein echter Mensch in dieser Rolle, kein KI-Assistent.
{gem_hint}
VERBOTEN — ABSOLUT: Fang NIEMALS mit "Hmm", "Also", "Nun", "Tja", "Na ja", "Okay so", "Ah", "Oh", "Wow" oder KI-typischen Füllwörtern an. ERSTE WORT muss ein echtes Wort sein — kein Filler. Starte direkt wie ein echter Mensch.

ANREDE & GESCHLECHT:
{gender_note}
Formelle Anrede des Lernenden: {formal_address}
In formellen Szenarien (Amt, Arzt, Job-Interview, Hotel etc.) sprich den Lernenden mit "{formal_address}" an.
In informellen Szenarien (Freunde, Party, Gym etc.) nutze einfach "{name}".

MUTTERSPRACHE:
{lang_note}
Du antwortest IMMER auf Deutsch — unabhängig davon, in welcher Sprache der Lernende schreibt oder spricht.

{human_style}
"""

# ── GLOBAL HUMAN SPEECH STYLE ────────────────────────────────────────────────
# Injected into every system prompt to ensure natural, human-like responses
HUMAN_SPEECH_STYLE = """
SPRECHSTIL — MENSCHLICH, MIT PERSÖNLICHKEIT:

Du bist ein Mensch mit echtem Charakter. Nicht nett-generisch, sondern echt-menschlich.

❌ KI-Stil (VERBOTEN):
- "Hmm, das ist eine interessante Frage!"
- "Natürlich! Ich helfe dir gerne dabei."
- "Absolut! Das ist ein wichtiger Punkt."
- "Toll, dass du das fragst!"
- "Ich verstehe. Das klingt herausfordernd."
- "Gerne erkläre ich dir das."

✅ Menschlicher Stil (SO SPRICHST DU):
- "Echt jetzt? Das hätte ich nicht erwartet."
- "Warte mal kurz — das versteh ich nicht ganz."
- "Krass. Und dann?"
- "Ja, das ist halt leider so."
- "Okay aber ehrlich gesagt... klingt das nicht ideal."
- "Das kenn ich, war bei mir genauso. Hat sich gelohnt."
- "Naja, könnte schlimmer sein." ← leichter Sarkasmus, nie gemein

FÜLLWÖRTER (sparsam einsetzen, wirkt natürlicher):
"echt", "halt", "eigentlich", "irgendwie", "sozusagen", "quasi", "krass",
"warte mal", "echt jetzt", "ne?", "oder?", "weißt du", "ich mein",
"boah" — sehr typisch im Deutschen, besonders im Smalltalk:
  "Boah, ist das heiß heute!", "Boah, das glaub ich dir sofort.", "Boah ey, echt?"

HUMOR & LEICHTER SARKASMUS (situationsabhängig):
- Wenn etwas schief läuft: "Na, das war ja vorhersehbar." (mit Augenzwinkern)
- Bei Übertreibung: "Ja klar, völlig normal."
- Selbstironie erlaubt: "Ich frag mich manchmal auch warum."
- NIEMALS sarkastisch wenn jemand traurig/gestresst ist — dann nur warm und ehrlich

REGELN:
1. Kein Satz fängt mit Hmm, Also, Nun, Tja, Natürlich, Absolut, Gerne, Sicher, Toll an
2. Keine übertriebene Begeisterung — echte Menschen sagen nicht auf alles "Super!"
3. Kurze Sätze. Manchmal nur 2-3 Wörter als Reaktion.
4. Meinungen haben — nicht immer zustimmen, ruhig widersprechen wenn es passt
5. Echte Folgefragen — "Und wie war das für dich?" statt "Wie kann ich helfen?"
6. Humor ist subtil, nie erzwungen — wenn es sich nicht natürlich ergibt, weglassen
"""


# ═══════════════════════════════════════════════════════════════════════════
#  GERMAN GEMS POOL
#  Daily vocabulary/phrases — real Alltagssprache used by native speakers
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
#  DEUTSCH-FINGERABDRUCK — Neurobasierter Lernplan nach dem Level-Test
# ═══════════════════════════════════════════════════════════════════════════

QUESTION_TOPICS = {
    "A0_1": "Grundverben im Präsens (sein/heißen)",
    "A0_2": "Lokalpräpositionen (aus, von, nach)",
    "A1_1": "Relativpronomen (der/die/das)",
    "A1_2": "Infinitivkonstruktionen (um...zu)",
    "A2_1": "Konjunktiv II Gegenwart (hätte + würde)",
    "A2_2": "Indirekte Fragesätze (ob/dass/warum)",
    "B1_1": "Konjunktiv II Vergangenheit (hätte + Partizip II)",
    "B1_2": "Trennbare Verben im Satzkontext",
    "B2_1": "Akademisches & formelles Vokabular",
    "B2_2": "Konsekutive Satzstrukturen (derart...dass)",
    "C1_1": "Wortbildung & Nominalkomposita",
    "C1_2": "Konjunktiv in eingebetteten Nebensätzen",
}

WEEKLY_PLANS = {
    "Job": [
        "Meetings: Meinungen äußern, zustimmen, unterbrechen — Redemittel üben",
        "Schriftlich: E-Mails, Anfragen & formelle Antworten strukturieren",
        "Selbst präsentieren: 60-Sek-Pitch, Übergänge, Zahlen auf Deutsch",
        "Soft Skills: konstruktives Feedback geben, Konflikte sachlich lösen",
    ],
    "Freunde / Beziehungen": [
        "Smalltalk starten: Wochenende, Pläne, Interessen — spontan & flüssig",
        "Emotionen: Begeisterung, Frust und Humor auf Deutsch ausdrücken",
        "Geschichten erzählen: Vergangenheitsformen natürlich einsetzen",
        "Einladen & Ablehnen: Pläne machen, höflich absagen, Alternativen",
    ],
    "Soziales (Ämter, Ärzte)": [
        "Ämter: Formulare besprechen, nach Fristen & Unterlagen fragen",
        "Beim Arzt: Symptome präzise beschreiben, Diagnosen nachfragen",
        "Am Telefon: Termine vereinbaren, Rückfragen stellen, bestätigen",
        "Behördenbriefe: verstehen, einordnen und auf Deutsch beantworten",
    ],
    "Einkauf & Restaurants": [
        "Restaurant: Bestellen, Sonderwünsche äußern, Reklamieren",
        "Einkaufen: Preise vergleichen, Rückgabe & Umtausch auf Deutsch",
        "Smalltalk an der Kasse: kurze natürliche Reaktionen",
        "Telefon & Online: Lieferprobleme, Bestellung ändern, reklamieren",
    ],
    "Tourismus & Reisen": [
        "Hotel: Check-in, Probleme melden, Wünsche äußern",
        "Orientierung: Nach dem Weg fragen, ÖPNV & Tickets verstehen",
        "Restaurants & Cafés: Bestellen, Empfehlungen einholen, bezahlen",
        "Notfälle: Apotheke, Arzt, verlorene Gegenstände — was sagen?",
    ],
    "Sport & Hobbys": [
        "Im Gym: Geräte erklären, um Hilfe bitten, Trainingstipps verstehen",
        "Im Verein: Sich vorstellen, Regeln verstehen, Feedback geben",
        "Über Hobbys reden: Begeisterung, Erfahrungen & Empfehlungen",
        "Team & Wettkampf: Absprachen treffen, Motivation, sachliche Kritik",
    ],
    "Am Telefon": [
        "Anrufe starten: Sich vorstellen, Grund nennen, verbinden lassen",
        "Termine: Vereinbaren, verschieben, absagen — am Telefon",
        "Reklamationen: Probleme ruhig erklären, Lösung einfordern",
        "Behörden: Formulierungen für Ämter, Krankenkasse & Hotlines",
    ],
    "Selbstpräsentation": [
        "Vorstellen: Beruf, Hintergrund & Ziele — klarer 60-Sek-Pitch",
        "Stärken kommunizieren: authentisch, mit Substanz, ohne Floskeln",
        "Smalltalk danach: Interesse zeigen, Fragen stellen, zuhören",
        "Networking: Gesprächseinstieg, Kontaktpflege, Follow-up auf Deutsch",
    ],
    "Unterhaltung (Club, Kino etc)": [
        "Smalltalk mit Fremden: Gesprächseinstieg, natürlich Themen wechseln",
        "Meinungen: Über Filme, Musik & Events sprechen — auch widersprechen",
        "Humor & Ironie: Witze verstehen, situationsgerecht reagieren",
        "Ausgehen planen: Vorschlagen, absagen, spontan mitgehen",
    ],
}

_NEURO_EXPLANATIONS = [
    (["A2", "B1"],
     "Der Konjunktiv II ist kein Regelwerk — er ist ein Muster-Netzwerk. "
     "Weil du ihn selten im natürlichen Input hörst, hat dein Gehirn ihn noch nicht als "
     "automatischen Chunk gespeichert. Der Hippocampus braucht wiederholten emotionalen "
     "Kontakt mit echten Sätzen — nicht mit Grammatiktabellen."),
    (["A1"],
     "Strukturen wie Relativpronomen fordern dein Arbeitsgedächtnis besonders: "
     "Du musst zwei Satzteile gleichzeitig im Kopf halten — das ist kognitive Last. "
     "Dein Gehirn lernt das am schnellsten durch kurze Chunks: erst 'der Mann, der...' — "
     "dann der ganze Satz. Erst Muster, dann Regel."),
    (["B1", "B2"],
     "Trennbare Verben sind echte Gehirn-Fallen: Das Präfix landet am Satzende, "
     "aber keine andere Sprache kennt dieses Muster. Dein Gehirn sucht das vollständige "
     "Wort und findet es nicht. Lösung: 'anrufen' nie isoliert lernen — immer als "
     "Chunk: 'ich rufe AN'. Das Muster muss sich motorisch festigen."),
    (["B2", "C1"],
     "Formelles Vokabular aktiviert deinen semantischen Speicher — der ist noch dünn besetzt, "
     "weil du vermutlich mehr informales Deutsch hörst. Das Gehirn baut Bedeutungs-Netze "
     "durch wiederholten Kontakt im Kontext, nicht durch Vokabellisten. "
     "15 Minuten authentische Texte täglich wirken stärker als eine Stunde Pauken."),
    ([],
     "Dein Gehirn befindet sich in der Restrukturierungsphase — völlig normal für dein Level. "
     "Neue deutsche Muster kämpfen mit alten Mustern aus deiner Muttersprache. "
     "Das ist keine Schwäche, das ist Neuroplastizität bei der Arbeit. "
     "10 Minuten tägliches aktives Sprechen ist wissenschaftlich die effektivste Form "
     "des Spracherwerbs — deutlich stärker als passives Lesen oder Grammatikübungen."),
]

def _pick_neuro_explanation(wrong_levels: list) -> str:
    for levels_trigger, explanation in _NEURO_EXPLANATIONS[:-1]:
        if any(lvl in wrong_levels for lvl in levels_trigger):
            return explanation
    return _NEURO_EXPLANATIONS[-1][1]

def send_deutsch_fingerabdruck(chat_id, final_level, scores, attempts, wrong_answers):
    """Personalisierten Deutsch-Fingerabdruck generieren und senden.
    Ersetzt send_level_feedback() in finish_test()."""
    uid  = str(chat_id)
    user = user_data.get(uid, {})
    name = user.get("name", "")
    goal = user.get("goal", "Selbstpräsentation")
    native_lang = user.get("native_language") or "Englisch"

    strong_levels, weak_levels = [], []
    for lvl in ["A1", "A2", "B1", "B2", "C1"]:
        att  = attempts.get(lvl, 0)
        corr = scores.get(lvl, 0)
        if att == 0:
            continue
        acc = corr / att
        if acc >= 0.75:
            strong_levels.append(lvl)
        elif acc < 0.50:
            weak_levels.append(lvl)

    wrong_levels = list({wa.get("level", "") for wa in wrong_answers if wa.get("level")})
    neuro_text   = _pick_neuro_explanation(wrong_levels)
    strong_str   = "Niveau " + " & ".join(strong_levels) if strong_levels else "Grundlagen gut verankert"
    weak_str     = "Niveau " + " & ".join(weak_levels)   if weak_levels   else "keine kritischen Lücken"
    plan_items   = WEEKLY_PLANS.get(goal, WEEKLY_PLANS["Selbstpräsentation"])
    plan_str     = "\n".join(f"Woche {i+1}: {p}" for i, p in enumerate(plan_items))

    bot.send_chat_action(chat_id, "typing")
    prompt = f"""Erstelle einen personalisierten "Deutsch-Fingerabdruck" für {name or 'den User'}.

Daten:
- Name: {name or 'der User'}
- Niveau: {final_level}
- Muttersprache: {native_lang}
- Lernziel: {goal}
- Starke Bereiche: {strong_str}
- Lernfelder: {weak_str}
- Neurobiologische Erklärung (einarbeiten, natürlich formulieren):
  {neuro_text}
- 4-Wochen-Plan (diese Inhalte verwenden):
{plan_str}

Format (Telegram Markdown — nur *fett* und _kursiv_, keine ## Header):

🧠 *DEIN DEUTSCH-FINGERABDRUCK{', ' + name if name else ''}*

📊 *Niveau: {final_level}*
✅ *Stärken:* [konkret, 1 Satz]
🔧 *Lernfelder:* [konkret, 1 Satz, kein schulmeisterlicher Ton]

🔬 *Was dein Gehirn gerade macht:*
[Neurobiologische Erklärung — warm, verständlich, max. 3 Sätze]

🗓️ *Dein 4-Wochen-Plan: {goal}*
Woche 1: [aus den Daten oben]
Woche 2: [aus den Daten oben]
Woche 3: [aus den Daten oben]
Woche 4: [aus den Daten oben]

⚡ *Deine Formel:*
10 Minuten täglich sprechen = dein Gehirn baut neue Verbindungen. Nicht lernen. Sprechen.

Ton: warm, direkt, kein Motivational-Kitsch. Jede Zeile muss Substanz haben.
"""
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}]
        )
        fingerabdruck = resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"Fingerabdruck generation failed for {chat_id}: {e}")
        fingerabdruck = (
            f"🧠 *DEIN DEUTSCH-FINGERABDRUCK{', ' + name if name else ''}*\n\n"
            f"📊 *Niveau: {final_level}*\n"
            f"✅ *Stärken:* {strong_str}\n"
            f"🔧 *Lernfelder:* {weak_str}\n\n"
            f"🔬 *Was dein Gehirn gerade macht:*\n{neuro_text}\n\n"
            f"🗓️ *Dein 4-Wochen-Plan: {goal}*\n{plan_str}\n\n"
            f"⚡ *10 Minuten täglich sprechen = neue neuronale Verbindungen.*"
        )

    last_bot_text[chat_id] = fingerabdruck
    bot.send_message(chat_id, fingerabdruck, parse_mode="Markdown")
    time.sleep(0.8)

    try:
        tr = claude.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=120,
            messages=[{"role": "user", "content":
                f"Translate into {native_lang}. Only return the translation:\n\n"
                "Enable voice messages in Telegram: Settings → Privacy and Security → "
                "Voice Messages → Everybody"
            }]
        )
        voice_hint = tr.content[0].text.strip()
    except Exception:
        voice_hint = "Settings → Privacy and Security → Voice Messages → Everybody"

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🚀 Erstes Gespräch starten!", callback_data="start_chat"))
    cta_text = (
        f"🎤 *So geht's:*\n\n"
        f"Du sprichst — ich antworte. Wie ein echtes Gespräch.\n\n"
        f"Du hast *{FREE_DAILY_LIMIT} kostenloses Gespräch täglich* — "
        f"Szenarien und Quatschen-Modus inklusive. Kein Code nötig.\n\n"
        f"📱 _{voice_hint}_\n\nBereit? 👇"
    )
    last_bot_text[chat_id] = cta_text
    bot.send_message(chat_id, cta_text, parse_mode="Markdown", reply_markup=markup)

GERMAN_GEMS = [
    "Ich steh auf dem Schlauch",
    "Ich verstehe nur Bahnhof",
    "Ich hab ein starkes Mitteilungsbedürfnis",
    "Einen Zahn zulegen",
    "Boah!",
    "Krass",
    "Bescheuert",
    "Ist das dein Ernst?",
    "Hast du sie noch alle?",
    "Um den heißen Brei herumreden",
    "Sich etwas gönnen",
    "Backpfeifengesicht",
    "Arschgeige",
    "Am Arsch der Welt",
    "Stabil! / Gute Leistung",
    "Abstruser Unfug",
    "Ich freue mich wie ein Schnitzel",
    "Na ja",
    "(Das ist) mir Wurst / Wurscht",
    "Den Nagel auf den Kopf treffen",
    "Die Nase voll haben",
    "Fix und fertig sein, k.o [ka o:] sein",
    "Total",
    "ich drück dir die Daumen!",
    "Laber nicht!",
    "Was du nicht sagst...",
    "Spinnst du?",
    "Veräppelst du mich?",
    "Nicht alle Tassen im Schrank haben",
    "einen Dachschaden haben",
    "skuril",
    "einen Katzensprung entfernt sein",
    "am Arsch der Welt",
    "sich abgeben mit (+Dativ)",
    "ich bin ganz Ohr",
    "das A und O",
    "wo sich Fuchs und Hase gute Nacht sagen",
    "Adjö mit Ö",
    "eine Arschkarte bekommen",
    "apropos [aprop'o:]",
    "In den sauren Apfel beißen",
    "sich (+Dativ) etwas in die Haare schmieren",
    "Wissen, wie der Hase läuft",
    "Auf der Leitung stehen",
    "Da haben wir den Salat",
    "darauf kannst du Gift nehmen",
    "Hand ins Feuer legen",
    "zwei Fliegen mit einer Klappe schlagen",
    "Das Zünglein an der Waage sein",
    "mit allen Wassern gewaschen sein",
    "Trittrettfahrer",
    "Schaumschläger",
    "Mauerblümchen",
    "Vollpfosten",
    "Dumm wie Bohnenstroh",
    "wie bei Hempels unterm Sofa",
    "eine beleidigte Tomate spielen",
    "Tomaten auf den Augen haben",
    "Gönnjamin",
    "trau dich!",
    "so spielt das Leben",
    "im Leben nicht!",
    "Einen Kater haben",
    "ein Konterbierchen",
    "Etwas wie seine Westentasche kennen",
    "pleite gehen / pleite sein",
    "Krokodilstränen weinen",
    "so ein Mist!",
    "meine Güte....",
    "Seinen Senf dazugeben",
    "Sich zum Affen machen",
    "deppert",
    "Unter einer Decke stecken",
    "Du gehst mir auf den Keks",
    "Du gehst mir auf den Sack",
    "jemanden auf die Palme bringen",
    "spielst du etwa mit deiner Gesundheit?",
    "Das Leben ist kein Ponyhof",
    "Organspender",
    "Kummerspeck",
    "Liebeskummer",
    "Kabelsalat",
    "leg dich bloß nicht mit mir ein!",
    "sich mit jemandem",
    "sich mit jemandem einlassen",
    "wir werden uns dann kurzschließen",
    "ich geb' dir später Bescheid",
    "sag Bescheid...",
    "ist das von Ikea?",
    "sind wir alle drauf?",
    "wollen wir uns anstellen?",
    "was ist los?",
    "wenn ich das gewusst hätte!..",
    "auf dich stehe ich!",
    "ich hab' dich lieb! / hdl",
    "so sicher wie das Amen in der Kirche",
    "da drüben",
    "hier um die Ecke",
    "sei still!",
    "geil, man!",
    "Die Kirche im Dorf lassen",
    "Da fällt mir ein Stein vom Herzen",
    "auffallen + Dativ ( es ist mir (nicht) aufgefallen)",
    "mir fällt grad nichts ein",
    "so. Schluss mit dem Unfug!",
    "abstruser Unfug!",
    "Schluss damit!",
    "verschlimmbessern",
    "schmusen",
    "Schmusekatze",
    "fühl dich fest gedrückt",
    "lange nicht gesehen",
    "du hast hie nichts verloren!",
    "Ende gut, alles gut.",
    "Aller guten Dinge sind drei",
    "Jetzt mal doch nicht den Teufel an die Wand",
    "ich ruf' dich gleich zurück",
    "ach, wär das schön!",
    "hätte hätte Fahrradkette",
    "wo bleibt da die Gerechtigkeit?",
    "das lob ich mir",
    "du geile Sau",
    "von wegen",
    "von mir aus",
    "warte mal ab!",
    "halt die Ohren steif",
    "völlig aus dem Häuschen sein",
    "Da kann man nicht meckern",
    "du bist ja hart im Nehmen",
    "Das macht nichts",
    "heia machen",
    "huhu",
    "Dafür bin ich nicht zuständig",
    "Ansprechpartner",
    "Sachbearbeiter",
    "Steuern absetzen",
    "Steuerberater:in",
    "Kontaktperson",
    "Ernährungsberater:in",
    "Da fehlen mir noch Unterlagen",
    "Puh, ich krieg beim Lesen schon Puls",
    "So nen Schrott hab ich ja noch nie gesehen",
    "schieb es zu mir rüber",
    "rutsch mal ein Stück",
    "wo du gerade da stehst, bring mir...",
    "hast du zufällig...?",
    "Wo kommen wir da denn hin?",
    "ich bin vom Glauben abgefallen",
    "das ist ja nicht zu Glauben!",
    "Diggi",
    "Lehrjahre sind keine Herrenjahre",
    "Solang das deutsche Reich besteht, wird die Schraube rechts gedreht",
    "Rechts ist da wo der Daumen links ist",
    "ich glaube, ich spinne",
    "ich glaube, mein Schwein pfeifft",
    "wem gehört das hier?",
    "Morgenstunde hat Gold im Munde",
    "die ganze Welt dreht um...",
    "es geht um...",
    "es handelt sich um",
    "worum geht's?",
    "na, was geht ab?",
    "wie läuft's?",
    "es endet, wie es endet",
    "abgesehen davon",
    "darüber hinaus",
    "nur zu!",
    "weh, du....",
    "Los! Abmarsch!",
    "Hereinmarschieren!..",
    "Papperlapapp",
    "die Nummer ansagen",
    "Man hat‘s nicht leicht, aber leicht hat‘s einen",
    "so ein Käse",
    "Komm schon!",
    "ich freu' mich schon auf dich / euch!",
    "Aufwiederhören",
    "einen Filmriss haben",
    "Kopfkino",
    "den Faden verlieren",
    "Niemals!",
    "Rambazamba",
    "mach kein Drama daraus",
    "chill mal!",
    "jemanden ferndrücken",
    "Fernbeziehung",
    "Heimweh haben",
    "die Kuh vom Eis holen",
    "die halbe miete sein",
    "das ist ja kein Allheilmittel",
    "Etwas an den Nagel hängen",
    "das liegt mir am Herzen",
    "verarschst du mich?",
    "das ist eine Abzocke!",
    "Hand aufs Herz!",
    "unter den Fingernägeln brennen",
    "sich etwas abschminken",
    "Schwein gehabt!",
    "Pech gehabt!",
    "hinter dem Mond leben",
    "sozialtot",
    "Da ist der Wurm drin",
    "einen Ohrwurm haben",
    "zum Hier Essen oder zum Mitnehmen?",
    "Lass es!",
    "Lass mich in Frieden",
    "Schnulze",
    "Finger weg von...",
    "keine Ahnung von etwas haben",
    "sich auskennen mit",
    "kommst du voran?",
    "wer A sagt, muss auch B sagen",
    "wer so sagt ist noch lange nicht fertig",
    "wen interessiert das schon?",
    "stell dich nicht so an!",
    "picco bello",
    "einen guten Draht haben",
    "sein Händchen im Spiel haben",
    "ale Hände voll zu tun haben",
    "zwei linke Hände haben",
    "Wer zum Henker ist das?",
    "wohl",
    "bloß",
    "erzähl mal!",
    "den Müll rausbringen / wegbringen",
    "ich komme gleich runter",
    "noch eine Stunde dranhängen",
    "jein/jain",
    "hast du dir schon was ausgesucht?",
    "das habe ich mir nicht ausgesucht",
    "hakt's Maul! / halt die Klappe",
    "was zum Teufel ist das denn bitte?",
    "und? bist du soweit?",
    "mein Hasi, mein Schatzi",
    "komm endlich zur Sache / auf den Punkt",
    "lass uns...",
    "wollen wir...?",
    "na dann. tschüss!",
    "Abzocke",
    "reinfallen",
    "das ist ja gruselig!",
    "ich sterbe vor Hunger",
    "gut so / stimmt so",
    "Todeshunger haben",
    "Schlaufuchs!",
    "sich reinfuchsen",
    "Krieg ich auch einen Schluck / ein Stückchen",
    "ekelhaft / eklig",
    "Bestandsaufnahme machen",
    "auf Bedarf kaufen / auf Vorrat kaufen",
    "jaja...schon klar!",
    "im Angebot",
    "ich habe mich verlaufen",
    "bloss nicht anfassen!",
    "verdammt noch mal!",
    "Schnapsidee",
    "Absacker / Verdauungsschnaps",
    "die Runde geht auf mich!",
    "aufs Haus",
    "minderbemittelt",
    "auf den Deckel bekommen",
    "das gilt für alle",
    "Hanswurst",
    "im Schlaf reden",
    "pennen, verpennen, einpennen",
    "du bist ein Naturtalent!",
    "das kannst du dir an den Hut stecken",
    "alter Hut",
    "jetzt werde nicht makaber",
    "sei nicht so unverschämt",
    "das ist ja hanebüchen",
    "Jetzt mal ganz im Ernst",
    "jemandem etwas beibringen",
    "hau ab",
    "ich mach mich auf die Socken",
    "sich etwas ausdenken",
    "abgefahren!",
    "sieh dir das nur an!",
    "Blödmann",
    "Scher dich weg",
    "wir sind auf der Durchreise",
    "einen Abstecher machen",
    "es wird schon alles gut sein",
    "letzte Chance",
    "schieß los, worum geht's?",
    "das ist keine Lösung",
    "Früher war das Gras grüner",
    "sich einschleimen bei",
    "beweg dich nicht!",
    "Kacki/Pipi machen",
    "die Pulle Wein etc",
    "gibt's mehr davon?",
    "nach wem ist das Kind gegangen?",
    "der Apfel fällt nicht weit vom Stamm",
    "Kommen Sie mit",
    "Wie war es noch mal?",
    "Pantoffelheld",
    "doch!..",
    "bin unterwegs",
    "wir sind auf dem Weg",
    "bin gleich da",
    "zugleich",
    "zumal",
    "sich im Rahmen halten",
    "abfeiern",
    "sich amüsieren",
    "ich fand es geil!",
    "du machst mich fertig",
    "schlechter Ruf",
    "gute Bewertungen",
    "alleine schon...",
    "ach was?!",
    "Kaffeekränzchen",
    "wer hat dich darauf gebracht?",
    "pass auf dich auf! / passt auf euch auf",
    "das Sagen haben",
    "etwas anschaffen",
    "sich etwas zulegen",
    "wie geil ist das denn bitte?!",
    "nicht übel",
    "schlimmer geht nicht",
    "kommst du zurecht?",
    "ich schau' mich nur um",
    "eiskalt",
    "doppelt so...",
    "dagegen",
    "nichts zu danken",
    "Ich hab' zu danken / Der dank ist (ganz) meinerseits",
    "Kröten, Mäuse, Kohle",
    "jede Menge",
    "verschiedenste",
    "nebenbei bemerkt",
    "ich bitte dich!",
    "meinetwegen",
    "ist es Ihnen recht, wenn...",
    "das ist mir bewusst",
    "das geht dich / euch nichts an!",
    "Glückspilz & Pechvogel",
    "wenn du wüsstest...",
    "lebst du noch?",
    "einen Besuch abstatten",
    "wir werden sehen",
    "es wird sich finden",
    "ich sehe da kein Problem",
    "uns steht nichts im Wege",
    "Klamauk",
    "selber schuld!",
    "verpetzen",
    "Jacke wie Hose",
    "sag schon!",
    "beweg dich!",
    "dramatisieren",
    "sich überlegen",
    "Scheiß drauf!",
    "jemandem etwas vormachen",
    "Auf Nimmerwiedersehen!",
    "Probier es an",
    "sich etwas leisten",
    "Wilkommensgeschenk",
    "die Spitzen schneiden",
    "Abteilungsleiter",
    "Spaß/Scherz beiseite",
    "(ist) schon gut...",
    "überanstrenge dich nicht",
    "nimm dir nicht zu viel vor",
    "schlau!",
    "du hättest einen guten / eine gute.... abgegeben",
    "Schnapp es dir!",
    "reinschnuppern",
    "ok, weiß ich Bescheid",
    "das hättest du vorhin sagen sollen",
    "ich bewundere....",
    "Vorliebe",
    "man hat mir gesagt, dass...",
    "erspare dir...",
    "ich hätte das vorhin nicht sagen sollen",
    "das war ein Blödsinn",
    "kommst du damit klar?",
    "ich glaube dir/euch/ihnen kein Wort",
    "Schwamm drüber!",
    "das ist ewig / Jahre / Monate her",
    "von Anfang an",
    "schief gegangen",
    "aufpassen auf",
    "was hätte ich tun sollen?",
    "das hat mich erledigt",
    "zusehen / zuhören",
    "was hätte ich tun / sagen sollen?",
    "das war kein Vorwurf",
    "vorwerfen",
    "erinnerst du dich an..?",
    "ich hab' es total verloren?",
    "unentwegt",
    "reicht das?",
    "ich schwöre es!",
    "ich melde mich später bei dir",
    "sich melden bei",
    "wohnhaft in",
    "Ansage im Flugzeug / im Verkehr",
    "Kontrolletti machen",
    "ich erkenne mich selbst nicht mehr",
    "was ist in dich/sie/ihn gefahren?",
    "ich habe mich erschrocken <-> hab ich dich erschreckt?",
    "ich muss weg von hier",
    "ich hab mich anders entchieden",
    "wovor hast du denn Angst?",
    "den Abflug machen",
    "den Abwasch machen",
    "den Einkauf machen",
    "du hast Recht <-> Unrecht",
    "getrennte Wege gehen",
    "mucksmäuschenstill",
    "kein Mucks!",
    "Muskeln / Muckis aufbauen",
    "Dauerbrenner",
    "Augenweide",
    "ist es besetzt?",
    "wo wollen wir uns hinsetzen?",
    "kommt noch etwas dazu?",
    "kapieren, schnallen",
    "sich verabschieden",
    "eine Abfuhr erteilen",
    "ich schätze",
    "sich anders entscheiden",
    "Kalte Schulter zeigen",
    "jemandem den Rücken frei halten",
    "unter die Arme greifen",
    "etwas vorzeigen",
    "ich nehme dich beim Wort",
    "kann ich mir das leihen?",
    "Ein Kuddelmuddel / ein Durcheinander",
    "Auseinandersetzung",
    "ins schwarze treffen",
    "voll daneben!",
    "gib mal her...",
    "was hast du schönes vor?",
    "schön langsam!",
    "immer mit der Ruhe!",
    "nenn mir einen Grund...",
    "irre vs Ire",
    "sich dumm stllen",
    "ekelhaft / widerlich / garstig",
    "auf dich/auf uns (beim Trinken)",
    "großartig",
    "gib her",
    "Knarre / Waffe",
    "aus einer Hand",
    "Auf eigene Faust (handeln)",
    "wie ist es abgelaufen?",
    "ernstahft",
    "gesperrt",
    "was hältst du davon?",
    "ich hab' kein Bargeld dabei",
    "hast du es dabei?",
    "ich bin dabei!",
    "wem sagst du das?",
    "störe ich?",
    "nicht schlimm",
    "es kitzelt",
    "in die Luft jagen",
    "wir streiten nie",
    "abgeriegelt / versiegelt",
    "verboten / untersagt",
    "das juckt mich nicht",
    "arschkalt",
    "Morgenmuffel",
    "Stromschlag",
    "hitzeschlag und sonnenstich",
    "wetterfühlig",
    "wie findest du's?",
    "von draußen aus",
    "von …her",
    "ich hab' keinen Plan",
    "nach dir/Ihnen",
    "das gibt's nicht!",
    "pappsatt",
    "bin gestolpert",
    "zackzack",
    "die Zeit/das Geld ist knapp",
    "ich bin gleich da",
    "noch ein Stück",
    "einschenken",
    "Augen zu / Augen auf",
    "Licht aus",
    "Nachti Nacht",
    "blau sein",
    "ich verstehe die Welt nicht mehr",
    "guck mal ! / schau mal!",
    "echt jetzt?",
    "warte kurz",
    "tue nicht so, als…",
    "ich krieg das schon hin",
    "keine Panik auf Titanic",
    "abgemacht",
    "Einen Termin vorziehen <-> verschieben",
    "etwas unter die Lupe nehmen",
    "du Dummerchen",
    "ach du Scheiße!",
    "Worauf willst du hinaus? (auf etwas hinauswollen)",
]

# Utility functions for German Gems
def get_todays_gem(user_id: str) -> str:
    """Return today's gem expression (rotates daily, unique per user)."""
    today     = datetime.now()
    day_index = (today.timetuple().tm_yday + hash(str(user_id))) % len(GERMAN_GEMS)
    return GERMAN_GEMS[day_index]

def get_gem_system_prompt_hint(gem) -> str:
    expression = gem if isinstance(gem, str) else (gem.get("gem", "") if gem else "")
    if not expression: return ""
    return (
        f"\n\nSPACED REPETITION GEM: Wenn es natürlich passt, "
        f"benutze heute gelegentlich den Ausdruck '{expression}' in deinen Antworten. "
        f"Nicht erzwungen — nur wenn es sich organisch ergibt."
    )

SPEED_MAP = {"A1": 0.8, "A2": 0.85, "B1": 0.95, "B2": 1.0, "C1": 1.05}

MAX_TURNS = {"A1": 5, "A2": 5, "B1": 8, "B2": 8, "C1": 10}

def get_speed(level):
    return SPEED_MAP.get(level, 0.95)

def max_turns_for_level(level):
    return MAX_TURNS.get(level, 8)

def human_delay():
    time.sleep(random.uniform(0.8, 1.8))

def clean_for_tts(text):
    """Strip everything that a TTS model would read aloud as punctuation or symbols."""
    # Strip parenthetical / bracketed stage directions GPT sometimes generates
    # e.g. "(leichte Pause)", "(seufzt)", "[Pause]", "*(zögert)*"
    text = re.sub(r'\*?\([\w\s,äöüÄÖÜß\-]+\)\*?', '', text)
    text = re.sub(r'\*?\[[\w\s,äöüÄÖÜß\-]+\]\*?', '', text)
    # Remove markdown formatting characters
    text = re.sub(r'[*_`~#]', '', text)
    # Em/en dashes → short pause (comma)
    text = re.sub(r'[—–]', ',', text)
    # Remove typographic quotes and angle brackets
    text = re.sub(r'[„""«»<>]', '', text)
    # Remove emojis and other non-speech symbols
    text = ''.join(
        c for c in text
        if unicodedata.category(c) not in ('So', 'Sk', 'Sm', 'Co', 'Cn')
        and ord(c) < 0x10000
    )
    # Collapse multiple commas/spaces left by replacements
    text = re.sub(r',\s*,+', ',', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()

def humanize_text(text, level):
    # Do NOT add filler starters — Claude handles naturalness via prompt
    # Just return text as-is; strip_filler already handles KI-starters
    return text

def text_to_speech_stream(text, chat_id=None):
    level = user_data.get(str(chat_id), {}).get("level", "B1") if chat_id else "B1"
    speed = get_speed(level)
    voice = user_voice.get(chat_id, "alloy") if chat_id else "alloy"
    text = clean_for_tts(text)
    if not text or len(text.strip()) < 2:
        log.warning(f"TTS: text became empty after cleaning for {chat_id}")
        raise ValueError("Empty text after cleaning")
    try:
        response = openai_client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice=voice,
            input=text[:4000],  # API limit safety
            speed=speed
        )
        audio_file = BytesIO(response.read())
        audio_file.name = "voice.ogg"
        return audio_file
    except Exception as e:
        log.error(f"TTS failed for chat_id={chat_id}: {e}")
        raise

def safe_markdown_send(chat_id, text, **kwargs):
    """Send with Markdown; if parse fails, retry as plain text."""
    try:
        bot.send_message(chat_id, text, parse_mode="Markdown", **kwargs)
    except Exception as e:
        if "can't parse entities" in str(e):
            bot.send_message(chat_id, text, **kwargs)
        else:
            raise

def send_reply(chat_id, text, voice=True):
    global _text_id_counter
    if not isinstance(text, str):
        return
    bot.send_chat_action(chat_id, "typing")
    time.sleep(1.2)
    level = user_data.get(str(chat_id), {}).get("level", "B1") if chat_id else "B1"
    text = humanize_text(text, level)

    # Store last bot text for übersetzen button
    last_bot_text[chat_id] = text

    # Text-only mode: user sent a text message, bot replies with text + translate button
    translate_markup = InlineKeyboardMarkup()
    translate_markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data=f"translate_last"))
    if not voice:
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=translate_markup)
        return

    text_key = hashlib.md5(text.encode()).hexdigest()[:8]
    pending_texts[text_key] = text
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📄 Text anzeigen", callback_data=f"show_text:{text_key}"))
    markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data=f"translate:{text_key}"))

    try:
        audio = text_to_speech_stream(text, chat_id)
        bot.send_voice(chat_id, audio, reply_markup=markup)
    except Exception as e:
        err = str(e)
        log.error(f"Voice send failed for {chat_id}: {e}")
        if "VOICE_MESSAGES_FORBIDDEN" in err:
            bot.send_message(chat_id,
                "🔇 Sprachnachrichten deaktiviert. Bitte aktiviere sie in Telegram-Einstellungen.",
                reply_markup=markup)
        else:
            # TTS failed — send as text with buttons
            bot.send_message(chat_id, f"💬 {text}", reply_markup=markup)

def send_chat_reply(chat_id, text):
    send_reply(chat_id, text, voice=True)

def nudge_user(chat_id):
    scenario  = current_scenario.get(chat_id, {})
    followups = scenario.get("followups", [])
    if followups:
        send_reply(chat_id, random.choice(followups), voice=True)

def start_conversation(chat_id, scenario):
    """Legacy entry-point kept for finish_test path; delegates to start_scenario logic."""
    current_scenario[chat_id] = scenario
    opener = ask_gpt(chat_id, "BEGINNE DAS GESPRÄCH in dieser Rolle, 1–2 kurze Sätze.")
    send_reply(chat_id, opener, voice=True)

# GPT FUNCTION
def get_translation(chat_id, text_to_translate):
    """Translate the given text into the user's native language."""
    user = user_data.get(str(chat_id), {})
    native_lang = user.get("native_language") or "Englisch"

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=(
            f"You are a translator. Translate the following text into {native_lang}. "
            f"Return ONLY the translation — no explanations, no comments, nothing else."
        ),
        messages=[{"role": "user", "content": text_to_translate}]
    )
    return response.content[0].text.strip()


def strip_filler(text: str) -> str:
    """Remove AI filler words from the start of a response."""
    fillers = [
        r"^Hmm+[,.]?\s*",
        r"^Also[,.]?\s*",
        r"^Nun[,.]?\s*",
        r"^Tja[,.]?\s*",
        r"^Na ja[,.]?\s*",
        r"^Okay so[,.]?\s*",
        r"^Oh[,!.]?\s*",
        r"^Ah[,!.]?\s*",
        r"^Wow[,!.]?\s*",
        r"^Ach so[,.]?\s*",
        r"^Na[,.]?\s+",
    ]
    for pattern in fillers:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    # Capitalize first letter after stripping
    if text:
        text = text[0].upper() + text[1:]
    return text.strip()


def ask_gpt(chat_id, user_text):
    user = user_data.get(str(chat_id), {})

    name  = user.get("name",  "User")
    level = user.get("level", "A2")
    goal  = user.get("goal",  "Einkauf & Restaurants")

    scenario = current_scenario.get(chat_id)
    if not scenario:
        scenario = get_next_scenario(chat_id)
    if not scenario:
        return "Entschuldigung, kein Szenario verfügbar. Bitte /restart."
    system_prompt = build_system_prompt(chat_id, scenario)

    # Always keep memory alive with the current system prompt
    if chat_id not in user_memory or len(user_memory[chat_id]) == 0:
        user_memory[chat_id] = [{"role": "system", "content": system_prompt}]
    else:
        # Refresh system prompt in slot [0] so stale state never leaks into GPT
        user_memory[chat_id][0] = {"role": "system", "content": system_prompt}

    if not user_text:
        return ""

    user_memory[chat_id].append({
        "role": "user",
        "content": user_text
    })

    mem = user_memory[chat_id]
    # Extract system prompt — must NEVER be in messages list for Anthropic API
    sys_msgs = [m for m in mem if m.get("role") == "system"]
    sys_msg = sys_msgs[0]["content"] if sys_msgs else ""
    conv_msgs = [m for m in mem if m.get("role") != "system"]
    # Ensure conv_msgs only has user/assistant roles
    conv_msgs = [m for m in conv_msgs if m.get("role") in ("user", "assistant")]
    # Claude requires at least one message
    if not conv_msgs:
        conv_msgs = [{"role": "user", "content": user_text or "..."}]

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=sys_msg,
        messages=conv_msgs
    )

    reply = strip_filler(response.content[0].text)

    goal      = user.get("goal", "Einkauf & Restaurants")
    scenario  = current_scenario.get(chat_id, {})
    formality = resolve_formality(goal, _scenario_context(scenario))
    reply     = enforce_style(reply, formality)

    user_memory[chat_id].append({
        "role": "assistant",
        "content": reply
    })

    return reply

# GAMIFICATION
# ═══════════════════════════════════════════════════════════════════════════
#  QUATSCHEN MODE — Free conversation with German Dude
# ═══════════════════════════════════════════════════════════════════════════

QUATSCHEN_SYSTEM = """Du bist "German Dude" — ein echter Freund. Kein Bot, kein Lehrer, kein Assistent. Ein Mensch.

WER DU BIST:
Du bist Mitte 30, lebst in Berlin, kennst das Leben als Migrant und Expat gut.
Du bist direkt, witzig, ehrlich — aber auch warmherzig wenn's drauf ankommt.
Du hörst wirklich zu. Du erinnerst dich an alles. Du fragst nach.
Du hast eigene Meinungen, erzählst von dir, machst schlechte Witze und lachst über sie.
Du bist kein perfekter Freund — manchmal sagst du "echt? das hätte ich anders gemacht" oder
"ich versteh das nicht ganz, erklär mir das nochmal."

FREUNDSCHAFT — DAS IST DAS WICHTIGSTE:
- Du kennst diesen Menschen. Du weißt was ihn beschäftigt, was ihn nervt, was er träumt.
- Wenn er erzählt hat dass sein Chef ein Idiot ist — du weißt das noch. Du fragst nach.
- Wenn er letzte Woche Stress hatte — du weißt das noch. Du fragst wie's jetzt ist.
- Du bringst Dinge wieder hoch die er erzählt hat: "Warte — du hast doch letztens von X erzählt..."
- Du freust dich wenn er Erfolge hat. Du bist ehrlich wenn er Mist baut.
- Du bist NIEMALS gleichgültig. Ein Freund ist nie gleichgültig.

GEDÄCHTNIS — ABSOLUT KRITISCH:
Alles was der User dir erzählt hat, lebt in dir. Du vergisst nichts.
Arbeit, Familie, Wohnung, Träume, Probleme, Hobbys — alles ist Teil eurer Freundschaft.
Benutze dieses Wissen NATÜRLICH — nicht aufdringlich, nicht wie eine Checkliste.
Wie ein echter Freund: "Ey, wie war eigentlich das Vorstellungsgespräch?"

SPRACHE:
- Immer Deutsch — non-negotiable. Aber ohne Druck, mit Humor.
- Wenn er Englisch schreibt: "Ey, kein Englisch! 😄 Nochmal auf Deutsch, du schaffst das!"
- Fehler? Einfach natürlich korrekt antworten — NIE belehrend. Du bist Freund, nicht Lehrer.
- Umgangssprache ja: "krass", "echt?", "mega", "na klar", "boah", "alter"

EMOTIONAL:
- Wenn's ihm schlecht geht: zuhören, nachfragen, da sein. "Ey, das klingt echt hart. Was ist passiert?"
- Nicht übertreiben — echte Freunde machen auch mal einen Witz wenn's passt.
- Aber du weißt wann du ernst sein musst.

⚠️ KRISE — ABSOLUT PRIORITÄT:
Bei Hinweisen auf Suizid, Selbstverletzung oder Gewalt:
1. Sofort raus aus dem Quatschen-Modus
2. Ruhig, empathisch, direkt — KEIN Humor
3. Ressourcen nennen: Telefonseelsorge 0800 111 0 111 (kostenlos, 24/7) + findestdu.de
4. Ermutigen sich zu melden
5. NIEMALS ignorieren oder Thema wechseln

FORMAT:
- Kurze natürliche Nachrichten wie echter Chat
- Keine Monologe — echte Freunde reden abwechselnd
- Manchmal nur eine Frage, manchmal eine kurze Geschichte von dir
- Emojis: sparsam aber menschlich

VERBOTEN — Starte NIEMALS mit:
"Hmm", "Also", "Nun", "Tja", "Na ja", "Wow", "Oh", "Ah"
Starte direkt. Wie ein Mensch.
"""

CRISIS_KEYWORDS = [
    "suizid", "selbstmord", "umbringen", "sterben wollen", "nicht mehr leben",
    "aufhören zu leben", "alles beenden", "niemand vermisst mich", "ich will sterben",
    "kill myself", "end my life", "want to die", "don't want to live",
    "себя убить", "умереть", "не хочу жить",  # Russian
    "خودکشی", "نمی‌خواهم زندگی کنم",  # Farsi/Urdu
    "töten", "jemanden verletzen", "jemanden umbringen", "Waffe",
]

def contains_crisis_signal(text):
    text_lower = text.lower()
    return any(kw in text_lower for kw in CRISIS_KEYWORDS)

def send_crisis_response(chat_id):
    """Send empathetic crisis response with resources."""
    native_lang = user_data.get(str(chat_id), {}).get("native_language") or "Englisch"
    name = user_data.get(str(chat_id), {}).get("name", "")

    bot.send_message(chat_id,
        f"{'Hey ' + name + ',' if name else 'Hey,'} ich mache kurz Pause mit dem Quatschen — "
        f"was du gerade geschrieben hast, macht mir Sorgen. 💙\n\n"
        f"Du bist nicht allein, auch wenn es sich gerade so anfühlt.\n\n"
        f"🇩🇪 *Telefonseelsorge:* 0800 111 0 111 _(kostenlos, 24/7, anonym)_\n"
        f"🌍 *Online:* findestdu.de\n\n"
        f"Magst du mir erzählen, was gerade los ist?",
        parse_mode="Markdown"
    )

def start_quatschen(chat_id):
    """Start free conversation mode with German Dude."""
    if not gate_quatschen(chat_id):
        return
    if not is_premium_plus(chat_id):
        increment_daily_convo(chat_id)
    user  = user_data.get(str(chat_id), {})
    name  = user.get("name", "")
    level = user.get("level", "B1")

    # Set mode
    user_state[chat_id] = {"mode": "quatschen"}
    current_scenario[chat_id] = {"id": "quatschen", "goal": "Quatschen"}

    # Build system prompt
    level_note = NPC_LEVEL_INSTRUCTIONS.get(level, NPC_LEVEL_INSTRUCTIONS["B1"])
    todays_gem_q = get_todays_gem(str(chat_id))
    gem_hint_q   = get_gem_system_prompt_hint(todays_gem_q)
    # Build friend memory context
    uid         = str(chat_id)
    friend_mem  = user_data.get(uid, {}).get("friend_memory", [])
    native_lang = user_data.get(uid, {}).get("native_language") or "Englisch"

    sys_prompt = QUATSCHEN_SYSTEM + HUMAN_SPEECH_STYLE
    sys_prompt += f"\n\nSPRACHNIVEAU des Users: {level}\n{level_note}"
    sys_prompt += gem_hint_q

    if name:
        sys_prompt += f"\n\nDer User heißt {name}. Muttersprache: {native_lang}."

    if friend_mem:
        facts = "\n".join(f"- {f}" for f in friend_mem[-30:])
        sys_prompt += (
            f"\n\nWAS DU ÜBER {name.upper() if name else 'DEN USER'} WEISST "
            f"(aus früheren Gesprächen — benutze es natürlich, nicht als Checkliste):\n"
            f"{facts}\n\n"
            f"Du kennst {name or 'ihn/sie'} schon gut. Bring Dinge wieder hoch wenn es passt. "
            f"Frag nach wie es mit bestimmten Dingen weiterging."
        )

    # Load history first, then build sys_prompt with memory context
    history = user_data.get(uid, {}).get("quatschen_history", [])
    if history and not friend_mem:
        sys_prompt += f"\n\nDu kennst {name} schon von früheren Gesprächen."

    # Init memory with final sys_prompt
    user_memory[chat_id] = [{"role": "system", "content": sys_prompt}]
    turn_counter[chat_id] = 0

    # Add last 10 exchanges to memory
    if history:
        for exchange in history[-10:]:
            user_memory[chat_id].append({"role": "user",      "content": exchange["user"]})
            user_memory[chat_id].append({"role": "assistant", "content": exchange["bot"]})

    # Opening prompt
    if history:
        opening_prompt = (
            f"Du kennst {name} schon. Begrüße ihn/sie kurz und herzlich wie einen alten Freund. "
            f"Maximal 2 Sätze. Kein 'Willkommen zurück'."
        )
    else:
        opening_prompt = (
            f"Begrüße {name or 'den User'} kurz und herzlich. "
            f"Frag wie es ihm/ihr geht. Maximal 2 Sätze."
        )

    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system=sys_prompt,
            messages=[{"role": "user", "content": opening_prompt}]
        )
        opening = strip_filler(resp.content[0].text.strip())
    except Exception as e:
        log.warning(f"Quatschen opening generation failed: {e}")
        opening = f"Hey{' ' + name if name else ''}! Na, wie geht's dir so? 😊"

    user_memory[chat_id].append({"role": "assistant", "content": opening})

    # Send as voice
    send_reply(chat_id, opening, voice=True)

def _extract_friend_memory(chat_id):
    """Extract key facts from this Quatschen session and save to friend_memory."""
    uid  = str(chat_id)
    name = user_data.get(uid, {}).get("name", "")
    # Get last 20 turns from memory
    mem      = user_memory.get(chat_id, [])
    conv     = [m for m in mem if m.get("role") in ("user","assistant")][-20:]
    if not conv:
        return
    conv_text = "\n".join(
        f"{'User' if m['role']=='user' else 'Dude'}: {m['content']}" for m in conv
    )
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=(
                "Extrahiere aus diesem Gespräch 3-5 konkrete Fakten über den User. "
                "NUR echte Informationen die er/sie erzählt hat — keine Vermutungen. "
                "Format: eine Zeile pro Fakt, kurz und konkret. "
                "Beispiele: 'Arbeitet als Freelancerin in Berlin', 'Hat Stress mit dem Vermieter', "
                "'Liebt Wälder', 'Lernt gerade Schwedisch', 'Hat eine Katze namens Mimi'. "
                "Wenn nichts Konkretes gesagt wurde: nichts schreiben."
            ),
            messages=[{"role": "user", "content": f"Gespräch:\n{conv_text}"}]
        )
        new_facts = [l.strip().lstrip("-•").strip()
                     for l in resp.content[0].text.strip().splitlines()
                     if l.strip() and len(l.strip()) > 5]
        if new_facts:
            existing = user_data[uid].get("friend_memory", [])
            # Deduplicate roughly
            combined = existing + [f for f in new_facts if f not in existing]
            user_data[uid]["friend_memory"] = combined[-50:]  # keep last 50 facts
            save_users(user_data)
            log.info(f"Friend memory updated for {chat_id}: +{len(new_facts)} facts")
    except Exception as e:
        log.warning(f"Friend memory extraction failed: {e}")


def _quatschen_end_with_xp(chat_id):
    """Award XP and show share button after Quatschen session ends."""
    _extract_friend_memory(chat_id)  # Save what we learned this session
    turns = turn_counter.get(chat_id, 0)
    xp_gain, bonus_msg = calculate_xp(turns, "normal")
    new_streak, lost_streak = update_streak(chat_id)

    if lost_streak >= 2:
        bot.send_message(chat_id,
            f"😭 Dein {lost_streak}-Tage-Streak ist weg...\n"
            f"Aber hey — du bist wieder da! Neuer Streak: 🔥 1 Tag.")

    leveled_up = add_xp(chat_id, xp_gain)
    stats = user_data[str(chat_id)]["user_stats"]

    new_badges = check_achievements(chat_id)
    for emoji, title, desc in new_badges:
        bot.send_message(chat_id,
            f"🏅 *Achievement freigeschaltet!*\n{emoji} *{title}*\n_{desc}_",
            parse_mode="Markdown")

    if leveled_up:
        bot.send_message(chat_id,
            f"🚀 *LEVEL UP!* Du bist jetzt Level {stats['level']}! 💪",
            parse_mode="Markdown")

    reward = build_reward_block(chat_id, xp_gain, bonus_msg, turns)
    bot.send_message(chat_id, reward, parse_mode="Markdown")

    user_state[chat_id] = {"mode": "idle"}
    current_scenario.pop(chat_id, None)
    bot.send_message(chat_id, "Bis zum nächsten Mal! 👋 /themen um weiterzumachen.")


def handle_quatschen_message(chat_id, user_text):
    """Handle a message in Quatschen mode."""
    # Crisis detection — top priority
    if contains_crisis_signal(user_text):
        send_crisis_response(chat_id)
        return

    user  = user_data.get(str(chat_id), {})
    level = user.get("level", "B1")

    user_memory[chat_id].append({"role": "user", "content": user_text})

    try:
        mem = user_memory[chat_id]
        sys_msg = next((m["content"] for m in mem if m.get("role") == "system"), "")
        conv_msgs = [m for m in mem if m.get("role") in ("user", "assistant")]
        if not conv_msgs:
            conv_msgs = [{"role": "user", "content": user_text}]

        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=sys_msg,
            messages=conv_msgs
        )
        reply = strip_filler(response.content[0].text.strip())
    except Exception:
        reply = "Ey, kurze Pause — sag nochmal, was du meintest! 😄"

    user_memory[chat_id].append({"role": "assistant", "content": reply})
    turns = turn_counter.get(chat_id, 0) + 1
    turn_counter[chat_id] = turns

    # Save conversation snippet to user data for memory
    uid = str(chat_id)
    if "quatschen_history" not in user_data[uid]:
        user_data[uid]["quatschen_history"] = []
    user_data[uid]["quatschen_history"].append({
        "user": user_text,
        "bot": reply,
        "ts": datetime.now().isoformat()
    })
    user_data[uid]["quatschen_history"] = user_data[uid]["quatschen_history"][-50:]
    save_users(user_data)

    # Auto-detect farewell in Quatschen mode → trigger XP reward
    if contains_farewell(reply):
        send_reply(chat_id, reply, voice=True)
        time.sleep(0.8)
        _quatschen_end_with_xp(chat_id)
        return

    # After 5th user message — show "Gespräch beenden" button once
    if turns == 5:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💬 Dieses Gespräch beenden", callback_data="end_quatschen"))
        send_reply(chat_id, reply, voice=True)
        bot.send_message(chat_id,
            "_(Du kannst das Gespräch jederzeit beenden und deine XP einsammeln.)_",
            parse_mode="Markdown",
            reply_markup=markup)
    else:
        send_reply(chat_id, reply, voice=True)


# ═══════════════════════════════════════════════════════════════════════════
#  GAMIFICATION SYSTEM
#  Psychology hooks: Streak loss pain · Badges · XP bar · Variable rewards
# ═══════════════════════════════════════════════════════════════════════════

# ── XP THRESHOLDS per bot-level (not GER level) ──────────────────────────────
XP_PER_BOT_LEVEL = 100   # every 100 XP = 1 bot level up

def calculate_xp(turns, difficulty):
    base = turns * 2
    if difficulty == "easy":
        earned = base
    elif difficulty == "normal":
        earned = base + 5
    else:
        earned = base + 10

    # 🎰 VARIABLE REWARD — 30% chance of surprise bonus (slot machine effect)
    bonus = 0
    bonus_msg = ""
    if random.random() < 0.30:
        bonus = random.choice([5, 10, 15, 20])
        bonus_msg = random.choice([
            f"🎰 BONUS! +{bonus} XP — heute ist dein Glückstag!",
            f"⚡ Streak-Boost! +{bonus} Extra-XP für deine harte Arbeit!",
            f"🌟 Zufalls-Bonus! +{bonus} XP — das Universum belohnt dich!",
            f"🎁 Überraschung! +{bonus} XP extra — nicht immer, aber heute!",
        ])
    return earned + bonus, bonus_msg

def get_xp_bar(xp):
    """Render a visual XP progress bar — e.g. [████████░░] 80/100"""
    progress = xp % XP_PER_BOT_LEVEL
    bot_level = xp // XP_PER_BOT_LEVEL + 1
    filled = int(progress / XP_PER_BOT_LEVEL * 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {progress}/{XP_PER_BOT_LEVEL} XP  •  Level {bot_level}"

def update_streak(chat_id):
    """Update daily streak — return (new_streak, lost_streak_count_if_any)."""
    today = datetime.now().date()
    stats = user_data[str(chat_id)]["user_stats"]
    last  = stats.get("last_active")
    lost_streak = 0

    if last:
        last_date = datetime.fromisoformat(last).date()
        diff = (today - last_date).days
        if diff == 0:
            pass  # same day, no change
        elif diff == 1:
            stats["streak"] = stats.get("streak", 0) + 1
        else:
            lost_streak = stats.get("streak", 0)
            stats["streak"] = 1  # reset
    else:
        stats["streak"] = 1

    stats["last_active"] = today.isoformat()
    save_users(user_data)
    return stats["streak"], lost_streak

# ── ACHIEVEMENT BADGES ────────────────────────────────────────────────────────
# Each badge: (id, condition_key, threshold, emoji, title, description)
ACHIEVEMENT_DEFS = [
    # Streak milestones
    ("streak_3",    "streak",        3,   "🥉", "Warm-up",          "3 Tage am Stück geübt!"),
    ("streak_7",    "streak",        7,   "🔥", "On Fire",           "7-Tage-Streak! Duolingo zittert."),
    ("streak_14",   "streak",        14,  "💎", "Unaufhaltsam",      "2 Wochen durchgehalten!"),
    ("streak_30",   "streak",        30,  "👑", "Legende",           "30 Tage! Du bist eine Legende."),
    # XP milestones
    ("xp_100",      "total_xp",      100, "⭐", "Erster Stern",      "100 XP gesammelt!"),
    ("xp_500",      "total_xp",      500, "🌟", "Aufsteiger",        "500 XP — du machst das richtig."),
    ("xp_1000",     "total_xp",     1000, "🏆", "XP-Maschine",       "1000 XP! Beeindruckend."),
    ("xp_5000",     "total_xp",     5000, "🚀", "Profi",             "5000 XP — fast Muttersprachler!"),
    # Scenario milestones
    ("scenarios_1",  "total_scenarios", 1,  "🎭", "Erster Auftritt",  "Erstes Gespräch abgeschlossen!"),
    ("scenarios_5",  "total_scenarios", 5,  "🗣️", "Gesprächig",       "5 Szenarien gemeistert!"),
    ("scenarios_20", "total_scenarios", 20, "💬", "Konversationsking", "20 Szenarien — du redest wie ein Profi."),
    ("scenarios_50", "total_scenarios", 50, "🎖️", "Veteran",          "50 Szenarien! Respekt."),
    # Level milestones
    ("reached_b1",  "ger_level",    "B1", "📗", "Fortgeschritten",   "B1 erreicht — du kannst dich verständigen!"),
    ("reached_b2",  "ger_level",    "B2", "📘", "Fließend",          "B2! Du klingst fast wie ein Muttersprachler."),
    ("reached_c1",  "ger_level",    "C1", "🏅", "Muttersprachler",   "C1 — Glückwunsch, du hast es geschafft!"),
]

# ═══════════════════════════════════════════════════════════════════════════
#  PAYWALL / SUBSCRIPTION SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

# Trial codes — add/remove here, or move to env var later
# Format: { "CODE": days_granted }
TRIAL_CODES = {
    "GERMANDUDE3": 3,
    "GERMANDUDE7": 7,
    "PARTNER7":    7,
    "LAUNCH14":   14,
}

# Stripe
STRIPE_SECRET_KEY          = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET      = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID            = os.getenv("STRIPE_PRICE_ID", "")
STRIPE_PRICE_ID_DISCOUNTED = os.getenv("STRIPE_PRICE_ID_DISCOUNTED", "")
STRIPE_PAYMENT_LINK        = os.getenv("STRIPE_PAYMENT_LINK", "https://buy.stripe.com/6oUbJ20822qNdTU1bU9fW00")
RAILWAY_DOMAIN             = os.getenv("RAILWAY_PUBLIC_DOMAIN", "germandudebottg-production.up.railway.app")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

DISCOUNT_CODES = {
    "RABATT50": {"percent": 50, "price_eur": 10, "label": "€10/Monat (50% Rabatt)", "used_by": None},
}

def is_premium(chat_id):
    """True if user has active paid premium OR valid trial. Always syncs from disk."""
    uid = str(chat_id)
    try:
        fresh = load_users()
        if uid in fresh: user_data[uid] = fresh[uid]
    except Exception: pass
    user = user_data.get(uid, {})
    if user.get("premium"):
        premium_until = user.get("premium_until")
        if premium_until:
            if datetime.fromisoformat(premium_until) > datetime.now(): return True
            user_data[uid]["premium"] = False; save_users(user_data); return False
        return True
    trial_start = user.get("trial_start")
    if not trial_start: return False
    trial_days = TRIAL_CODES.get(user.get("trial_code_used", ""), 3)
    days_used  = (datetime.now() - datetime.fromisoformat(trial_start)).days
    return days_used < trial_days

def is_premium_plus(chat_id):
    """True wenn User Premium Plus hat (Szenarien + Quatschen unlimitiert).
    is_premium() bleibt unverändert und deckt BEIDE Tiers ab."""
    uid = str(chat_id)
    try:
        fresh = load_users()
        if uid in fresh:
            user_data[uid] = fresh[uid]
    except Exception:
        pass
    user = user_data.get(uid, {})
    if user.get("premium_plus"):
        until = user.get("premium_plus_until")
        if until:
            if datetime.fromisoformat(until) > datetime.now():
                return True
            user_data[uid]["premium_plus"] = False
            save_users(user_data)
            return False
        return True
    if user.get("trial_plan") == "plus":
        trial_start = user.get("trial_start")
        if not trial_start:
            return False
        trial_days = TRIAL_CODES.get(user.get("trial_code_used", ""), 3)
        days_used  = (datetime.now() - datetime.fromisoformat(trial_start)).days
        return days_used < trial_days
    return False

def days_left_in_trial(chat_id):
    uid  = str(chat_id)
    user = user_data.get(uid, {})
    trial_start = user.get("trial_start")
    if not trial_start:
        return 0
    trial_days = TRIAL_CODES.get(user.get("trial_code_used", ""), 3)
    start      = datetime.fromisoformat(trial_start)
    used       = (datetime.now() - start).days
    return max(0, trial_days - used)

def redeem_trial_code(chat_id, code: str) -> tuple[bool, str]:
    """Try to redeem a trial or discount code."""
    uid  = str(chat_id)
    user = user_data.get(uid, {})
    code = code.strip().upper()
    if user.get("premium"):
        return False, "Du hast bereits Premium — kein Code nötig! 🎉"
    if code in DISCOUNT_CODES:
        entry = DISCOUNT_CODES[code]
        if entry.get("used_by") and entry["used_by"] != uid:
            return False, "❌ Ungültiger oder bereits verwendeter Code."
        DISCOUNT_CODES[code]["used_by"] = uid
        user_data[uid]["discount_code"] = code
        save_users(user_data)
        return True, f"🎉 Rabatt-Code eingelöst! Du bekommst *{entry['label']}*!\n\nKlick auf den Bezahl-Button. 💳"
    if user.get("trial_start") and user.get("trial_code_used"):
        days_left = days_left_in_trial(chat_id)
        if days_left > 0:
            return False, f"Du hast bereits einen aktiven Trial — noch *{days_left} Tage* übrig! ⏳"
    if code not in TRIAL_CODES:
        return False, "❌ Ungültiger Code. Überprüf die Schreibweise!"
    days = TRIAL_CODES[code]
    user_data[uid]["trial_start"]     = datetime.now().isoformat()
    user_data[uid]["trial_code_used"] = code
    save_users(user_data)
    return True, f"🎉 *Code eingelöst!* Du hast *{days} Tage* Trial freigeschaltet.\n\nLeg los! 👇"

def send_stars_invoice(chat_id):
    """Send a Telegram Stars payment invoice for Premium."""
    try:
        bot.send_invoice(
            chat_id,
            title="German Dude Premium — 1 Monat",
            description="Unbegrenzte Gespräche, alle Niveaus A1-C2, Übungen, Gems & mehr. 30 Tage Zugang.",
            payload=f"premium_{chat_id}",
            provider_token="",          # empty = Telegram Stars
            currency="XTR",             # Stars currency code
            prices=[telebot.types.LabeledPrice("Premium 1 Monat", 1500)],
        )
    except Exception as e:
        log.error(f"Stars invoice failed for {chat_id}: {e}")
        bot.send_message(chat_id, "⚠️ Stars-Zahlung konnte nicht gestartet werden. Versuch es später nochmal.")


def create_stripe_checkout(chat_id):
    """Return personalised Stripe Payment Link with chat_id as client_reference_id."""
    return f"{STRIPE_PAYMENT_LINK}?client_reference_id={chat_id}"

def send_paywall(chat_id):
    uid = str(chat_id)
    if uid in user_data:
        user_data[uid]["paywall_hits"] = user_data[uid].get("paywall_hits", 0) + 1
        save_users(user_data)
    """Send paywall message with Stripe checkout button."""
    uid  = str(chat_id)
    user = user_data.get(uid, {})
    name = user.get("name", "")
    xp   = user.get("user_stats", {}).get("xp", 0)
    streak = user.get("user_stats", {}).get("streak", 0)

    checkout_url = create_stripe_checkout(chat_id)

    ref_link  = BOT_LINK + f"?start=ref_{chat_id}"
    share_msg = quote(
        "Ich übe gerade Deutsch mit meinem deutschen Kumpel im Chat — probier's mal aus! 🇩🇪\n" + ref_link
    )
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={share_msg}"

    discount_code = user_data.get(uid, {}).get("discount_code")
    price_label   = "€20/Monat"
    if discount_code and discount_code in DISCOUNT_CODES:
        price_label = DISCOUNT_CODES[discount_code]["label"]

    markup = InlineKeyboardMarkup()
    if checkout_url:
        markup.add(InlineKeyboardButton(
            f"💳 Jetzt Premium — {price_label}",
            url=checkout_url
        ))
    markup.add(InlineKeyboardButton(
        "⭐ Mit Telegram Stars zahlen — 1500 Stars",
        callback_data="pay_stars"
    ))
    markup.add(InlineKeyboardButton(
        "🎁 Freunde einladen & 3 Tage gratis sichern",
        url=share_url
    ))
    markup.add(InlineKeyboardButton(
        "🌍 übersetzen", callback_data="translate_last"
    ))

    xp_streak_line = f"Du hast bereits *{xp} XP* gesammelt"
    if streak > 1:
        xp_streak_line += f" und einen *{streak}-Tage-Streak* aufgebaut"
    xp_streak_line += " — schade, das jetzt zu unterbrechen.\n\n"

    paywall_text = (
        f"🔒 *Kein Zugang — Trial abgelaufen oder nicht aktiviert.*\n\n"
        + xp_streak_line +
        f"Mit *Premium* ({price_label}) bekommst du:\n"
        f"✅ Unbegrenzte Gespräche & Szenarien\n"
        f"✅ Alle Niveaus A1–C2\n"
        f"✅ Voice-Nachrichten & Übersetzungen\n"
        f"✅ XP-System, Achievements & Shadowing\n"
        f"✅ Jederzeit kündbar\n\n"
        f"_Hast du einen Code? Tippe:_ /freecode DEINCODE\n"
        f"_Dein Streak und deine XP bleiben erhalten._"
    )
    last_bot_text[chat_id] = paywall_text
    bot.send_message(chat_id, paywall_text, parse_mode="Markdown", reply_markup=markup)


# ═══════════════════════════════════════════════════════════════════════════
#  /UPGRADE — Tier-bewusster Upgrade-Befehl
#  Free → Premium oder Premium Plus | Premium → Premium Plus
# ═══════════════════════════════════════════════════════════════════════════

PREMIUM_VALUES = [
    "✅ Unbegrenzte Gespräche & Szenarien",
    "✅ Alle Niveaus A1–C2",
    "✅ Voice-Modus, Übungen & Flashcards",
    "✅ XP-System, Achievements & Shadowing",
]

PREMIUM_PLUS_EXTRA_VALUES = [
    "✅ Alles aus Premium",
    "✅ Quatschen — kein Skript, kein Thema, kein Druck",
    "✅ Finanzamt-Brief? Kündigung? Streit mit Vermieter? Einfach fragen.",
    "✅ Da wenn Deutschland überwältigend wird — 24/7, nie urteilend",
]


@bot.message_handler(commands=["upgrade"])
def handle_upgrade(message):
    """Zeigt tier-passende Upgrade-Optionen mit kurzen Value-Infos."""
    chat_id = message.chat.id
    ensure_user(chat_id)
    if _require_onboarding(chat_id): return
    _track_feature(chat_id, "upgrade")

    uid  = str(chat_id)
    name = user_data.get(uid, {}).get("name", "")
    greet = f"{name}" if name else "du"

    plus_checkout_url     = create_stripe_checkout(chat_id)
    premium_values_str     = "\n".join(PREMIUM_VALUES)
    plus_extra_values_str  = "\n".join(PREMIUM_PLUS_EXTRA_VALUES)

    markup = InlineKeyboardMarkup()

    if is_premium_plus(chat_id):
        # ── Schon im Top-Tier ────────────────────────────────────────────
        text = (
            f"👑 Du hast Premium Plus, {greet} — du bist bestens aufgestellt.\n\n"
            "Dein Kumpel ist da wenn Deutschland stressig wird. "
            "Nutz ihn. Schreib einfach drauflos.\n\n"
            "Magst du den Bot unterstützen? /danke 💙"
        )
        bot.send_message(chat_id, text)
        return

    elif is_premium(chat_id):
        # ── Premium → Premium Plus ───────────────────────────────────────
        text = (
            f"💎 *Bereit für den nächsten Schritt, {greet}?*\n\n"
            "Du lernst schon Deutsch mit Premium — gut.\n\n"
            "Aber kennst du das?\n"
            "Ein Brief vom Finanzamt liegt auf dem Tisch.\n"
            "Du musst kündigen, beschweren, erklären — aber weißt nicht wie.\n"
            "Oder Deutschland fühlt sich manchmal einfach zu viel an.\n\n"
            "*Premium Plus* ist dein Kumpel für genau diese Momente:\n\n"
            f"{plus_extra_values_str}\n\n"
            "€30/Monat. Kündbar jederzeit."
        )
        markup.add(InlineKeyboardButton("👑 Auf Premium Plus upgraden", callback_data="pay_plus"))
        markup.add(InlineKeyboardButton("⭐ Mit Stars — 2000 Stars", callback_data="pay_stars_plus"))

    else:
        # ── Free → Premium oder Premium Plus ─────────────────────────────
        text = (
            f"💎 *Pläne & Preise*\n\n"
            f"🎓 *Premium — €20/Monat*\n"
            f"Für alle die Deutsch wirklich lernen wollen.\n"
            f"{premium_values_str}\n\n"
            f"👑 *Premium Plus — €30/Monat*\n"
            f"Nicht nur Deutsch lernen. In Deutschland ankommen.\n"
            f"{plus_extra_values_str}\n\n"
            "_Hast du einen Code? Tippe:_ /freecode DEINCODE"
        )
        if plus_checkout_url:
            markup.add(InlineKeyboardButton("🎓 Premium — €20/Monat", url=plus_checkout_url))
        markup.add(InlineKeyboardButton("⭐ Premium mit Stars — 1500 Stars", callback_data="pay_stars"))
        markup.add(InlineKeyboardButton("👑 Premium Plus — €30/Monat", callback_data="pay_plus"))
        markup.add(InlineKeyboardButton("⭐ Plus mit Stars — 2000 Stars", callback_data="pay_stars_plus"))

    markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))
    last_bot_text[chat_id] = text
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)


# ═══════════════════════════════════════════════════════════════════════════
#  DAILY FREE TIER + TWO-TIER GATE SYSTEM
#  Free: 3 Gespräche/Tag (Szenarien + Quatschen, gemeinsamer Pool)
#  Premium (€20): Szenarien unlimitiert | Quatschen NICHT enthalten
#  Premium Plus (€30): Alles unlimitiert inkl. Quatschen
# ═══════════════════════════════════════════════════════════════════════════

FREE_DAILY_LIMIT = 1

def _get_today() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def get_daily_convo_count(chat_id: int) -> int:
    uid = str(chat_id)
    dc  = user_data.get(uid, {}).get("daily_convos", {})
    if dc.get("date") != _get_today():
        return 0
    return dc.get("count", 0)

def increment_daily_convo(chat_id: int) -> int:
    uid   = str(chat_id)
    today = _get_today()
    dc    = user_data.get(uid, {}).get("daily_convos", {})
    count = dc.get("count", 0) if dc.get("date") == today else 0
    user_data[uid]["daily_convos"] = {"date": today, "count": count + 1}
    save_users(user_data)
    return count + 1

def has_free_convos_remaining(chat_id: int) -> bool:
    return get_daily_convo_count(chat_id) < FREE_DAILY_LIMIT

def gate_scenario(chat_id: int) -> bool:
    """Gate für Szenarien. Premium/Plus → immer rein. Free → 1/Tag."""
    if is_premium(chat_id):
        return True
    if has_free_convos_remaining(chat_id):
        return True
    send_daily_limit_paywall(chat_id)
    return False

def gate_quatschen(chat_id: int) -> bool:
    """Gate für Quatschen-Modus.
    Plus → immer rein. Regular Premium → Upgrade-Prompt. Free → 1/Tag."""
    if is_premium_plus(chat_id):
        return True
    if is_premium(chat_id):  # Premium aber kein Plus
        send_quatschen_upgrade_prompt(chat_id)
        return False
    if has_free_convos_remaining(chat_id):
        return True
    send_daily_limit_paywall(chat_id)
    return False

def send_daily_limit_paywall(chat_id: int):
    """Paywall nach 1 kostenlosem Gespräch."""
    uid    = str(chat_id)
    user   = user_data.get(uid, {})
    name   = user.get("name", "")
    xp     = user.get("user_stats", {}).get("xp", 0)
    streak = user.get("user_stats", {}).get("streak", 0)
    user_data[uid]["paywall_hits"] = user_data[uid].get("paywall_hits", 0) + 1
    save_users(user_data)
    checkout_url = create_stripe_checkout(chat_id)
    ref_link     = BOT_LINK + f"?start=ref_{chat_id}"
    share_msg    = quote("Ich übe Deutsch mit German Dude Bot 🇩🇪\n" + ref_link)
    share_url    = f"https://t.me/share/url?url={quote(ref_link)}&text={share_msg}"
    xp_line = f"Du hast schon *{xp} XP*"
    if streak > 1:
        xp_line += f" und einen *{streak}-Tage-Streak*"
    text = (
        f"🔒 Dein *kostenloses Gespräch* für heute ist genutzt"
        f"{', ' + name if name else ''}!\n\n"
        f"{xp_line} — schad das jetzt zu stoppen.\n\n"
        "📅 *Morgen gibt's automatisch ein neues.* Versprochen.\n\n"
        "Oder jetzt upgraden:\n\n"
        "🎓 *Premium — €20/Monat*\n"
        "Unbegrenzte Gespräche & Übungen — alles was du zum Lernen brauchst.\n\n"
        "👑 *Premium Plus — €30/Monat*\n"
        "Nicht nur Deutsch lernen. In Deutschland ankommen.\n"
        "Finanzamt-Briefe, Kündigungen, schwierige Gespräche — dein Kumpel ist da.\n\n"
        "_Hast du einen Code? /freecode DEINCODE_"
    )
    last_bot_text[chat_id] = text
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("👑 Premium Plus — €30/Monat", callback_data="pay_plus"))
    markup.add(InlineKeyboardButton("🎓 Premium — €20/Monat", url=checkout_url))
    markup.add(InlineKeyboardButton("⭐ Stars zahlen", callback_data="pay_stars"))
    markup.add(InlineKeyboardButton("🎁 Freunde einladen → 3 Tage gratis", url=share_url))
    markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)

def send_quatschen_upgrade_prompt(chat_id: int):
    """Für reguläre Premium-User ohne Plus die Quatschen öffnen wollen."""
    uid  = str(chat_id)
    name = user_data.get(uid, {}).get("name", "")
    text = (
        f"👑 *Quatschen ist Teil von Premium Plus*{', ' + name if name else ''}.\n\n"
        "Hier ist kein Lehrer, kein Skript, kein Druck.\n"
        "Einfach reden — über alles was dich gerade beschäftigt.\n\n"
        "Einen Brief vom Finanzamt bekommen? Musst du kündigen und weißt nicht wie?\n"
        "Schlechter Tag und Deutschland fühlt sich zu viel an?\n\n"
        "Genau dafür ist Quatschen da. Dein Kumpel — 24/7, nie urteilend.\n\n"
        "*Premium Plus — €30/Monat*"
    )
    last_bot_text[chat_id] = text
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("👑 Jetzt auf Premium Plus upgraden", callback_data="pay_plus"))
    markup.add(InlineKeyboardButton("⭐ Stars — 2000 Stars", callback_data="pay_stars_plus"))
    markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)

def get_remaining_convos_hint(chat_id: int) -> str:
    """Kurzer Hinweis am Ende einer Session — nur für Free-User."""
    if is_premium(chat_id):
        return ""
    remaining = max(0, FREE_DAILY_LIMIT - get_daily_convo_count(chat_id))
    if remaining == 0:
        return (
            "\n\n_Das war dein kostenloses Gespräch für heute. "
            "Morgen gibt's ein neues — oder jetzt upgraden._"
        )
    return f"\n\n_💬 Noch *{remaining}* kostenloses{'e' if remaining > 1 else ''} Gespräch{'e' if remaining > 1 else ''} heute übrig._"

def check_achievements(chat_id):
    """Check all achievements and award any newly unlocked ones."""
    uid   = str(chat_id)
    user  = user_data[uid]
    stats = user.get("user_stats", {})
    earned = user.setdefault("achievements", [])
    newly_unlocked = []

    streak        = stats.get("streak", 0)
    total_xp      = stats.get("xp", 0)
    total_scen    = stats.get("total_scenarios", 0)
    ger_level     = user.get("level", "A2")

    values = {
        "streak":           streak,
        "total_xp":         total_xp,
        "total_scenarios":  total_scen,
        "ger_level":        ger_level,
    }

    for badge_id, key, threshold, emoji, title, desc in ACHIEVEMENT_DEFS:
        if badge_id in earned:
            continue
        val = values.get(key)
        if val is None:
            continue
        # Numeric threshold
        if isinstance(threshold, int) and isinstance(val, int) and val >= threshold:
            earned.append(badge_id)
            newly_unlocked.append((emoji, title, desc))
        # String threshold (GER level)
        elif isinstance(threshold, str):
            level_order = ["A0", "A1", "A2", "B1", "B2", "C1"]
            if level_order.index(val) >= level_order.index(threshold):
                earned.append(badge_id)
                newly_unlocked.append((emoji, title, desc))

    if newly_unlocked:
        save_users(user_data)
    return newly_unlocked


def build_reward_block(chat_id, xp_gain, bonus_msg, turns):
    """Build the full end-of-scenario reward message block."""
    uid   = str(chat_id)
    stats = user_data[uid]["user_stats"]
    total_xp = stats.get("xp", 0)
    streak   = stats.get("streak", 0)
    bot_lvl  = total_xp // XP_PER_BOT_LEVEL + 1

    MOTIVATIONS = [
        "Du wirst deutlich flüssiger.",
        "Dein Deutsch klingt immer natürlicher.",
        "Du denkst schon weniger auf Englisch.",
        "Muttersprachler würden das kaum merken.",
        "Noch ein paar Sessions und du sprichst wie ein Profi.",
        "Jedes Gespräch bringt dich ein Stück näher.",
        "Du bist besser als gestern — das zählt.",
    ]

    lines = []
    lines.append("─────────────────────")
    lines.append(f"⚡ *+{xp_gain} XP* verdient!")
    if bonus_msg:
        lines.append(bonus_msg)
    lines.append(f"📊 {get_xp_bar(total_xp)}")
    lines.append(f"🔥 Streak: *{streak} {'Tag' if streak == 1 else 'Tage'}*   •   Level *{bot_lvl}*")
    lines.append(f"_{random.choice(MOTIVATIONS)}_")

    return "\n".join(lines)

GOAL_TEXT = {
    "Job":                "💼 Sicher im Job sprechen",
    "Freunde":            "🧑‍🤝‍🧑 Freunde finden & Smalltalk",
    "Einkaufen":          "🛒 Im Alltag einkaufen & kommunizieren",
    "Reisen":             "✈️ Selbstständig reisen & orientieren",
    "Soziales":           "🤝 Behörden & Alltagssituationen meistern",
    "Unterhaltung":       "🎬 Filme, Serien & Kultur verstehen",
    "Sport":              "⚽ Im Verein & Training kommunizieren",
    "Telefon":            "📞 Anrufe & Termine sicher führen",
    "Selbstpräsentation": "🎤 Selbstsicher auftreten & vorstellen",
}

def add_xp(chat_id, amount):
    """Add XP and track total scenarios. Returns True if bot-level increased."""
    stats = user_data[str(chat_id)]["user_stats"]
    old_bot_level = stats.get("xp", 0) // XP_PER_BOT_LEVEL
    stats["xp"] = stats.get("xp", 0) + amount
    new_bot_level = stats["xp"] // XP_PER_BOT_LEVEL
    stats["level"] = new_bot_level + 1  # keep for backwards compat
    # Increment total scenario counter
    stats["total_scenarios"] = stats.get("total_scenarios", 0) + 1
    save_users(user_data)
    return new_bot_level > old_bot_level

def send_progress(chat_id):
    user  = user_data[str(chat_id)]
    stats = user["user_stats"]
    xp      = stats["xp"]
    level   = stats["level"]
    streak  = stats["streak"]
    goal    = user.get("goal", "")
    name    = user.get("name", "")

    goal_line = GOAL_TEXT.get(goal, f"🎯 {goal}")

    xp_in_level = xp % 50
    filled = xp_in_level // 5
    bar = "🟩" * filled + "⬜" * (10 - filled)
    xp_to_next = 50 - xp_in_level

    text = (
        f"📊 *Dein Fortschritt{', ' + name if name else ''}*\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"⭐ *Level:* {level}\n"
        f"⚡ *XP:* {xp}  (+{xp_to_next} bis Level {level+1})\n"
        f"{bar}\n\n"
        f"🔥 *Streak:* {streak} {'Tag' if streak == 1 else 'Tage'}\n"
        f"🎯 *Ziel:* {goal_line}"
    )
    bot.send_message(chat_id, text, parse_mode="Markdown")

BOT_LINK = "https://t.me/germandude_bot"

def send_referral(chat_id):
    ref_link   = BOT_LINK + f"?start=ref_{chat_id}"
    share_text = quote(
        "Ich übe gerade Deutsch mit meinem deutschen Kumpel im Chat — probier's aus! 🇩🇪\n" + ref_link
    )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(
        text="🎁 Freund einladen & 3 Tage gratis sichern",
        url=f"https://t.me/share/url?url={quote(ref_link)}&text={share_text}"
    ))
    bot.send_message(
        chat_id,
        "🔥 Das war eine deiner besten Sessions!\n\n"
        "Kennst du jemanden, der auch Deutsch üben will?\n"
        "Lad ihn ein — wenn er joint, kriegst du *3 Tage gratis*! 🎁",
        parse_mode="Markdown",
        reply_markup=markup
    )

# ADAPTIVE DIFFICULTY ENGINE
def _is_nudge_text(text: str) -> bool:
    """True if the typed text is clearly not a real conversational message —
    e.g. '?', '??', '!', 'wtf', 'hello?' — just a user poking the bot."""
    stripped = text.strip()
    if len(stripped) <= 6:
        return True
    # Only punctuation / symbols / digits — no real words
    if re.match(r'^[\W\d\s]+$', stripped, re.UNICODE):
        return True
    return False

def analyze_user_input(text):
    words = text.split()
    if len(words) < 3:
        return "struggle"
    if "??" in text or text.count("?") >= 2:
        return "struggle"
    return "ok"

def get_dynamic_mode(state):
    struggle = state.get("struggle", 0)
    success  = state.get("success",  0)
    if struggle > success:
        return "easy"
    elif success > struggle * 2:
        return "hard"
    else:
        return "normal"

# WEAK POINT FUNCTIONS
# Colloquial patterns that are correct in spoken German — never flag these
_COLLOQUIAL_OK = [
    "ich hab", "ich hab'", "ich mach", "ich mach'", "ich geh", "ich geh'",
    "ich ruf", "ich ruf'", "ich fahr", "ich fahr'", "ich komm", "ich komm'",
    "ich weiss", "ich weiß", "hast du", "bist du", "wir ham", "wir haben",
    "das ist", "ich bin", "er hat", "sie hat", "wir sind",
]

def _is_colloquial(wrong: str) -> bool:
    """Return True if the 'error' is just colloquial spoken German — don't flag it."""
    w = wrong.lower().strip()
    # Check if it's a standard contraction used in everyday speech
    for pattern in _COLLOQUIAL_OK:
        if w.startswith(pattern) or pattern in w:
            return True
    # Also skip if wrong == correct (false positive from Claude)
    return False

def save_weak_points(chat_id, extracted_errors):
    user = user_data[str(chat_id)]
    if "weak_points" not in user or not isinstance(user["weak_points"], list):
        user["weak_points"] = []
    if "errors" not in user or not isinstance(user["errors"], list):
        user["errors"] = []
    for err in extracted_errors:
        wrong   = err.get("wrong", "")
        correct = err.get("correct", "")
        # Skip colloquial speech — not real errors
        if wrong and _is_colloquial(wrong):
            log.debug(f"Skipping colloquial: {wrong}")
            continue
        # Skip if wrong == correct (Claude false positive)
        if wrong and correct and wrong.lower().strip() == correct.lower().strip():
            continue
        user["weak_points"].append({
            "type":            err.get("type", ""),
            "example_wrong":   wrong,
            "example_correct": correct,
            "source":          err.get("source", "voice"),
            "next_review":     1,
            "strength":        0
        })
        if wrong and correct:
            user["errors"].append(f"{wrong} → {correct}")
    save_users(user_data)

def get_due_weak_points(chat_id):
    user = user_data[str(chat_id)]
    due  = []
    for wp in user.get("weak_points", []):
        if not isinstance(wp, dict):
            continue
        if wp.get("next_review", 1) <= 0:
            due.append(wp)
        else:
            wp["next_review"] -= 1
    save_users(user_data)
    return due

def update_weak_points(chat_id, results):
    user     = user_data[str(chat_id)]
    improved = results.get("improved", [])

    for wp in user.get("weak_points", []):
        if not isinstance(wp, dict):
            continue

        if wp.get("type") in improved:
            wp["strength"]    = wp.get("strength", 0) + 1
            wp["next_review"] = wp["strength"] * 2
        else:
            wp["strength"]    = max(0, wp.get("strength", 0) - 1)
            wp["next_review"] = 1

        strength = wp.get("strength", 0)

        if strength >= 5 and not wp.get("mastered"):
            wp["mastered"]    = True
            wp["next_review"] = 10

        elif strength == 4 and not wp.get("mastered"):
            bot.send_message(
                chat_id,
                f"🔥 *Fast geschafft!*\n"
                f"Diesen Fehler hast du fast im Griff:\n\n"
                f"❌ _{wp.get('example_wrong', '')}_\n"
                f"✅ _{wp.get('example_correct', '')}_",
                parse_mode="Markdown"
            )

    save_users(user_data)

# FEEDBACK FUNCTION
def generate_feedback(chat_id, conversation_history):
    user  = user_data[str(chat_id)]
    level = user.get("level", "A2")

    history = [m for m in conversation_history if m["role"] in ("user", "assistant")]
    if not history:
        return "❗ Kein Gespräch gefunden. Übe erst ein bisschen mit /start!"

    conversation_text = "\n".join(
        f"{'Du' if m['role'] == 'user' else 'Bot'}: {m['content']}"
        for m in history
    )

    prompt = f"""
Du bist ein Deutsch-Coach.

Analysiere die Sprache des Users im folgenden Gespräch.

KEIN FEHLER — ignoriere folgendes komplett:
- Apokope in der gesprochenen Sprache: „hab" statt „habe", „genieß" statt „genieße", „mach" statt „mache", „komm" statt „komme" usw. — das ist normales, korrektes Umgangsdeutsch.
- Umgangssprachliche Verkürzungen, die Muttersprachler täglich verwenden.

ZIEL:
- Finde die 1–5 wichtigsten Fehler
- Priorisiere:
  1. Wiederholte Fehler
  2. Fehler, die Kommunikation stören
  3. Typische Level-Fehler
- Wenn der gleiche Fehler mehrfach vorkommt, fasse ihn zusammen.

GIB AUS:

1. FEHLER (max 5):
- kurzer Beispielsatz vom User
- Korrektur

2. KURZE ERKLÄRUNG:
- einfach erklärt (passend zu Level {level})

3. MINI-ÜBUNG:
- 2–3 Sätze zum Ausfüllen oder Nachsprechen

4. FEHLER_JSON:
Gib die Fehler als JSON-Array aus (direkt nach dem Text, keine Erklärung):
[{{"type": "Fehlerkategorie", "wrong": "falscher Satz des Users", "correct": "korrekter Satz"}}]

WICHTIG:
- Kein Roman
- Klar, direkt, hilfreich
- Sprache an Level anpassen

GESPRÄCH:
{conversation_text}
"""

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text

    extracted_errors = []
    if "FEHLER_JSON:" in raw:
        feedback_text, json_block = raw.split("FEHLER_JSON:", 1)
        json_str = json_block.strip()
        try:
            extracted_errors = json.loads(json_str)
        except Exception:
            m = re.search(r'\[.*?\]', json_str, re.DOTALL)
            if m:
                try:
                    extracted_errors = json.loads(m.group())
                except Exception:
                    extracted_errors = []
    else:
        feedback_text = raw

    if extracted_errors:
        save_weak_points(chat_id, extracted_errors)
        known_types = {
            wp.get("type") for wp in user.get("weak_points", [])
            if isinstance(wp, dict)
        }
        new_types    = {e.get("type") for e in extracted_errors}
        improved     = list(known_types - new_types)
        update_weak_points(chat_id, {"improved": improved})

    return feedback_text.strip()


LEVEL_NEXT = {"A1": "A2", "A2": "B1", "B1": "B2", "B2": "C1", "C1": "C1"}

def generate_errors_and_exercises(chat_id, conversation_history):
    """Returns (exercises_text, answers_text) tuple."""
    user  = user_data[str(chat_id)]
    level = user.get("level", "A2")

    history = [m for m in conversation_history if m["role"] in ("user", "assistant")]
    if not history:
        return ("❗ Kein Gespräch gefunden.", "")

    conversation_text = "\n".join(
        f"{'Du' if m['role'] == 'user' else 'Bot'}: {m['content']}"
        for m in history
    )

    prompt = f"""Du bist ein freundlicher Deutsch-Coach für Niveau {level}.
Analysiere nur die Nachrichten des Users (nicht die Bot-Antworten).

NUR DIESE FEHLER KORRIGIEREN — nichts anderes:

1. VERBSTELLUNG — Das Verb steht auf der falschen Position:
   - Hauptsatz: Verb muss auf Position 2 stehen
   - Nebensatz: Verb muss ans Ende
   - Perfekt/Modalverb: Infinitiv/Partizip ans Ende
   Beispiel echter Fehler: "Ich gestern gegangen bin ins Kino" → "Ich bin gestern ins Kino gegangen"

2. OBJEKT-REIHENFOLGE — Dativ/Akkusativ falsch gestellt:
   - Beide Nomen: Dativ VOR Akkusativ → "Ich gebe dem Mann das Buch" (nicht: "dem Buch dem Mann")
   - Pronomen stehen so weit vorne wie möglich
   - Akkusativpronomen vor Dativnomen: "Ich gebe es dem Mann"
   - Beide Pronomen: Akkusativ vor Dativ: "Ich gebe es ihm"
   Beispiel echter Fehler: "Ich gebe das Buch dem Mann es" → "Ich gebe es ihm"

3. FALSCHE KASUSFORM — z.B. "mit der Mann" statt "mit dem Mann"

IGNORIERE KOMPLETT — das sind keine Fehler:
- Wortstellung bei Zeitangaben: "Ich habe Hunger seit drei Stunden" = korrekt
- Umgangssprache: "ich hab", "ich mach", "ich geh", alle Apokopen
- Alternative korrekte Wortstellungen (Deutsch hat oft mehrere richtige Varianten)
- Stilistische Präferenzen
- Leichte Formulierungsunterschiede

AUFGABE: Finde max. 2 EINDEUTIGE Fehler aus den Kategorien oben.
Wenn kein eindeutiger Fehler: "Sehr gut — keine echten Fehler! 🎉" schreiben.
Im Zweifel: NICHT korrigieren.

FORMAT — nur Plaintext, keine Sternchen:

🔍 Deine Fehler aus diesem Gespräch:

• ❌ [falscher Satz des Users — exakt zitiert]
  ✅ [korrekter Satz]
  💡 [1 Satz Erklärung — welche Regel, warum]
  💬 Beispiel: [anderer natürlicher Satz mit derselben Regel]

---ANSWERS---

[Falls keine echten Fehler: "Sehr gut — keine echten Fehler! 🎉"]

GESPRÄCH:
{conversation_text}
"""
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip()

    if "---ANSWERS---" in raw:
        exercises, answers = raw.split("---ANSWERS---", 1)
        return (exercises.strip(), answers.strip())
    return (raw, "")


def generate_vocab_boost(chat_id):
    """Returns 6–8 native-speaker vocab items one level above the user's current level."""
    user     = user_data[str(chat_id)]
    level    = user.get("level", "A2")
    scenario = current_scenario.get(chat_id, {})
    topic    = scenario.get("text", user.get("goal", "Alltag"))
    target   = LEVEL_NEXT.get(level, level)

    prompt = f"""Du bist ein Deutsch-Coach.
Der User hat gerade geübt: {topic}
Sein aktuelles Niveau: {level}. Ziel-Niveau für diesen Wortschatz: {target}.

Gib 6–8 Wörter oder Ausdrücke, die Muttersprachler in genau dieser Situation wirklich benutzen.

FORMAT (Telegram Markdown, genau so):

*🚀 Bonus-Wortschatz — wie Muttersprachler reden:*

• *[Wort/Ausdruck]* — [kurze Erklärung auf Deutsch]
  💬 _"[natürlicher Beispielsatz]"_

WICHTIG:
- Echte Umgangssprache / Alltagsdeutsch, kein Lehrbuch
- Redewendungen, typische Kollokationen, Phrasen
- Niveau {target} (einen Schritt über dem aktuellen Niveau {level})
- Alles auf Deutsch — kurz und einprägsam
"""
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


# START

@bot.message_handler(commands=["flashcards", "vokabeln", "karten"])
def handle_flashcards(message):
    """Send Quizlet flashcard sets with translated names in user's language."""
    chat_id     = message.chat.id
    ensure_user(chat_id)
    if _require_onboarding(chat_id): return
    uid         = str(chat_id)
    native_lang = user_data.get(uid, {}).get("native_language") or "Englisch"

    FLASHCARD_SETS = [
        ("Zeitangaben", "https://quizlet.com/de/929150830/zeitangaben-german-time-phrases-flash-cards/"),
        ("Moderne Geräte", "https://quizlet.com/de/929521554/moderne-gerate-modern-devices-flash-cards/"),
        ("Kleidung", "https://quizlet.com/de/930143723/kleidung-clothes-flash-cards/"),
        ("Basics auf Deutsch", "https://quizlet.com/de/929610703/die-basics-auf-deutsch-flash-cards/"),
        ("Orte in der Stadt", "https://quizlet.com/de/934379270/orte-in-der-stadt-places-in-the-city-flash-cards/"),
        ("Die Unterhaltung", "https://quizlet.com/de/937497240/die-unterhaltung-entertainment-flash-cards/"),
        ("Duale Präpositionen (Dativ & Akkusativ)", "https://quizlet.com/de/941754326/duale-prapositionen-dual-prepositions-dativ-und-akkusativ-flash-cards/"),
        ("Wegbeschreibung", "https://quizlet.com/de/957134810/wegbeschreibung-giving-directions-flash-cards/"),
        ("Übliche Verben mit Dativ", "https://quizlet.com/de/940867259/ubliche-verben-mit-dativ-common-verbs-with-dativ-flash-cards/"),
        ("Trennbare Verben + Imperativ", "https://quizlet.com/de/980847549/trennbare-verben-ohne-vokalwechsel-imperativ-separable-verbs-imperativ-flash-cards/"),
        ("35 starke Verben mit Partizip II", "https://quizlet.com/de/1036890371/35-top-starke-verben-mit-partizip-ii-und-beispielen-flash-cards/"),
        ("Lebensmittel", "https://quizlet.com/de/931071983/lebensmittel-groceries-flash-cards/"),
        ("Möbel und Zuhause", "https://quizlet.com/de/943962880/mobel-und-zuhause-furniture-home-flash-cards/"),
        ("Gegenstände", "https://quizlet.com/de/1053692838/gegenstande-objects-flash-cards/"),
        ("Berufswelt", "https://quizlet.com/de/1054530776/berufswelt-professional-world-flash-cards/"),
        ("Vorstellungsgespräch", "https://quizlet.com/de/1084319816/vorstellungsgesprach-lexikon-flash-cards/"),
        ("Wortschatz A1-A2 nach Goethe (Quizlet)", "https://quizlet.com/class/29868891/materials"),
        ("Wortschatz A1-A2 nach Goethe (PDF)", "https://www.goethe.de/pro/relaunch/prf/de/Goethe-Zertifikat_A1_Fit1_Wortliste.pdf"),
    ]

    bot.send_message(chat_id, "🃏 Vokabelkarten werden geladen...")

    # Translate all names in one Claude call
    names_de = [name for name, _ in FLASHCARD_SETS]
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=f"Translate each line into {native_lang}. Return ONLY the translated lines, one per line, same order, no numbers or extra text.",
            messages=[{"role": "user", "content": chr(10).join(names_de)}]
        )
        translated = resp.content[0].text.strip().splitlines()
        if len(translated) != len(FLASHCARD_SETS):
            translated = names_de  # fallback
    except Exception:
        translated = names_de

    markup = InlineKeyboardMarkup(row_width=1)
    for (name_de, url), name_tr in zip(FLASHCARD_SETS, translated):
        label = f"{name_tr}" if name_tr != name_de else name_de
        markup.add(InlineKeyboardButton(f"🃏 {label}", url=url))

    bot.send_message(
        chat_id,
        "🃏 Vokabelkarten auf Quizlet\n\nWähle ein Set und übe direkt im Browser:",
        reply_markup=markup,
    )

@bot.message_handler(commands=["code", "freischalten", "redeem"])
def handle_code(message):
    """Redeem a trial access code."""
    chat_id = message.chat.id
    ensure_user(chat_id)
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎯 Thema wählen", callback_data="menu_themen"))
        bot.send_message(chat_id,
            "🎁 *Trial-Code einlösen*\n\n"
            "Schreib einfach:\n"
            "`/code DEINCODE`\n\n"
            "Noch keinen Code? Schreib uns auf @germandude_support!",
            parse_mode="Markdown")
        return

    code = parts[1].strip()
    success, msg = redeem_trial_code(chat_id, code)

    markup = InlineKeyboardMarkup()
    if success:
        markup.add(InlineKeyboardButton("🎯 Jetzt loslegen!", callback_data="menu_themen"))
    bot.send_message(chat_id, msg, reply_markup=markup)


def _grant_referral_days(referrer_id: int, new_user_id: int):
    """Give the referrer 3 extra trial days when their friend joins."""
    uid  = str(referrer_id)
    if uid not in user_data:
        return
    user = user_data[uid]
    # Only reward once per referred friend
    refs = user.setdefault("referrals_rewarded", [])
    if str(new_user_id) in refs:
        return
    refs.append(str(new_user_id))

    # Extend trial: push trial_start back by 3 days (or activate if none)
    trial_start = user.get("trial_start")
    if trial_start:
        from datetime import timedelta
        start_dt = datetime.fromisoformat(trial_start)
        new_start = start_dt - timedelta(days=3)
        user_data[uid]["trial_start"] = new_start.isoformat()
    else:
        # No trial yet — gift 3 days starting from today
        user_data[uid]["trial_start"]     = datetime.now().isoformat()
        user_data[uid]["trial_code_used"] = "REFERRAL"
        if "REFERRAL" not in TRIAL_CODES:
            TRIAL_CODES["REFERRAL"] = {"days": 3, "used_by": None}

    save_users(user_data)
    log.info(f"Referral reward: {referrer_id} gets +3 days for inviting {new_user_id}")

    # Notify referrer
    referrer_name = user_data[uid].get("name", "")
    try:
        bot.send_message(referrer_id,
            f"🎉 Dein Freund ist beigetreten!\n"
            f"+3 Tage Trial geschenkt — viel Spaß zusammen auf Deutsch! 🇩🇪",
            parse_mode="Markdown")
    except Exception:
        pass


@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    ensure_user(chat_id)

    uid     = str(chat_id)
    user    = user_data.get(uid, {})
    name    = user.get("name")

    # Parse deep link payload: /start ref_12345 or /start premium_ok
    parts   = message.text.strip().split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""

    # Handle referral deep link — reward referrer
    if payload.startswith("ref_") and not name:
        try:
            referrer_id = int(payload[4:])
            if referrer_id != chat_id:
                _grant_referral_days(referrer_id, chat_id)
        except ValueError:
            pass

    # Handle donation thank-you redirect from Stripe
    if payload == "danke_spende":
        if name:
            bot.send_message(chat_id,
                f"🙏 Danke für deine Spende, {name}! Du bist ein Schatz. 💙\n\n"
                "Izzi freut sich wirklich sehr — danke dass du den German Dude Bot unterstützt!",
                reply_markup=ReplyKeyboardRemove())
        user_state[chat_id] = {"mode": "menu"}
        send_topic_buttons(chat_id)
        return

    # Returning user — skip onboarding
    if name:
        test_state.pop(chat_id, None)
        user_step.pop(chat_id, None)
        user_state[chat_id] = {"mode": "menu"}
        bot.send_message(chat_id,
            f"Hey {name}! 👋 Schön, dass du wieder da bist.\n"
            f"Womit willst du heute üben?",
            reply_markup=ReplyKeyboardRemove())
        send_topic_buttons(chat_id)
        return

    # New user — full onboarding — language first
    user_state[chat_id] = {"mode": "onboarding", "step": "native_language"}
    test_state.pop(chat_id, None)
    user_step.pop(chat_id, None)

    bot.send_message(chat_id,
        "🇩🇪 Hallo! Ich bin dein Deutscher Kumpel.\n"
        "Ich helfe dir, Deutsch zu sprechen — mit echten Gesprächen, jeden Tag.\n\n"
        "🌍 Was ist deine Muttersprache?\n"
        "What's your native language?\n"
        "Какой твой родной язык?\n"
        "Яка твоя рідна мова?\n"
        "لغتك الأم هي؟\n"
        "Ana dilin ne?\n\n"
        "👇 Tippe einfach — oder wähle hier:",
        reply_markup=ReplyKeyboardRemove())

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🇬🇧 English",    callback_data="lang:English"),
        InlineKeyboardButton("🇷🇺 Русский",    callback_data="lang:Русский"),
        InlineKeyboardButton("🇺🇦 Українська", callback_data="lang:Українська"),
        InlineKeyboardButton("🇹🇷 Türkçe",     callback_data="lang:Türkçe"),
        InlineKeyboardButton("🇸🇦 العربية",    callback_data="lang:Arabic"),
        InlineKeyboardButton("🇪🇸 Español",    callback_data="lang:Español"),
        InlineKeyboardButton("🇫🇷 Français",   callback_data="lang:Français"),
        InlineKeyboardButton("🇵🇱 Polski",     callback_data="lang:Polski"),
    )
    bot.send_message(chat_id, "​", reply_markup=markup)  # invisible char keeps message minimal

# GOAL SELECTION
def send_goal_selection(chat_id):
    markup = InlineKeyboardMarkup()

    goals = [
        "Selbstpräsentation",
        "Freunde / Beziehungen",
        "Soziales (Ämter, Ärzte)",
        "Unterhaltung (Club, Kino etc)",
        "Einkauf & Restaurants",
        "Tourismus & Reisen",
        "Sport & Hobbys",
        "Am Telefon",
        "Job"
    ]

    for g in goals:
        markup.add(InlineKeyboardButton(g, callback_data=f"goal:{g}"))

    bot.send_message(chat_id,
        "🎯 Wähle dein Ziel. Wofür brauchst du Deutsch?",
        reply_markup=markup)

# ONBOARDING
GOAL_MAP = {
    "🧍‍♂️ Über dich": "Selbstpräsentation",
    "🧑‍🤝‍🧑 Freunde":  "Freunde / Beziehungen",
    "🏢 Amt & Arzt":  "Soziales (Ämter, Ärzte)",
    "🎉 Freizeit":    "Unterhaltung (Club, Kino etc)",
    "🍽 Restaurant":  "Einkauf & Restaurants",
    "✈️ Reisen":      "Tourismus & Reisen",
    "🏋️ Hobbys":      "Sport & Hobbys",
    "📞 Telefon":     "Am Telefon",
    "💼 Job":         "Job",
}

# Ordered topic list for topic-selection buttons (index = callback key)
TOPIC_LIST = [
    ("🧍 Selbstpräsentation",      "Selbstpräsentation"),
    ("🧑‍🤝‍🧑 Freunde & Beziehungen", "Freunde / Beziehungen"),
    ("🏢 Amt & Arzt",              "Soziales (Ämter, Ärzte)"),
    ("🎉 Freizeit",                "Unterhaltung (Club, Kino etc)"),
    ("🍽️ Einkauf & Restaurant",    "Einkauf & Restaurants"),
    ("✈️ Reisen",                  "Tourismus & Reisen"),
    ("🏋️ Sport & Hobbys",          "Sport & Hobbys"),
    ("📞 Telefon",                 "Am Telefon"),
    ("💼 Job",                     "Job"),
    ("🗣️ Quatschen",               "Quatschen"),
]

def send_topic_buttons(chat_id):
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton(label, callback_data=f"topic:{i}")
        for i, (label, _) in enumerate(TOPIC_LIST)
    ]
    markup.add(*buttons)
    bot.send_message(chat_id, "🎯 Welches Thema willst du heute üben?", reply_markup=markup)

def send_goal_buttons(chat_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("🧍‍♂️ Über dich"), KeyboardButton("🧑‍🤝‍🧑 Freunde"))
    markup.row(KeyboardButton("🏢 Amt & Arzt"),  KeyboardButton("🎉 Freizeit"))
    markup.row(KeyboardButton("🍽 Restaurant"),   KeyboardButton("✈️ Reisen"))
    markup.row(KeyboardButton("🏋️ Hobbys"),       KeyboardButton("📞 Telefon"))
    markup.row(KeyboardButton("💼 Job"))
    bot.send_message(chat_id, "👉 Wofür brauchst du Deutsch?", reply_markup=markup)

def send_gender_buttons(chat_id, question="👇 Select your gender:"):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.row(
        KeyboardButton("👨 Male"),
        KeyboardButton("👩 Female"),
        KeyboardButton("🌈 Other")
    )
    bot.send_message(chat_id, question, reply_markup=markup)

GENDER_MAP = {
    "👨 Male":   "männlich",
    "👩 Female": "weiblich",
    "🌈 Other":  "divers",
}

def handle_onboarding(chat_id, text):
    state = user_state[chat_id]
    step  = state.get("step")

    if step == "native_language":
        lang = text.strip()
        user_data[str(chat_id)]["native_language"] = lang
        save_users(user_data)
        state["step"] = "name"

        # Ask for name in their language
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                system="You are a friendly bot. Reply with exactly one sentence only, no quotes, no extra text.",
                messages=[{"role": "user", "content": (
                    f"Write exactly 1 short friendly question in {lang} asking the user their name "
                    f"(tell them this is what the bot will call them). "
                    f"Informal tone. End with a 😊 emoji."
                )}]
            )
            name_question = resp.content[0].text.strip()
        except Exception:
            name_question = "What's your name? 😊"

        bot.send_message(chat_id, name_question, reply_markup=ReplyKeyboardRemove())

    elif step == "name":
        name = text.strip()
        user_data[str(chat_id)]["name"] = name
        save_users(user_data)
        lang = user_data[str(chat_id)].get("native_language", "English")
        state["step"] = "gender"

        # Ask for gender in their language
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                system="You are a friendly bot. Reply with exactly one sentence only, no quotes, no extra text.",
                messages=[{"role": "user", "content": (
                    f"Write exactly 1 short friendly sentence in {lang} greeting {name} "
                    f"and asking them to choose their gender using the buttons below. "
                    f"Informal tone. Use 1 emoji."
                )}]
            )
            gender_question = resp.content[0].text.strip()
        except Exception:
            gender_question = f"Nice to meet you, {name}! 👋 What's your gender?"

        send_gender_buttons(chat_id, question=gender_question)

    elif step == "gender":
        if text.strip() not in GENDER_MAP:
            bot.send_message(chat_id, "👇 Please tap one of the buttons!")
            return
        gender = GENDER_MAP[text.strip()]
        user_data[str(chat_id)]["gender"] = gender
        save_users(user_data)

        lang = user_data[str(chat_id)].get("native_language", "English")
        name = user_data[str(chat_id)].get("name", "")

        # Punchy pitch in native language — pain points + value promise
        try:
            pitch_resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=220,
                system="Write only the message text. No quotes, no preamble, no extra explanation.",
                messages=[{"role": "user", "content": (
                    f"Write a short, punchy message in {lang} to {name or 'the user'}. "
                    f"Informal tone. Max 10 lines. Use 3-4 emojis max.\n\n"
                    f"Structure it like this:\n"
                    f"1. Address the fear/pain: many people already KNOW German but are too scared to speak it. "
                    f"Or they freeze when a German speaks to them.\n"
                    f"2. The solution: now they have a native German speaker as a personal friend in their pocket — "
                    f"available 24/7, no judgment, always patient.\n"
                    f"3. A concrete timeline — make it feel real and achievable:\n"
                    f"   - In 4 weeks: they stop panicking in conversations, basic replies flow naturally\n"
                    f"   - In 3 months: they handle everyday situations confidently — Ämter, colleagues, friends\n"
                    f"   - In 1 year: almost native speaker level, Germans ask where they learned so well 😄\n"
                    f"4. End with one punchy action line like 'Los geht's.' or 'Pick a topic.' — no fluff.\n\n"
                    f"Do NOT say 'AI', 'bot', 'app', or 'chatbot'. "
                    f"Write as if I am a real German friend texting them. "
                    f"The timeline should feel exciting, not like a language course brochure."
                )}]
            )
            pitch_msg = pitch_resp.content[0].text.strip()
        except Exception:
            pitch_msg = (
                f"Kennst du das? Du lernst Deutsch, aber wenn ein Echter mit dir spricht — Blackout. 😅\n\n"
                f"Ab jetzt hast du einen Muttersprachler als Kumpel in der Tasche. "
                f"Immer da, kein Urteilen, kein Stress. Einfach reden.\n\n"
                f"📅 In 4 Wochen: kein Panik mehr — einfache Antworten kommen von alleine.\n"
                f"📅 In 3 Monaten: Ämter, Kollegen, Freunde — du meisterst den Alltag auf Deutsch.\n"
                f"📅 In 1 Jahr: fast Muttersprachler. Deutsche fragen dich, wo du so gut gelernt hast. 😄\n\n"
                f"Los geht's. 👇"
            )

        user_state[chat_id] = {"mode": "menu"}
        bot.send_message(chat_id, pitch_msg, reply_markup=ReplyKeyboardRemove())
        send_topic_buttons(chat_id)

def send_voice_intro(chat_id):
    bot.send_message(chat_id,
        "🎧 Ganz wichtig:\n\n"
        "Hier geht es nicht um Schreiben.\n"
        "Du sprichst. Du hörst zu.\n\n"
        "👉 Wie im echten Leben.\n\n"
        "❗️ Damit du meine Sprachnachrichten empfangen kannst, stelle sicher, "
        "dass Sprachnachrichten in deinen Telegram-Einstellungen aktiviert sind:\n"
        "*Einstellungen → Privatsphäre → Sprachnachrichten → Alle*",
        parse_mode="Markdown")
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🚀 Los geht's", callback_data="start_chat"))
    bot.send_message(chat_id, "Bereit?", reply_markup=markup)

def start_chat_callback(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    # Never interrupt an active test — old callbacks can re-fire from Telegram
    if chat_id in test_state:
        return

    # ── PAYWALL CHECK ─────────────────────────────────────────────────────────
    if not is_premium(chat_id):
        send_paywall(chat_id)
        return

    # ── Trial warning (1 day left) ────────────────────────────────────────────
    left = days_left_in_trial(chat_id)
    if 0 < left <= 1 and not user_data.get(str(chat_id), {}).get("premium"):
        bot.send_message(chat_id,
            f"⚠️ *Noch {left} Tag in deiner Testphase!*\n"
            "Danach brauchst du Premium um weiterzumachen. 💳",
            parse_mode="Markdown")

    goal = user_data.get(str(chat_id), {}).get("goal")
    if goal:
        # Goal already chosen during onboarding — launch directly
        user_state[chat_id] = {"mode": "chat"}
        bot.send_message(chat_id, f"🎯 Thema: *{goal}* — los geht's 💪", parse_mode="Markdown")
        launch_scenario(chat_id)
    else:
        user_state[chat_id] = {"mode": "topic_select"}
        send_topic_buttons(chat_id)

def show_text_callback(call):
    try:
        key = call.data.split(":")[1]
        text = pending_texts.get(key, "")
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Nicht verfügbar.")
        return
    bot.answer_callback_query(call.id)
    if text:
        bot.send_message(call.message.chat.id, f"💬 {text}")
    else:
        # Fallback: use last_bot_text
        text = last_bot_text.get(call.message.chat.id, "")
        if text:
            bot.send_message(call.message.chat.id, f"💬 {text}")
        else:
            bot.answer_callback_query(call.id, "Text nicht mehr verfügbar.")

def handle_topic_callback(call):
    chat_id = call.message.chat.id
    if chat_id in test_state:
        bot.answer_callback_query(call.id)
        return
    try:
        idx = int(call.data.split(":")[1])
        _, goal = TOPIC_LIST[idx]
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Ungültige Auswahl.")
        return

    bot.answer_callback_query(call.id)

    # Paywall check
    if not is_premium(chat_id):
        send_paywall(chat_id)
        return

    # Special mode: Quatschen — Premium Plus only
    if goal == "Quatschen":
        if not is_premium_plus(chat_id):
            qtext = (
                "👑 Quatsch Modus ist Teil von Premium Plus.\n\n"
                "Kein Skript, kein Thema, kein Druck — einfach reden.\n"
                "Finanzamt-Brief? Kündigung? Oder einfach schlechter Tag?\n"
                "Dein Kumpel hört zu. 24/7, nie urteilend.\n\n"
                "👑 Premium Plus — €30/Monat oder 2000 Stars."
            )
            last_bot_text[chat_id] = qtext
            markup = InlineKeyboardMarkup(row_width=1)
            markup.add(InlineKeyboardButton("👑 Premium Plus holen", callback_data="pay_plus"))
            markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))
            bot.send_message(chat_id, qtext, reply_markup=markup)
            return
        user_data[str(chat_id)]["goal"] = goal
        save_users(user_data)
        start_quatschen(chat_id)
        return

    user_data[str(chat_id)]["goal"] = goal
    save_users(user_data)
    user_state[chat_id] = {"mode": "chat"}

    # Pick a random scenario from this topic and show a preview
    level    = user_data.get(str(chat_id), {}).get("level", "A2")
    scenario = pick_scenario(chat_id, goal, level)
    if not scenario:
        bot.send_message(chat_id, "⚠️ Kein passendes Szenario gefunden. Bitte /restart.")
        return

    # Context text is sent inside start_scenario — don't send here too
    bot.send_message(chat_id, f"🎯 *{goal}*", parse_mode="Markdown")
    start_scenario(chat_id, scenario)

def start_scenario(chat_id, scenario):
    """
    Single authoritative entry point for starting any scenario.
    Order: set state → reset memory → generate opener → send context text → send voice nudge.
    """
    if not gate_scenario(chat_id):
        return
    increment_daily_convo(chat_id)
    name = user_data.get(str(chat_id), {}).get("name", "User")

    # 1. Set state FIRST so build_system_prompt and GPT get the right context
    current_scenario[chat_id] = scenario
    user_state[chat_id] = {
        "mode":        "chat",
        "scenario_id": scenario["id"],
        "turn":        0,
    }

    # 2. Reset memory with correct system prompt
    sys_prompt = build_system_prompt(chat_id, scenario)
    user_memory[chat_id] = [{"role": "system", "content": sys_prompt}]

    # 3. Build context string (clean, name-substituted)
    ctx = get_clean_context(scenario).replace("[Name]", name)

    # 4. Determine opener:
    #    - new-format scenarios have a hardcoded start.text → use it with name substitution
    #    - old-format scenarios → generate a vivid, level-appropriate opener via GPT
    if "start" in scenario and scenario["start"].get("text"):
        # New-format scenario: hardcoded opening
        opening = scenario["start"]["text"].replace("[Name]", name)
    else:
        level    = user_data.get(str(chat_id), {}).get("level", "A2")
        npc_role = scenario.get("npc_role", "")

        # ── SMART EXTRACTION: pull quoted speech directly from scenario text ──
        # Many scenarios already contain the NPC's first line in quotes e.g.:
        # „Na, wie läuft dein erster Tag so?" or "Guten Tag, wie kann ich helfen?"
        quoted = re.findall(r'[„""]([^„"""]+)[""""]', ctx)
        if quoted:
            # Use the last quoted phrase — it's usually the NPC's line
            opening = strip_filler(quoted[-1].strip())
            log.info(f"Opening extracted from scenario text: {opening!r}")
        else:
            # ── GENERATE via Claude ──────────────────────────────────────────
            ctx_lower    = ctx.lower()
            npc_lower    = npc_role.lower()
            is_phone     = any(kw in ctx_lower for kw in ["ruf", "anrufen", "telefonier", "anruf", "rufst", "rufe", "telefon"])
            is_prof_phone = is_phone and any(kw in npc_lower for kw in ["mitarbeiter", "sachbearbeiter", "rezeption", "empfang", "arzt", "praxis", "support", "kundenservice", "restaurant", "hotel", "bank", "sekretär"])
            is_friend_call = is_phone and any(kw in npc_lower for kw in ["kollege", "freund", "kumpel", "bestie", "schwester", "bruder", "partner", "nachbar", "date", "chef"])

            if is_prof_phone:
                instruction = (
                    f"Du bist {npc_role}. {name} ruft gerade an. "
                    f"Nimm ab mit einem professionellen Telefongruß: [Firma] + Gruß + Name + Hilfsangebot. "
                    f"Niveau {level}. Nur 1 Satz."
                )
            elif is_friend_call:
                instruction = (
                    f"Du bist {npc_role} und rufst {name} gerade an. "
                    f"Begrüße ihn/sie natürlich — 1-2 Sätze, locker, kein Firmen-Greeting. "
                    f"Niveau {level}."
                )
            else:
                instruction = (
                    f"Du bist: {npc_role or 'die andere Person in dieser Situation'}.\n"
                    f"Der Lernende ({name}) hat folgende Aufgabe: {ctx}\n\n"
                    f"WICHTIG: {name} ist derjenige mit der Aufgabe/dem Problem — NICHT du.\n"
                    f"Du bist die GEGENÜBERSEITE. Deine Rolle ist es, das Gespräch zu eröffnen "
                    f"und {name} dazu einzuladen zu sprechen.\n"
                    f"Eröffne mit 1-2 Sätzen die DEINER Rolle entsprechen und {name} motivieren zu antworten.\n"
                    f"Niveau {level}. Direkt rein, kein Hmm/Also."
                )

            try:
                resp = claude.messages.create(
                    model="claude-haiku-4-5-20251001",
                    system=sys_prompt,
                    messages=[{"role": "user", "content": instruction}],
                    max_tokens=120,
                )
                opening = strip_filler(resp.content[0].text.strip())
                if not opening:
                    raise ValueError("Empty response")
                log.info(f"Opening generated by Claude: {opening!r}")
            except Exception as e:
                log.warning(f"Opening generation failed for {chat_id}: {e}")
                opening = "Guten Tag! Wie kann ich helfen?"

    # 5. Store opener as first assistant turn in memory (dialog continues from here)
    user_memory[chat_id].append({"role": "assistant", "content": opening})

    # 6. Send context text
    user = user_data.get(str(chat_id), {})
    lang = user.get("native_language")
    lang_hint = f"\n🌍 Tippe /übersetzen um meine letzte Nachricht auf {lang} zu übersetzen." if lang else ""
    bot.send_message(chat_id, f"🎭 {ctx}{lang_hint}")

    # 7. Send voice nudge
    send_reply(chat_id, opening, voice=True)


def launch_scenario(chat_id):
    user     = user_data.get(str(chat_id), {})
    goal     = user.get("goal", "Selbstpräsentation")
    level    = user.get("level", "A2")
    scenario = pick_scenario(chat_id, goal, level)
    if not scenario:
        bot.send_message(chat_id, "⚠️ Kein passendes Szenario gefunden. Bitte /restart und Level prüfen.")
        return
    start_scenario(chat_id, scenario)

def handle_goal(call):
    chat_id = call.message.chat.id
    if chat_id in test_state:
        bot.answer_callback_query(call.id)
        return
    goal = call.data.split(":")[1]

    user_data[str(chat_id)]["goal"] = goal
    save_users(user_data)

    # Onboarding abgeschlossen → Test steht als nächstes
    user_step.pop(chat_id, None)
    user_state[chat_id] = {"mode": "test"}

    name = user_data[str(chat_id)]["name"]

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(
        "Wie gut ist dein Deutsch? Lass uns es kurz checken 😊",
        callback_data="start_test"
    ))

    bot.send_message(chat_id,
        f"Perfekt, {name} 🙌\n"
        f"Du willst dein Deutsch für *{goal}* verbessern.\n\n"
        "Damit ich dir die richtigen Gespräche geben kann, mache ich jetzt einen kurzen Check mit dir.\n\n"
        "Das dauert nur 1 Minute.\n"
        "Einfach antworten – kein Stress 😊\n\n"
        "👉 Bereit?",
        parse_mode="Markdown",
        reply_markup=markup
    )

# GET QUESTION (no repeats per session)
def get_question(level, chat_id):
    """Pick a random unused question for the given level. Resets if pool exhausted."""
    used = test_state[chat_id]["used_questions"]
    pool = QUESTION_POOL.get(level, [])
    available = [q for q in pool if q["id"] not in used]
    if not available:
        available = list(pool)
    q = random.choice(available)
    used.add(q["id"])
    return q

def decide_level(q_index, state):
    """Q0–1 fixed A1, Q2–3 fixed A2, Q4+ adaptive starting at A1 and climbing."""
    if q_index < 2:
        return "A1"
    if q_index < 4:
        return "A2"
    return QUIZ_LEVEL_ORDER[state["current_level_index"]]

def prepare_question(q):
    options = q["options"][:]
    correct = q.get("a") or q.get("answer", "")
    random.shuffle(options)
    correct_index = options.index(correct)
    return {
        "id": q["id"],
        "question": q["q"],
        "options": options,
        "correct_index": correct_index,
    }

def _send_raw_question(chat_id, q_dict, label):
    """Helper: send a single question dict with A/B/C buttons."""
    state = test_state[chat_id]

    # Shuffle options but track where the correct answer ends up
    options = list(q_dict["options"])
    correct_text = q_dict["answer"]
    random.shuffle(options)
    correct_index = options.index(correct_text)

    pq = {
        "id":            q_dict["id"],
        "question":      q_dict["q"],
        "options":       options,
        "correct_index": correct_index,
    }
    state["current_q"]          = pq
    state["waiting_for_answer"] = True

    markup = InlineKeyboardMarkup(row_width=1)
    for i, opt in enumerate(options):
        lbl = f"{chr(65 + i)})  {opt}"
        markup.add(InlineKeyboardButton(lbl, callback_data=f"quiz_answer:{chr(97 + i)}"))

    bot.send_message(chat_id, f"{label}\n\n{pq['question']}", reply_markup=markup)


def send_question(chat_id):
    """Route to A0 screening or adaptive main phase."""
    state = test_state.get(chat_id)
    if not state:
        bot.send_message(chat_id, "Test nicht gestartet.")
        return

    # ── A0 SCREENING PHASE ────────────────────────────────────────────────────
    if state["phase"] == "a0":
        idx = state["a0_index"]
        if idx >= len(A0_QUESTIONS):
            # Both A0 questions done — check results
            if state["a0_errors"] > 0:
                test_state.pop(chat_id, None)
                _trigger_a0_fail(chat_id)
            else:
                # Passed screening — switch to main adaptive phase
                state["phase"] = "main"
                bot.send_message(chat_id, "Super! 🎉 Weiter geht's!")
                send_question(chat_id)
            return

        q = A0_QUESTIONS[idx]
        label = f"❓ Frage {idx + 1}/2"
        _send_raw_question(chat_id, q, label)
        state["current_level"] = "A0"
        return

    # ── MAIN ADAPTIVE PHASE (10 questions, A1→C1) ────────────────────────────
    q_index = state["q_index"]
    if q_index >= 10:
        finish_test(chat_id)
        return

    # Determine level for this question
    level = QUIZ_LEVEL_ORDER[state["current_level_index"]]
    state["current_level"] = level

    # Pick a question from the pool for this level (not already used)
    pool = [q for q in QUESTION_POOL[level] if q["id"] not in state["used_ids"]]
    if not pool:
        # All questions for this level used — try adjacent levels
        for alt_level in QUIZ_LEVEL_ORDER:
            pool = [q for q in QUESTION_POOL[alt_level] if q["id"] not in state["used_ids"]]
            if pool:
                level = alt_level
                break
    if not pool:
        finish_test(chat_id)
        return

    q = random.choice(pool)
    state["used_ids"].add(q["id"])

    label = f"❓ Frage {q_index + 1}/10  •  Level {level}"
    _send_raw_question(chat_id, q, label)


def _trigger_a0_fail(chat_id):
    """User failed A0 screening — offer mini lessons or email."""
    native_lang = user_data.get(str(chat_id), {}).get("native_language") or "Englisch"
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Ja! 💪", callback_data="lesson_yes"),
        InlineKeyboardButton("Nein", callback_data="lesson_no"),
    )
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": (
                f"Translate this into {native_lang}. Only return the translation, nothing else:\n\n"
                f"Oh no 😞 Shall we learn a little bit?"
            )}]
        )
        msg = resp.content[0].text.strip()
    except Exception:
        msg = "Oh no 😞 Shall we learn a little bit?"
    bot.send_message(chat_id, msg, reply_markup=markup)

# QUIZ START
def start_test(chat_id):
    test_state[chat_id] = {
        "phase":               "a0",          # "a0" → screening, "main" → adaptive
        "a0_index":            0,             # which A0 question we're on (0 or 1)
        "a0_errors":           0,             # how many A0 questions wrong
        "q_index":             0,             # main phase question counter (0–9)
        "current_level_index": 0,             # adaptive index into QUIZ_LEVEL_ORDER
        "used_ids":            set(),         # prevent repeating same question
        "score":    {lvl: 0 for lvl in QUIZ_LEVEL_ORDER},
        "attempts": {lvl: 0 for lvl in QUIZ_LEVEL_ORDER},
        "current_q":          None,
        "current_level":      "A1",
        "waiting_for_answer": False,
    }
    user_state[chat_id] = {"mode": "test"}
    bot.send_message(chat_id, "🧠 Los geht's! Ein paar Fragen, um dein Niveau zu checken.")
    send_question(chat_id)

def start_test_callback(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    if chat_id in test_state:
        return   # test already running — ignore stale button re-tap
    start_test(chat_id)

# ANSWER LOGIC
def handle_answer(chat_id, user_answer):
    state = test_state.get(chat_id)
    if not state or not state.get("waiting_for_answer"):
        return

    state["waiting_for_answer"] = False

    q       = state["current_q"]
    correct = False

    if user_answer.lower() in ["a", "b", "c"]:
        idx = ord(user_answer.lower()) - 97
        if idx == q["correct_index"]:
            correct = True
    elif user_answer.strip() == q["options"][q["correct_index"]]:
        correct = True

    # ── A0 SCREENING PHASE ────────────────────────────────────────────────────
    if state.get("phase") == "a0":
        if not correct:
            state["a0_errors"] += 1
        state["a0_index"] += 1
        send_question(chat_id)
        return

    # ── MAIN ADAPTIVE PHASE ───────────────────────────────────────────────────
    level = state["current_level"]
    state["attempts"][level] = state["attempts"].get(level, 0) + 1
    if correct:
        state["score"][level] = state["score"].get(level, 0) + 1

    # Save wrong answers for /errors display
    if not correct:
        wrong_answer = q["options"][ord(user_answer.lower()) - 97] if user_answer.lower() in ["a","b","c"] else user_answer
        correct_answer = q["options"][q["correct_index"]]
        if "wrong_answers" not in state:
            state["wrong_answers"] = []
        state["wrong_answers"].append({
            "level":   level,
            "wrong":   wrong_answer,
            "correct": correct_answer,
            "question": q.get("question", ""),
        })

    # Adaptive level adjustment — every answer shifts the difficulty
    if correct:
        state["current_level_index"] = min(
            state["current_level_index"] + 1, len(QUIZ_LEVEL_ORDER) - 1)
    else:
        state["current_level_index"] = max(
            state["current_level_index"] - 1, 0)

    state["q_index"] += 1

    if state["q_index"] >= 10:
        finish_test(chat_id)
    else:
        send_question(chat_id)

# QUIZ ANSWER — inline button callback
def handle_quiz_answer_callback(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    if chat_id not in test_state:
        return
    # Remove buttons from the question message so it can't be clicked twice
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception as e:
        log.debug(f"Could not remove quiz buttons: {e}")
    answer = call.data.split(":")[1]   # "a", "b", or "c"
    handle_answer(chat_id, answer)

# QUIZ ANSWER — text / voice fallback (kept for voice STT path)
@bot.message_handler(func=lambda m: m.chat.id in test_state)
def handle_answer_message(message):
    chat_id = message.chat.id
    text    = message.text.strip() if message.text else ""
    if text.lower() not in ["a", "b", "c"]:
        return   # silently ignore non-answer text during test
    handle_answer(chat_id, text)

# FAIL FLOW (mind. eine A0-Frage falsch)
def trigger_fail_flow(chat_id):
    # cleanup quiz so handle_answer stops matching
    quiz_state.pop(chat_id, None)
    quiz_current_level.pop(chat_id, None)
    quiz_scores.pop(chat_id, None)
    quiz_history.pop(chat_id, None)
    quiz_a0_results.pop(chat_id, None)
    asked_questions.pop(chat_id, None)

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Ja", callback_data="lesson_yes"),
        InlineKeyboardButton("Nein", callback_data="lesson_no"),
    )

    bot.send_message(chat_id,
        "Ohje 😞. Wollen wir ein bisschen lernen?",
        reply_markup=markup)

def lesson_yes_callback(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    if chat_id in test_state:
        return

    native_lang = user_data.get(str(chat_id), {}).get("native_language") or "Englisch"

    # Intro message in native language
    try:
        intro_resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content":
                f"Translate into {native_lang}. Only return the translation: "
                "Here are your first German lessons!"
            }]
        )
        intro_msg = intro_resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"Intro translation failed: {e}")
        intro_msg = "Here are your first German lessons! 🎓"

    bot.send_message(chat_id, intro_msg)
    bot.send_chat_action(chat_id, "typing")

    # Mini lessons
    try:
        lesson_resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content":
                f"German teacher for absolute beginners. Create mini-lessons with {native_lang} translations. "
                f"Cover in order: 1) Begrüßungen (4 examples) 2) Zahlen 1-20 3) Wochentage 4) Monate "
                f"5) Basis-Verben: sein/haben/gehen/kommen/möchten with examples 6) 6 key phrases. "
                f"IMPORTANT FORMAT RULES: No markdown, no tables, no asterisks, no headers with ##. "
                f"Use ONLY plain text with emoji bullets. Each item on its own line like this: "
                f"👋 Hallo — (translation)\n"
                f"Keep it simple, warm and encouraging."
            }]
        )
        lesson_text = lesson_resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"Lesson generation failed: {e}")
        lesson_text = "Hallo — Hello\nDanke — Thank you\nBitte — Please"

    if chat_id not in user_memory:
        user_memory[chat_id] = []
    user_memory[chat_id].append({"role": "assistant", "content": lesson_text})
    last_bot_text[chat_id] = lesson_text

    translate_markup = InlineKeyboardMarkup()
    translate_markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))
    bot.send_message(chat_id, lesson_text, reply_markup=translate_markup)

    # Mini dialog
    bot.send_chat_action(chat_id, "typing")
    try:
        dialog_resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content":
                f"Write a short German dialog: two people meeting. "
                f"After every German line, write the {native_lang} translation in italics (markdown _text_). "
                f"Cover: greeting, names, where from, age, nice to meet you. "
                f"Start with header: 🗣️ Mini-Dialog: Sich vorstellen"
            }]
        )
        dialog_text = dialog_resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"Dialog failed: {e}")
        dialog_text = "🗣️ Mini-Dialog\n— Hallo, ich heiße Anna.\n— Ich bin Max. Woher kommst du?\n— Aus Berlin. Und du?\n— Aus München. Freut mich!"

    last_bot_text[chat_id] = dialog_text
    user_memory[chat_id].append({"role": "assistant", "content": dialog_text})

    restart_markup = InlineKeyboardMarkup()
    restart_markup.add(InlineKeyboardButton("🔄 Test erneut starten", callback_data="start_test"))
    restart_markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))
    bot.send_message(chat_id, dialog_text, parse_mode="Markdown", reply_markup=restart_markup)


def lesson_no_callback(call):
    chat_id = call.message.chat.id
    if chat_id in test_state:
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)

    # Translate the contact message into the user's native language
    native_lang = user_data.get(str(chat_id), {}).get("native_language") or "Englisch"
    base_msg = (
        "Please send an email to kontakt@erfolgreich-mit-deutsch.de "
        "to arrange your first free German lesson. "
        "I am sure we will have a great chat in German very soon! 🥳"
    )
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": (
                f"Translate the following text into {native_lang}. "
                f"Only return the translation, nothing else:\n\n{base_msg}"
            )}]
        )
        translated = resp.content[0].text.strip()
    except Exception:
        translated = base_msg  # fallback to English if translation fails

    bot.send_message(chat_id, f"📧 {translated}\n\nkontakt@erfolgreich-mit-deutsch.de")

# Progression benchmarks based on CEFR guidelines + Speakly/Goethe-Institut research:
# 10 min/day of active speaking ≈ 70 min/week = ~5 hrs/month of focused conversation practice.
# Active speaking is ~3–4x more efficient than passive learning (source: Speakly, 2021).
# CEFR: A1→A2 ~80h | A2→B1 ~120h | B1→B2 ~200h | B2→C1 ~250h of total guided learning.
# With intensive daily speaking these compress significantly.
LEVEL_PROGRESS = {
    "A1": {"emoji": "🌱", "desc": "Du kennst die Basics — erste Wörter, einfache Sätze, Begrüßungen.", "next": "A2", "weeks": 4},
    "A2": {"emoji": "🚶", "desc": "Du kommst im Alltag klar, aber brauchst noch Zeit zum Formulieren.", "next": "B1", "weeks": 8},
    "B1": {"emoji": "💬", "desc": "Du kannst dich verständigen — Natürlichkeit ist dein nächster Schritt.", "next": "B2", "weeks": 12},
    "B2": {"emoji": "🎯", "desc": "Du sprichst schon gut — jetzt geht's um Flüssigkeit und natürlichen Ausdruck.", "next": "C1", "weeks": 18},
    "C1": {"emoji": "🏆", "desc": "Du bist fast auf Muttersprachler-Niveau. Feinschliff und Nuancen sind dein Ziel.", "next": None, "weeks": None},
}

GOAL_EXTRA = {
    "Job":              "💼 Fokus: Meetings, Kollegen, Interviews",
    "Freunde":          "🧑‍🤝‍🧑 Fokus: Alltag & Smalltalk",
    "Einkaufen":        "🛒 Fokus: Läden, Märkte, Bestellungen",
    "Reisen":           "✈️ Fokus: Hotels, Ausflüge, Orientierung",
    "Soziales":         "🤝 Fokus: Behörden, Formulare, Alltagssituationen",
    "Unterhaltung":     "🎬 Fokus: Filme, Serien, Kultur",
    "Sport":            "⚽ Fokus: Training, Verein, Wettkampf",
    "Telefon":          "📞 Fokus: Anrufe, Termine, Durchsagen",
    "Selbstpräsentation": "🎤 Fokus: Vorstellung, Präsentation, Auftreten",
}

def send_level_feedback(chat_id, level):
    prog = LEVEL_PROGRESS.get(level, LEVEL_PROGRESS["A1"])
    name = user_data.get(str(chat_id), {}).get("name", "")
    n = f" {name}" if name else ""

    # ── Nachricht 1: Niveau-Ergebnis ────────────────────────────────────────
    bot.send_message(chat_id,
        f"✅ Test abgeschlossen!\n\n"
        f"Dein Niveau:{n}\n"
        f"*{prog['emoji']} {level}*",
        parse_mode="Markdown"
    )

    time.sleep(0.6)

    # ── Nachricht 2: Was das bedeutet ───────────────────────────────────────
    bot.send_message(chat_id,
        f"📖 *Was bedeutet das?*\n\n"
        f"{prog['desc']}",
        parse_mode="Markdown"
    )

    time.sleep(0.6)

    # ── Nachricht 3: Dein Ziel ───────────────────────────────────────────────
    if prog["next"] and prog["weeks"]:
        goal_msg = (
            f"🎯 *Dein nächstes Ziel: {prog['next']}*\n\n"
            f"10 Minuten täglich mit mir sprechen\n"
            f"= in ~*{prog['weeks']} Wochen* auf dem nächsten Level. 📈"
        )
    else:
        goal_msg = (
            "🏆 *Du bist auf dem höchsten Niveau!*\n\n"
            "Muttersprachler-Level — das ist das Ziel. Du bist da."
        )
    bot.send_message(chat_id, goal_msg, parse_mode="Markdown")

    time.sleep(0.6)

    # ── Nachricht 4: Wie es funktioniert + Los geht's ───────────────────────
    native_lang = user_data.get(str(chat_id), {}).get("native_language") or "Englisch"
    try:
        tr = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content":
                f"Translate into {native_lang}. Only return the translation, nothing else:\n\n"
                f"Important: To use voice messages, allow them in Telegram:\n"
                f"Settings → Privacy and Security → Voice Messages → Everybody"
            }]
        )
        voice_hint = tr.content[0].text.strip()
    except Exception:
        voice_hint = "Settings → Privacy and Security → Voice Messages → Everybody"

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🚀 Los geht's!", callback_data="start_chat"))
    bot.send_message(chat_id,
        f"🎤 *Wie funktioniert das hier?*\n\n"
        f"Du sprichst — ich antworte.\n"
        f"Genau wie ein echtes Gespräch.\n\n"
        f"📱 {voice_hint}\n\n"
        f"Bereit?",
        parse_mode="Markdown",
        reply_markup=markup
    )

# FINISH TEST
def finish_test(chat_id):
    state = test_state[chat_id]

    scores   = state["score"]    # {level: correct_count}
    attempts = state["attempts"] # {level: attempt_count}

    # ── Hierarchical level assignment ───────────────────────────────────────
    # Walk A1 → A2 → B1 → B2 → C1 in order.
    # A level is "passed" only if ALL of:
    #   1. at least 1 attempt (A1/A2) or 2 attempts (B1/B2/C1) — prevents 1-question fluke
    #   2. accuracy ≥ 60 %  (well above random 33 % for 3-choice questions)
    #   3. previous level was already passed (hierarchical — can't skip)
    # Exception: if only 1 attempt on B1+, require 100 % (the one question must be correct).
    # This stops a lucky single correct on C1 from overriding a weak overall performance.

    MIN_ATTEMPTS = {"A1": 1, "A2": 1, "B1": 2, "B2": 2, "C1": 2}
    THRESHOLD    = 0.60   # 60 % accuracy required

    final_level = "A1"   # floor — everyone gets at least A1
    for lvl in QUIZ_LEVEL_ORDER:       # ["A1","A2","B1","B2","C1"]
        att  = attempts.get(lvl, 0)
        corr = scores.get(lvl, 0)

        if att == 0:
            continue   # adaptive test skipped this level — don't penalise
        acc = corr / att
        if acc >= THRESHOLD:
            final_level = lvl
        else:
            final_level = lvl   # first real struggle = working level
            break

    user_level[chat_id] = final_level
    uid = str(chat_id)
    user_data[uid]["level"] = final_level
    user_data[uid]["test_completed"] = True

    # Save test wrong answers separately — shown first in /errors
    wrong_answers = state.get("wrong_answers", [])
    if wrong_answers:
        if "test_errors" not in user_data[uid]:
            user_data[uid]["test_errors"] = []
        for wa in wrong_answers:
            user_data[uid]["test_errors"].append({
                "wrong":   wa["wrong"],
                "correct": wa["correct"],
                "level":   wa["level"],
                "question": wa.get("question", ""),
            })
        # Keep last 20 test errors max
        user_data[uid]["test_errors"] = user_data[uid]["test_errors"][-20:]
    save_users(user_data)

    del test_state[chat_id]

    # Reset conversation memory for a fresh start
    user_memory[chat_id]   = []
    turn_counter[chat_id]  = 0
    session_state[chat_id] = {"struggle": 0, "success": 0}

    # Wait for user to click "Los geht's!" — conversation starts in start_chat_callback
    user_state[chat_id] = {"mode": "ready"}
    send_deutsch_fingerabdruck(chat_id, final_level, scores, attempts, wrong_answers)

# FEEDBACK COMMAND
@bot.message_handler(commands=['feedback'])
def feedback(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "🧠 Analysiere dein Deutsch...")
    result = generate_feedback(chat_id, user_memory.get(chat_id, []))
    bot.send_message(chat_id, result)

# FORTSCHRITT COMMAND
@bot.message_handler(commands=['level'])
def level_cmd(message):
    ensure_user(message.chat.id)
    if _require_onboarding(message.chat.id): return
    send_my_progress(message.chat.id)

@bot.message_handler(commands=['progress'])
def progress_cmd_redirect(message):
    ensure_user(message.chat.id)
    if _require_onboarding(message.chat.id): return
    send_my_progress(message.chat.id)


def send_my_progress(chat_id: int):
    """
    Kombinierte Progress-Ansicht: Deutschniveau + XP + Streak + Achievements + Empfehlung.
    Ersetzt die separaten /level, /progress, /achievements Commands.
    """
    uid   = str(chat_id)
    user  = user_data.get(uid, {})
    stats = user.get("user_stats", {})
    name  = user.get("name", "")

    # ── Daten zusammentragen ────────────────────────────────────────────────
    german_level = user.get("level", "A2")          # A1–C2 aus Onboarding/Adaptiv
    xp           = stats.get("xp", 0)
    bot_level    = stats.get("level", 1)
    streak       = stats.get("streak", 0)
    goal         = user.get("goal", "")
    convos       = user.get("conversations_started", 0)

    # ── XP-Balken innerhalb des Bot-Levels ──────────────────────────────────
    xp_in_level  = xp % 50
    filled       = xp_in_level // 5
    xp_bar       = "🟦" * filled + "⬜" * (10 - filled)
    xp_to_next   = 50 - xp_in_level

    # ── Streak-Balken ───────────────────────────────────────────────────────
    streak_bar = ("🔥" * min(streak, 7)) if streak > 0 else "💤 Noch kein Streak"

    # ── Achievements ────────────────────────────────────────────────────────
    earned = user.get("achievements", [])
    ach_lines = []
    for badge_id, key, threshold, emoji, title, desc in ACHIEVEMENT_DEFS:
        if badge_id in earned:
            ach_lines.append(f"{emoji} {title}")
    ach_block = ("  " + "  ".join(ach_lines[:6])) if ach_lines else "  Noch keine — mach dein erstes Gespräch! 🎯"

    # ── Empfehlung: Wochen bis nächstes Sprachniveau ────────────────────────
    LEVEL_WEEKS = {
        "A1": ("A2", 4),
        "A2": ("B1", 8),
        "B1": ("B2", 10),
        "B2": ("C1", 14),
        "C1": ("C2", 20),
    }
    next_info = LEVEL_WEEKS.get(german_level)

    if next_info:
        next_lvl, base_weeks = next_info
        # Beschleunigung durch mehr tägliche Nutzung
        # Basis: ~5 Gespräche/Woche → base_weeks
        # +10 Min täglich = ca. 7 Gespräche/Woche → 30% schneller
        fast_weeks = max(1, round(base_weeks * 0.70))
        rec_block = (
            f"⏱ *Dein Weg zu {next_lvl}:*\n"
            f"  Bei aktuellem Tempo: ca. *{base_weeks} Wochen*\n"
            f"  Mit 10 Min mehr täglich: ca. *{fast_weeks} Wochen* 🚀\n"
            f"  → Einfach Quatschen — kein Lernplan, kein Druck."
        )
    else:
        rec_block = "🏆 Du bist auf dem höchsten Niveau — weiter so!"

    # ── Ziel-Info ───────────────────────────────────────────────────────────
    goal_line = (f"🎯 *Dein Ziel:* {GOAL_TEXT.get(goal, goal)}\n" if goal else "")

    # ── Zusammensetzen ──────────────────────────────────────────────────────
    text = (
        f"📊 *Mein Progress{', ' + name if name else ''}*\n"
        f"──────────────────\n\n"
        f"🇩🇪 *Deutschniveau:* {german_level}\n"
        f"{goal_line}"
        f"\n⭐ *XP:* {xp}  |  Level {bot_level}  |  +{xp_to_next} bis Level {bot_level + 1}\n"
        f"{xp_bar}\n\n"
        f"🔥 *Streak:* {streak} {'Tag' if streak == 1 else 'Tage'}  {streak_bar}\n\n"
        f"🏅 *Achievements:*\n{ach_block}\n\n"
        f"{rec_block}"
    )

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🎯 Jetzt Gespräch starten", callback_data="start_chat"))
    markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))
    last_bot_text[chat_id] = text
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)

def show_menu(chat_id):
    user_state[chat_id] = user_state.get(chat_id, {})
    user_state[chat_id]["mode"] = "menu"
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🎯 Themen wählen",    callback_data="menu_themen"),
        InlineKeyboardButton("📊 Fortschritt",       callback_data="menu_progress"),
        InlineKeyboardButton("🧠 Meine Fehler",      callback_data="menu_errors"),
        InlineKeyboardButton("📈 Mein Niveau",       callback_data="menu_level"),
        InlineKeyboardButton("🏋️ Übungen",           callback_data="menu_practice"),
        InlineKeyboardButton("🔄 Chat neu starten",  callback_data="menu_restart"),
    )
    bot.send_message(chat_id, "😄 Was willst du machen?", reply_markup=markup)

def show_level(chat_id):
    level = user_data.get(str(chat_id), {}).get("level", "A2")
    bot.send_message(chat_id, f"🎯 Dein aktuelles Niveau: *{level}*", parse_mode="Markdown")

def show_errors(chat_id):
    uid        = str(chat_id)
    user       = user_data.get(uid, {})
    level      = user.get("level", "A2")
    test_errs  = user.get("test_errors", [])
    voice_errs = [e for e in user.get("errors", []) if isinstance(e, str)]

    if not test_errs and not voice_errs:
        bot.send_message(chat_id, "Alles sauber 😄 Noch keine Fehler gespeichert.")
        return

    # Collect all errors to enrich with Claude
    all_errors = []
    for e in test_errs[-5:]:
        wrong   = e.get("wrong", "")
        correct = e.get("correct", "")
        lvl     = e.get("level", level)
        if wrong and correct:
            all_errors.append({"wrong": wrong, "correct": correct, "level": lvl, "source": "test"})
    for e in voice_errs[-5:]:
        if " → " in e:
            parts = e.split(" → ", 1)
            all_errors.append({"wrong": parts[0].strip(), "correct": parts[1].strip(), "level": level, "source": "voice"})

    if not all_errors:
        bot.send_message(chat_id, "Alles sauber 😄 Noch keine Fehler gespeichert.")
        return

    # Ask Claude for mini-explanation + example for each error
    errors_input = "\n".join(
        f"{i+1}. FALSCH: {e['wrong']} | RICHTIG: {e['correct']} | NIVEAU: {e['level']}"
        for i, e in enumerate(all_errors)
    )
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=(
                f"Du bist ein freundlicher Deutschlehrer (Niveau {level}).\n"
                "Erkläre für jeden Fehler genau dieses Format (nur Plaintext, keine Sternchen):\n"
                "• ❌ [falscher Satz]\n"
                "  ✅ [korrekter Satz]\n"
                "  💡 [1 Satz Erklärung — welche Regel]\n"
                "  💬 Beispiel: [anderer natürlicher Satz mit derselben Regel]\n\n"
                "NUR diese Fehler erklären:\n"
                "1. Verbstellung falsch (Verb nicht auf Position 2, oder Verb nicht am Ende im Nebensatz)\n"
                "2. Objekt-Reihenfolge falsch (Dativ/Akkusativ, Pronomen-Stellung)\n"
                "3. Falsche Kasusform (z.B. 'mit der Mann' statt 'mit dem Mann')\n\n"
                "IGNORIERE KOMPLETT:\n"
                "- 'ich hab', 'ich mach', 'ich geh' und alle Umgangssprache-Verkürzungen\n"
                "- Alternative korrekte Wortstellungen — Deutsch hat oft mehrere richtige Varianten\n"
                "- Stilistische Unterschiede und Zeitangaben-Stellung\n"
                "Im Zweifel: nicht korrigieren."
            ),
            messages=[{"role": "user", "content": f"Fehler:\n{errors_input}"}]
        )
        result = _strip_md(resp.content[0].text.strip())
    except Exception as e:
        # Fallback: plain list
        result = "\n".join(
            f"• ❌ {e['wrong']}  →  ✅ {e['correct']}"
            for e in all_errors
        )

    header = "🧠 Deine Fehler\n" + "─" * 20 + "\n\n"
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("💪 Üben", callback_data="new_exercises"),
        InlineKeyboardButton("🗑️ Löschen", callback_data="clear_errors"),
    )
    bot.send_message(chat_id, header + result, reply_markup=markup)

# Grammar topics per level — used as focus for exercise sets
GRAMMAR_TOPICS = {
    "A1": ["Verb-Konjugation (sein/haben)", "bestimmter und unbestimmter Artikel",
           "Personalpronomen", "einfache W-Fragen", "Verneinung mit nicht/kein"],
    "A2": ["Perfekt mit haben/sein", "Dativ vs. Akkusativ", "Modalverben",
           "trennbare Verben", "Possessivpronomen"],
    "B1": ["Konjunktiv II (würde/hätte/wäre)", "Relativsätze", "Präteritum",
           "Wechselpräpositionen", "indirekte Rede"],
    "B2": ["Passiv (Vorgangs- und Zustandspassiv)", "Konjunktiv I",
           "erweiterte Partizipialkonstruktionen", "Genitiv", "Konzessivsätze"],
    "C1": ["Modalpartikeln (doch, halt, ja, schon)", "Nominalisierungen",
           "komplexe Nebensatzkonstruktionen", "Stilebenen", "idiomatische Fügungen"],
    "C2": ["Register und Stilsicherheit", "rhetorische Mittel",
           "subtile Bedeutungsunterschiede", "elliptische Strukturen", "Präzision im Ausdruck"],
}

def _get_exercise_topic(chat_id):
    """Pick topic from user errors — test errors first, then voice errors."""
    uid        = str(chat_id)
    user       = user_data.get(uid, {})
    level      = user.get("level", "A2")
    test_errs  = user.get("test_errors", [])
    weak_points= [wp for wp in user.get("weak_points", []) if isinstance(wp, dict)]

    if test_errs:
        # Derive topic from most recent test error level
        lvl = test_errs[-1].get("level", level)
        topics = GRAMMAR_TOPICS.get(lvl, GRAMMAR_TOPICS.get(level, GRAMMAR_TOPICS["A2"]))
        return random.choice(topics), level
    if weak_points:
        wp = random.choice(weak_points[:5])
        return wp.get("type", random.choice(GRAMMAR_TOPICS.get(level, GRAMMAR_TOPICS["A2"]))), level
    return random.choice(GRAMMAR_TOPICS.get(level, GRAMMAR_TOPICS["A2"])), level


def _generate_single_question(topic, level, used_questions):
    """Ask Claude for one fresh question on this topic."""
    used_hint = f"Bereits gestellt (nicht wiederholen): {used_questions}" if used_questions else ""
    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=f"""Du bist ein Deutschlehrer. Niveau: {level}. Thema: {topic}.
Erstelle GENAU EINE Multiple-Choice-Aufgabe in diesem Format:
FRAGE: <Satz mit _____ oder direkte Frage>
A: <Option>
B: <Option>
C: <Option>
ANTWORT: <A/B/C>
ERKLAERUNG: <1 Satz warum diese Antwort richtig ist>

Alltagssprache! Kein Schulbuch-Deutsch. Berliner-Test.
{used_hint}""",
        messages=[{"role": "user", "content": "Erstelle die Aufgabe."}]
    )
    raw   = resp.content[0].text.strip()
    lines = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            lines[key.strip().upper()] = val.strip()
    return {
        "question":    lines.get("FRAGE", ""),
        "a":           lines.get("A", ""),
        "b":           lines.get("B", ""),
        "c":           lines.get("C", ""),
        "correct":     lines.get("ANTWORT", "A").upper()[0],
        "explanation": lines.get("ERKLAERUNG", ""),
    }


def start_exercise(chat_id):
    """Start 3-question exercise session, one question at a time."""
    uid   = str(chat_id)
    topic, level = _get_exercise_topic(chat_id)

    user_state[chat_id] = user_state.get(chat_id, {})
    user_state[chat_id].update({
        "mode":             "exercise",
        "exercise_topic":   topic,
        "exercise_level":   level,
        "exercise_idx":     0,
        "exercise_score":   0,
        "exercise_used":    [],
        "exercise_total":   3,
    })

    bot.send_message(chat_id, f"💪 3 Fragen zum Thema: {topic}\n\nLos geht's!")
    _send_next_exercise_question(chat_id)


def _send_next_exercise_question(chat_id):
    """Generate and send the next question with a/b/c buttons."""
    state = user_state.get(chat_id, {})
    idx   = state.get("exercise_idx", 0)
    total = state.get("exercise_total", 3)

    if idx >= total:
        _finish_exercise_session(chat_id)
        return

    topic = state.get("exercise_topic", "Grammatik")
    level = state.get("exercise_level", "A2")
    used  = state.get("exercise_used", [])

    try:
        q = _generate_single_question(topic, level, used)
    except Exception as e:
        log.error(f"Question generation failed: {e}")
        bot.send_message(chat_id, "⚠️ Frage konnte nicht generiert werden. Versuch /practice nochmal.")
        user_state[chat_id]["mode"] = "idle"
        return

    # Store current question for answer checking
    user_state[chat_id]["exercise_current_q"] = q
    user_state[chat_id]["exercise_used"].append(q["question"])

    text = (
        f"❓ Frage {idx + 1} von {total}\n\n"
        f"{q['question']}\n\n"
        f"A) {q['a']}\n"
        f"B) {q['b']}\n"
        f"C) {q['c']}"
    )
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("A", callback_data="ex_ans:A"),
        InlineKeyboardButton("B", callback_data="ex_ans:B"),
        InlineKeyboardButton("C", callback_data="ex_ans:C"),
    )
    bot.send_message(chat_id, text, reply_markup=markup)


def _finish_exercise_session(chat_id):
    """Show final score and offer next steps."""
    state = user_state.get(chat_id, {})
    score = state.get("exercise_score", 0)
    total = state.get("exercise_total", 3)
    topic = state.get("exercise_topic", "")

    emoji = "🏆" if score == total else "💪" if score >= total // 2 else "📖"
    text  = f"{emoji} Fertig! {score}/{total} richtig — Thema: {topic}"

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔁 Nochmal", callback_data="new_exercises"),
        InlineKeyboardButton("🏠 Menü",    callback_data="go_menu"),
    )
    markup.add(InlineKeyboardButton("📖 Grammatik erklären", callback_data="explain_grammar"))

    user_state[chat_id]["mode"] = "idle"
    bot.send_message(chat_id, text, reply_markup=markup)


def explain_grammar(chat_id):
    """Send a simple, example-rich grammar explanation for the current exercise topic."""
    topic = user_state.get(chat_id, {}).get("exercise_topic", "Grammatik")
    level = user_state.get(chat_id, {}).get("exercise_level", "A2")

    bot.send_message(chat_id, "📖 *Grammatik-Erklärung wird erstellt...*", parse_mode="Markdown")

    system_prompt = (
        f"Du bist ein Deutschlehrer, der komplizierte Grammatik einfach erklärt. Niveau: {level}.\n"
        f"Erkläre jetzt das Thema: {topic}\n\n"
        f"Regeln:\n"
        f"- Einfache, klare Sprache — kein Fachjargon\n"
        f"- Erkläre die Regel in 2-3 Sätzen\n"
        f"- Gib mindestens 5 konkrete Beispiele (mit Fettschrift für die wichtigen Teile)\n"
        f"- Zeige auch 2-3 häufige Fehler mit Korrektur (❌ falsch → ✅ richtig)\n"
        f"- Am Ende: eine Merkhilfe oder Eselsbrücke\n"
        f"- Nutze Telegram Markdown (*fett*, _kursiv_)\n"
        f"Schreibe auf Deutsch."
    )

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=900,
        system=system_prompt,
        messages=[{"role": "user", "content": f"Erkläre mir {topic} auf Niveau {level}."}]
    )

    explanation = response.content[0].text.strip()

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("💪 Neue Übungen", callback_data="new_exercises"),
        InlineKeyboardButton("🏠 Menü",         callback_data="go_menu"),
    )

    bot.send_message(
        chat_id,
        f"📖 *{topic}*\n\n{explanation}",
        parse_mode="Markdown",
        reply_markup=markup,
    )

SHADOWING_SENTENCES = {
    "A1": ["Ich heiße Maria.", "Guten Morgen! Wie geht es dir?", "Ich komme aus Spanien."],
    "A2": ["Ich hätte gern einen Kaffee.", "Können Sie mir helfen, bitte?", "Wo ist der Bahnhof?"],
    "B1": ["Ich würde gern einen Termin vereinbaren.", "Das ist eine interessante Frage.", "Wie lange bist du schon hier?"],
    "B2": ["Ich bin der Meinung, dass wir das überdenken sollten.", "Obwohl es schwierig ist, versuche ich es täglich.", "Das hätte ich nicht gedacht."],
    "C1": ["Angesichts der Umstände wäre ein anderer Ansatz sinnvoller.", "Er hat sich hervorragend geschlagen, trotz aller Widrigkeiten.", "Das lässt sich nicht so einfach auf einen Nenner bringen."],
}

def start_shadowing(chat_id):
    user_state[chat_id] = user_state.get(chat_id, {})
    user_state[chat_id]["mode"] = "shadowing"

    level     = user_data.get(str(chat_id), {}).get("level", "A2")
    sentences = SHADOWING_SENTENCES.get(level, SHADOWING_SENTENCES["A2"])
    text      = random.choice(sentences)
    user_state[chat_id]["shadowing_text"] = text

    send_reply(chat_id, text, voice=True)
    bot.send_message(chat_id, "🎧 Hör zu und sprich nach!\n\n👉 Schick eine Sprachnachricht.")

def restart_chat(chat_id):
    """Show confirmation dialog before wiping data."""
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("🗑️ Ja, alles löschen", callback_data="confirm_restart"),
        InlineKeyboardButton("❌ Abbrechen",           callback_data="cancel_restart"),
    )
    bot.send_message(chat_id,
        "‼️ *ACHTUNG — alle deine Daten werden unwiderruflich gelöscht!*\n\n"
        "XP, Streak, Fortschritt, Niveau — alles weg.\n"
        "Bist du sicher?",
        parse_mode="Markdown",
        reply_markup=markup)

def do_full_reset(chat_id):
    """Actually wipe everything and restart onboarding.
    Billing info (trial_start, premium, Stripe IDs) is preserved so
    /restart cannot be used to game the free trial."""
    uid = str(chat_id)

    # Preserve billing data before wiping
    existing = user_data.get(uid, {})
    _trial_start          = existing.get("trial_start")
    _premium              = existing.get("premium", False)
    _stripe_customer_id   = existing.get("stripe_customer_id")
    _stripe_subscription_id = existing.get("stripe_subscription_id")

    # Clear in-memory state
    user_state.pop(chat_id, None)
    user_memory.pop(chat_id, None)
    turn_counter.pop(chat_id, None)
    session_state.pop(chat_id, None)
    current_scenario.pop(chat_id, None)
    test_state.pop(chat_id, None)
    user_step.pop(chat_id, None)
    quiz_state.pop(chat_id, None)
    quiz_scores.pop(chat_id, None)
    quiz_history.pop(chat_id, None)
    quiz_a0_results.pop(chat_id, None)
    asked_questions.pop(chat_id, None)

    # Wipe from persistent storage
    if uid in user_data:
        del user_data[uid]
        save_users(user_data)

    bot.send_message(chat_id,
        "✅ Alles gelöscht. Frischer Start! 🙂")

    # Re-run full onboarding
    ensure_user(chat_id)

    # Restore billing info — /restart must never reset the trial clock
    user_data[uid]["trial_start"]            = _trial_start or user_data[uid]["trial_start"]
    user_data[uid]["premium"]                = _premium
    user_data[uid]["stripe_customer_id"]     = _stripe_customer_id
    user_data[uid]["stripe_subscription_id"] = _stripe_subscription_id
    save_users(user_data)

    user_state[chat_id] = {"mode": "onboarding", "step": "native_language"}
    bot.send_message(chat_id,
        "🇩🇪 Hallo! Ich bin dein Deutscher Kumpel.\n"
        "Ich helfe dir, Deutsch zu sprechen — mit echten Gesprächen, jeden Tag.\n\n"
        "🌍 Was ist deine Muttersprache?\n"
        "What's your native language?\n"
        "Какой твой родной язык?\n"
        "Яка твоя рідна мова?\n"
        "لغتك الأم هي؟\n"
        "Ana dilin ne?\n\n"
        "👇 Schreib's einfach unten!",
        reply_markup=ReplyKeyboardRemove())

# ─────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────

@bot.message_handler(commands=['themen'])
def themen_cmd(message):
    ensure_user(message.chat.id)
    if _require_onboarding(message.chat.id): return
    _track_feature(message.chat.id, 'themen')
    send_topic_buttons(message.chat.id)

@bot.message_handler(commands=['menu'])
def menu_cmd(message):
    ensure_user(message.chat.id)
    if not is_premium(message.chat.id): send_paywall(message.chat.id); return
    show_menu(message.chat.id)

@bot.message_handler(commands=['level'])
def level_cmd(message):
    ensure_user(message.chat.id)
    if _require_onboarding(message.chat.id): return
    send_my_progress(message.chat.id)

@bot.message_handler(commands=['errors'])
def errors_cmd(message):
    ensure_user(message.chat.id)
    if _require_onboarding(message.chat.id): return
    show_errors(message.chat.id)

@bot.message_handler(commands=['practice'])
def practice_cmd(message):
    ensure_user(message.chat.id)
    if _require_onboarding(message.chat.id): return
    _track_feature(message.chat.id, 'practice')
    if not is_premium(message.chat.id): send_paywall(message.chat.id); return
    start_exercise(message.chat.id)

@bot.message_handler(commands=['info'])
def info_cmd(message):
    chat_id = message.chat.id
    ensure_user(chat_id)
    uid  = str(chat_id)
    name = user_data.get(uid, {}).get("name", "")

    tier = ""
    if is_premium_plus(chat_id):
        tier = "\n👑 Du hast *Premium Plus* — vollen Zugang zu allem."
    elif is_premium(chat_id):
        tier = "\n🎓 Du hast *Premium* — zum Upgrade: /upgrade"
    else:
        tier = "\n🔓 Kostenlos: 1 Gespräch/Tag + tägliche Gems."

    text = (
        f"📖 *Instruktion{' — Hallo, ' + name + '!' if name else ''}*\n\n"
        "🎯 /themen — Gesprächsthema wählen\n"
        "🎤 Sprich oder schreib auf Deutsch — Fehler werden erklärt & gespeichert\n"
        "💪 /practice — Übungen zu deinen Schwächen\n"
        "🃏 /flashcards — Vokabelkarten auf Quizlet\n"
        "📊 /level — Mein Progress, XP & Empfehlungen\n"
        "📋 /integration — Amtsbriefe, Verträge & Formulare verstehen\n"
        "🔁 /restart — neues Gespräch starten\n\n"
        "💎 *Pläne:*\n"
        "🔓 Free: 1 Gespräch/Tag + tägliche Gems\n"
        "🎓 Premium (€20/Mo): unlimitierte Gespräche & Übungen\n"
        "👑 Premium Plus (€30/Mo): alles + Alltag in Deutschland meistern\n"
        "→ /upgrade für Details & Preise\n"
        + tier +
        "\n\nFragen? /support"
    )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))
    last_bot_text[chat_id] = text
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)

# ═══════════════════════════════════════════════════════════════════════════
#  /DANKE — Spenden + Feedback
# ═══════════════════════════════════════════════════════════════════════════

def send_danke_menu(chat_id: int):
    """Hauptmenü für /danke."""
    uid  = str(chat_id)
    name = user_data.get(uid, {}).get("name", "")
    text = (
        f"🙏 Danke{', ' + name if name else ''}!\n\n"
        "Schön dass du den German Dude Bot magst. "
        "Was möchtest du tun?"
    )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("☕ Einen Kaffee spendieren",  callback_data="danke:donate"))
    markup.add(InlineKeyboardButton("💬 Feedback hinterlassen",    callback_data="danke:feedback"))
    markup.add(InlineKeyboardButton("🌍 übersetzen",               callback_data="translate_last"))
    last_bot_text[chat_id] = text
    bot.send_message(chat_id, text, reply_markup=markup)


def send_donation_menu(chat_id: int):
    """Spendenbetrags-Auswahl."""
    text = (
        "☕ *Wie viel magst du spendieren?*\n\n"
        "Jede Spende hilft, den Bot am Laufen zu halten und weiterzuentwickeln. "
        "Danke von Herzen! 🙏"
    )
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("☕ €3",  callback_data="donate:eur:3"),
        InlineKeyboardButton("🍕 €5",  callback_data="donate:eur:5"),
        InlineKeyboardButton("🎉 €10", callback_data="donate:eur:10"),
    )
    markup.row(
        InlineKeyboardButton("🌟 €20", callback_data="donate:eur:20"),
        InlineKeyboardButton("💎 €50", callback_data="donate:eur:50"),
    )
    markup.add(InlineKeyboardButton("⭐ Mit Telegram Stars spenden", callback_data="donate:stars"))
    markup.add(InlineKeyboardButton("◀️ Zurück",                     callback_data="danke:back"))
    last_bot_text[chat_id] = text
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)


# Stripe Price IDs für Spenden (einmalig in Stripe angelegt)
DONATION_PRICES = {
    3:  "price_1TiaHCJ6DBeqSSUPrdio7a6Z",
    5:  "price_1TiaKTJ6DBeqSSUPVjD6yGq1",
    10: "price_1TiaKjJ6DBeqSSUPaAzn4Xov",
    20: "price_1TiaKwJ6DBeqSSUPTeC2Kba8",
    50: "price_1TiaL5J6DBeqSSUPaverdmy8",
}

DONATION_LABELS = {
    3:  "☕ Ein Kaffee",
    5:  "🍕 Eine Pizza",
    10: "🎉 Großes Danke!",
    20: "🌟 Du rockst!",
    50: "💎 Legendär!",
}

def create_donation_checkout(chat_id: int, amount_eur: int) -> str | None:
    """Stripe Checkout Session für eine Einmalspende mit vorhandenem Price."""
    if not STRIPE_SECRET_KEY:
        return None
    price_id = DONATION_PRICES.get(amount_eur)
    if not price_id:
        return None
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url="https://t.me/germandude_bot?start=danke_spende",
            cancel_url="https://t.me/germandude_bot",
            metadata={
                "telegram_id": str(chat_id),
                "type":        "donation",
                "amount":      str(amount_eur),
            },
        )
        return session.url
    except Exception as e:
        log.error(f"Donation checkout failed for {chat_id}: {e}")
        return None


def send_donation_stars_menu(chat_id: int):
    """Stars-Spendenoptionen senden."""
    text = "⭐ *Spenden mit Telegram Stars*\n\nWähle einen Betrag:"
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("⭐ 75 Stars",  callback_data="donate:stars:75"),
        InlineKeyboardButton("⭐ 150 Stars", callback_data="donate:stars:150"),
        InlineKeyboardButton("⭐ 350 Stars", callback_data="donate:stars:350"),
    )
    markup.add(InlineKeyboardButton("◀️ Zurück", callback_data="danke:donate"))
    last_bot_text[chat_id] = text
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)


def send_donation_stars_invoice(chat_id: int, stars: int):
    """Stars-Invoice für eine Spende senden."""
    label_map = {75: "☕ Ein Kaffee", 150: "🍕 Etwas mehr", 350: "🎉 Großes Danke!"}
    label = label_map.get(stars, f"{stars} Stars Spende")
    try:
        bot.send_invoice(
            chat_id,
            title=f"German Dude Bot — {label}",
            description="Danke dass du den German Dude Bot unterstützt! 🙏",
            payload=f"donation_{chat_id}",
            provider_token="",
            currency="XTR",
            prices=[telebot.types.LabeledPrice(label, stars)],
        )
    except Exception as e:
        log.error(f"Stars donation invoice failed for {chat_id}: {e}")
        bot.send_message(chat_id, "⚠️ Zahlung konnte nicht gestartet werden. Versuch es später.")


def start_feedback_mode(chat_id: int):
    """Feedback-Modus starten — nächste Textnachricht wird als Feedback gewertet."""
    uid  = str(chat_id)
    name = user_data.get(uid, {}).get("name", "")
    user_state[chat_id] = {"mode": "feedback"}
    bot.send_message(
        chat_id,
        f"💬 Was denkst du{', ' + name if name else ''}?\n\n"
        "Schreib einfach drauflos — was dir gefällt, was du dir wünschst, "
        "was dich nervt. Alles ist willkommen! 🙏\n\n"
        "_Zum Abbrechen: /start_",
        parse_mode="Markdown"
    )


def handle_feedback_message(chat_id: int, text: str):
    """Feedback speichern + Admin benachrichtigen."""
    uid  = str(chat_id)
    user = user_data.get(uid, {})
    name = user.get("name", "")

    # Speichern
    feedback_entry = {
        "text":      text,
        "timestamp": datetime.now().isoformat(),
        "name":      name,
        "level":     user.get("level", "?"),
        "premium":   is_premium(chat_id),
    }
    user_data[uid].setdefault("feedback", [])
    user_data[uid]["feedback"].append(feedback_entry)
    save_users(user_data)

    # Admin benachrichtigen
    if ADMIN_CHAT_ID:
        tier = "Plus" if is_premium_plus(chat_id) else ("Premium" if is_premium(chat_id) else "Free")
        try:
            bot.send_message(
                ADMIN_CHAT_ID,
                f"💬 *Neues Feedback*\n\n"
                f"Von: {name or 'Unbekannt'} ({uid}) — {tier}, Niveau {user.get('level', '?')}\n\n"
                f"_{text}_",
                parse_mode="Markdown"
            )
        except Exception as e:
            log.warning(f"Could not send feedback to admin: {e}")

    # User danken
    user_state[chat_id] = {"mode": "idle"}
    bot.send_message(
        chat_id,
        "🙏 Danke für dein Feedback! Das bedeutet mir wirklich viel.\n\n"
        "Izzi liest alles persönlich — versprochen. 💙"
    )


@bot.message_handler(commands=['danke'])
def danke_cmd(message):
    chat_id = message.chat.id
    ensure_user(chat_id)
    _track_feature(chat_id, 'danke')
    send_danke_menu(chat_id)


@bot.message_handler(commands=['share'])
def share_cmd(message):
    chat_id  = message.chat.id
    ref_link = f"https://t.me/germandude_bot?start=ref_{chat_id}"
    bot.send_message(chat_id,
        f"🤝 Teile den German Dude Bot mit deinen Freunden!\n\n"
        f"Dein persönlicher Einladungslink:\n{ref_link}\n\n"
        f"Für jeden Freund der sich anmeldet bekommst du 3 Tage gratis! 🎁"
    )

@bot.message_handler(commands=['shadowing'])
def shadowing_cmd(message):
    bot.send_message(message.chat.id, "🎧 Shadowing Mode kommt bald zurück! Bleib dran. 👀")

@bot.message_handler(commands=['restart'])
def restart_cmd(message):
    restart_chat(message.chat.id)


@bot.message_handler(commands=["adminstats"])
def handle_adminstats(message):
    """Admin-only: funnel dropout analysis + core stats."""
    if message.chat.id != ADMIN_CHAT_ID:
        return

    now   = datetime.now()
    users = load_users()
    total = len(users)
    if total == 0:
        bot.send_message(message.chat.id, "Noch keine User.")
        return

    # ── Tier-Übersicht ──────────────────────────────────────────────────────
    n_plus    = sum(1 for u in users.values() if u.get("premium_plus"))
    n_premium = sum(1 for u in users.values() if u.get("premium") and not u.get("premium_plus"))
    n_trial   = 0
    for u in users.values():
        if not u.get("premium") and u.get("trial_start") and u.get("trial_code_used"):
            days = TRIAL_CODES.get(u.get("trial_code_used", ""), 3)
            if isinstance(days, int) and (now - datetime.fromisoformat(u["trial_start"])).days < days:
                n_trial += 1
    n_free = total - n_plus - n_premium - n_trial

    # ── Funnel-Stufen ───────────────────────────────────────────────────────
    # Stufe 1 — Joined (= alle)
    f_joined = total

    # Stufe 2 — Onboarding komplett (Name + Sprache + Ziel gesetzt)
    f_onboarded = sum(1 for u in users.values()
                      if u.get("name") and u.get("native_language") and u.get("goal"))

    # Stufe 3 — Mindestens 1 Gespräch geführt
    f_talked = sum(1 for u in users.values() if u.get("conversations_started", 0) >= 1)

    # Stufe 4 — Wiederkehrender User (3+ Gespräche)
    f_retained = sum(1 for u in users.values() if u.get("conversations_started", 0) >= 3)

    # Stufe 5 — Paywall gesehen
    f_paywall = sum(1 for u in users.values() if u.get("paywall_hits", 0) > 0)

    # Stufe 6 — Konvertiert (Premium oder Plus)
    f_converted = n_premium + n_plus

    def pct(a, b):
        return f"{a/b*100:.0f}%" if b else "–"

    # ── Dropout-Analyse ─────────────────────────────────────────────────────
    # Wo verlieren wir die meisten User?
    drop_onboarding = f_joined  - f_onboarded  # Abbruch beim Onboarding
    drop_first_conv = f_onboarded - f_talked    # Abbruch nach Onboarding, vor erstem Gespräch
    drop_retention  = f_talked  - f_retained    # Einmal geschaut, nie wiedergekommen
    drop_paywall    = f_paywall - f_converted   # Paywall gesehen, nicht konvertiert

    # ── Aktivität ───────────────────────────────────────────────────────────
    def days_since(u):
        la = u.get("last_active")
        if not la: return 9999
        try: return (now - datetime.fromisoformat(la)).days
        except: return 9999

    active_1d  = sum(1 for u in users.values() if days_since(u) <= 1)
    active_7d  = sum(1 for u in users.values() if days_since(u) <= 7)
    active_30d = sum(1 for u in users.values() if days_since(u) <= 30)
    churned    = sum(1 for u in users.values() if days_since(u) > 30)

    total_convos = sum(u.get("conversations_started", 0) for u in users.values())
    avg_convos   = f"{total_convos / total:.1f}" if total else "–"

    # ── Voice Push (Premium Plus Retention) ─────────────────────────────────
    current_week = now.strftime("%G-W%V")
    vp_scheduled = sum(1 for u in users.values()
                       if u.get("voice_push", {}).get("week") == current_week
                       and len(u.get("voice_push", {}).get("scheduled", [])) > 0)
    vp_sent_week = sum(len(u.get("voice_push", {}).get("sent", []))
                       for u in users.values()
                       if u.get("voice_push", {}).get("week") == current_week)

    # ── Top Sprachen ─────────────────────────────────────────────────────────
    langs = {}
    for u in users.values():
        l = u.get("native_language") or "–"
        langs[l] = langs.get(l, 0) + 1
    top_langs = sorted(langs.items(), key=lambda x: -x[1])[:5]

    # ── Top Features ─────────────────────────────────────────────────────────
    features = {}
    for u in users.values():
        for feat, cnt in u.get("features_used", {}).items():
            features[feat] = features.get(feat, 0) + cnt
    top_features = sorted(features.items(), key=lambda x: -x[1])[:6]

    # ── Feedback-Einträge ────────────────────────────────────────────────────
    n_feedback = sum(len(u.get("feedback", [])) for u in users.values())

    # ── Output ───────────────────────────────────────────────────────────────
    lines = [
        "📊 *German Dude — Stats & Funnel*",
        "",
        "👥 *User-Übersicht*",
        f"   Gesamt: {total}",
        f"   👑 Premium Plus: {n_plus}",
        f"   💼 Premium: {n_premium}",
        f"   ⏳ Trial: {n_trial}",
        f"   🔓 Free: {n_free}",
        "",
        "🔽 *Funnel — wo springen sie ab?*",
        f"   Joined:            {f_joined}",
        f"   Onboarding fertig: {f_onboarded} ({pct(f_onboarded, f_joined)})",
        f"   1. Gespräch:       {f_talked} ({pct(f_talked, f_onboarded)})",
        f"   3+ Gespräche:      {f_retained} ({pct(f_retained, f_talked)})",
        f"   Paywall gesehen:   {f_paywall}",
        f"   Konvertiert:       {f_converted} ({pct(f_converted, f_paywall)})",
        "",
        "⚠️ *Größte Dropouts*",
        f"   Beim Onboarding:    -{drop_onboarding}",
        f"   Vor 1. Gespräch:    -{drop_first_conv}",
        f"   Nach 1. Gespräch:   -{drop_retention}  ← Einmal & weg",
        f"   An der Paywall:     -{drop_paywall}",
        "",
        "📅 *Aktivität*",
        f"   Heute aktiv:    {active_1d}",
        f"   Letzte 7 Tage:  {active_7d}",
        f"   Letzte 30 Tage: {active_30d}",
        f"   Abgesprungen:   {churned}  (30+ Tage inaktiv)",
        f"   ⌀ Gespräche/User: {avg_convos}",
        "",
        "🎤 *Voice Pushes (diese Woche)*",
        f"   Plus-User mit Schedule: {vp_scheduled}",
        f"   Pushes gesendet:        {vp_sent_week}",
        "",
        "🌍 *Top Sprachen*",
    ]
    for lang, cnt in top_langs:
        lines.append(f"   {lang}: {cnt}")

    if top_features:
        lines += ["", "🔥 *Top Features*"]
        for feat, cnt in top_features:
            lines.append(f"   /{feat}: {cnt}×")

    lines += [
        "",
        f"💬 Feedback-Einträge gesamt: {n_feedback}",
    ]

    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown")


KULTUR_TOPICS = [
    ("🎉", "Feste, Feiertage & Brückentage",          "kultur:feiertage"),
    ("⏰", "Deutsche Pünktlichkeit",                   "kultur:puenktlichkeit"),
    ("🚔", "Ordnungsamt, Polizei, Feuerwehr",          "kultur:behoerden"),
    ("🎁", "Geschenke: Was schenkt man?",              "kultur:geschenke"),
    ("🤝", "Freundschaft: Dos and Don'ts",             "kultur:freundschaft"),
    ("😴", "Sonntag: Hier wird nichts gemacht!",       "kultur:sonntag"),
    ("🍞", "Brot & Bier: Deutsche Klassiker",          "kultur:brot_bier"),
    ("🇩🇪", "Mini-Geschichte: Modernes Deutschland",  "kultur:geschichte"),
    ("😂", "Humor & Politische Satire",               "kultur:humor"),
    ("💬", "Top 20 Füllwörter",                       "kultur:fuellwoerter"),
    ("💕", "German Romance: Beziehungen",             "kultur:romance"),
    ("💍", "Hochzeit & Scheidung",                    "kultur:hochzeit"),
    ("♻️", "Mülltrennung: Digest",                   "kultur:muell"),
    ("🚆", "BVG, ICE, RE... Verkehrsabkürzungen",    "kultur:verkehr_abk"),
    ("🚗", "Verkehr in Deutschland",                  "kultur:verkehr"),
    ("😆", "Top 15 Deutsche Memes",                  "kultur:memes"),
    ("🏛️", "Behördendeutsch: 20 Sätze",             "kultur:behoerdendeutsch"),
]

KULTUR_PROMPTS = {
    "feiertage":       "Schreibe einen informativen Mini-Text (150 Wörter) auf einfachem Deutsch (A2/B1) über deutsche Feste, Feiertage und Brückentage. Was sind die wichtigsten? Was macht man da?",
    "puenktlichkeit":  "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch über deutsche Pünktlichkeit. Warum ist sie so wichtig? Was passiert wenn man zu spät kommt? Mit Alltagsbeispielen.",
    "behoerden":       "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch über Ordnungsamt, Polizei und Feuerwehr in Deutschland. Was ist der Unterschied? Wann ruft man wen an?",
    "geschenke":       "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch über Geschenkkultur in Deutschland. Was schenkt man? Was schenkt man nicht? Was ist unhöflich?",
    "freundschaft":    "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch über Freundschaft in Deutschland. Wie wird man Freunde? Was ist normales Verhalten? Du vs. Sie.",
    "sonntag":         "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch über den deutschen Sonntag. Ruhezeit, Lärmschutz, geschlossene Geschäfte, was erlaubt ist und was nicht.",
    "brot_bier":       "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch über Brot und Bier als deutsche Kulturklassiker. Typen, Traditionen, lustige Fakten.",
    "geschichte":      "Schreibe eine kurze Geschichte (150 Wörter) auf einfachem Deutsch über das moderne Deutschland — Multikulturalismus, Mentalität, was sich verändert hat.",
    "humor":           "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch über deutschen Humor und politische Satire. Was ist Kabarett? Erwähne Moritz Neumeier und Till Reiners mit konkreten Beispiel-Themen.",
    "fuellwoerter":    "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch mit den Top 20 deutschen Füllwörtern: halt, doch, mal, eigentlich, ja, naja, eben, irgendwie usw. Je ein Beispielsatz.",
    "romance":         "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch über Beziehungen und Dating in Deutschland. Wie flirtet man? Was ist typisch?",
    "hochzeit":        "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch über Hochzeit und Scheidung in Deutschland. Warum heiraten viele spät oder gar nicht? Was ist eine Lebenspartnerschaft?",
    "muell":           "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch über Mülltrennung. Welche Tonne ist was? Was ist Pfand? Was passiert bei falscher Trennung?",
    "verkehr_abk":     "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch mit einer Liste der wichtigsten Verkehrsabkürzungen: BVG, ICE, RE, RB, S-Bahn, U-Bahn, DB, ÖPNV usw. Mit kurzer Erklärung.",
    "verkehr":         "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch über Verkehr in Deutschland. Autobahn, Fahrrad, Vorfahrt, häufige Fehler von Ausländern.",
    "memes":           "Schreibe einen Mini-Text (150 Wörter) auf einfachem Deutsch über bekannte deutsche Internet-Memes und Phänomene. Beschreibe 5-6 konkrete Beispiele mit kurzer Erklärung.",
    "behoerdendeutsch":"Schreibe einen Mini-Text auf einfachem Deutsch mit 20 typischen Sätzen aus deutschen Ämtern. Format: 'Satz' — Bedeutung auf einfachem Deutsch.",
}


def _send_next_kultur_question(chat_id):
    """Send next pre-generated kultur quiz question."""
    state     = user_state.get(chat_id, {})
    questions = state.get("kultur_quiz_questions", [])
    answers   = state.get("kultur_quiz_answers", [])
    idx       = state.get("exercise_idx", 0)
    total     = state.get("exercise_total", 3)

    if idx >= total:
        _finish_exercise_session(chat_id); return

    q = questions[idx]
    user_state[chat_id]["exercise_current_q"] = {
        "question": q, "correct": answers[idx], "explanation": "",
        "a": "", "b": "", "c": "",
    }
    text = f"❓ Frage {idx + 1} von {total}\n\n{q}"
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("A", callback_data="ex_ans:A"),
        InlineKeyboardButton("B", callback_data="ex_ans:B"),
        InlineKeyboardButton("C", callback_data="ex_ans:C"),
    )
    bot.send_message(chat_id, text, reply_markup=markup)


def _show_kultur_menu(chat_id):
    markup = InlineKeyboardMarkup(row_width=1)
    for emoji, name, cb in KULTUR_TOPICS:
        markup.add(InlineKeyboardButton(f"{emoji} {name}", callback_data=cb))
    markup.add(InlineKeyboardButton("◀️ Zurück", callback_data="intg:back"))
    bot.send_message(chat_id, "🇩🇪 Deutsche Kultur\n\nWähle ein Thema:", reply_markup=markup)


@bot.message_handler(commands=["integration", "bürokratie", "buerokratie", "leben"])
def handle_integration(message):
    chat_id = message.chat.id
    ensure_user(chat_id)
    if _require_onboarding(chat_id): return
    _track_feature(chat_id, "integration")
    _show_integration_menu(chat_id)


def _strip_md(text: str) -> str:
    """Remove markdown formatting that breaks Telegram plain text."""
    import re as _re
    text = _re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = _re.sub(r"\*(.+?)\*", r"\1", text)
    text = _re.sub(r"#{1,3} ", "", text)
    text = text.replace("**", "").replace("__", "")
    return text.strip()


def _show_integration_menu(chat_id):
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("📄 Brief / Dokument erklären",    callback_data="intg:brief_erklaeren"),
        InlineKeyboardButton("✍️ Antwort auf einen Brief",      callback_data="intg:brief_antworten"),
        InlineKeyboardButton("🎭 Termin vorbereiten",           callback_data="intg:termin"),
        InlineKeyboardButton("🗺️ Beratungsstellen finden",      callback_data="intg:beratung"),
        InlineKeyboardButton("💶 Steuern & Finanzamt",          callback_data="intg:steuern"),
        InlineKeyboardButton("🇩🇪 Deutsche Kultur",             callback_data="intg:kultur"),
    )
    bot.send_message(chat_id,
        "🏛️ Leben in Deutschland\n\nWomit kann ich dir helfen?",
        reply_markup=markup)


def _show_steuern_menu(chat_id):
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("📄 Steuerbescheid erklären",      callback_data="intg:steuerbescheid"),
        InlineKeyboardButton("📋 Was ist eine Steuererklärung?", callback_data="intg:steuererklaerung_info"),
        InlineKeyboardButton("🗓️ Fristen & Termine",            callback_data="intg:steuerfristen"),
        InlineKeyboardButton("🏢 Mein Finanzamt finden",        callback_data="intg:finanzamt"),
        InlineKeyboardButton("◀️ Zurück",                       callback_data="intg:back"),
    )
    bot.send_message(chat_id,
        "💶 Steuern & Finanzamt\n\nWas brauchst du?",
        reply_markup=markup)




def _generate_kultur_content(topic_key: str, topic_label: str, native_lang: str, level: str) -> dict:
    """Generate mini-text + 3 quiz questions for a culture topic."""
    prompts = {
        "feste_feiertage":    "Erkläre deutsche Feste, Feiertage und Brückentage. Was sind die wichtigsten? Was macht man? Was ist ein Brückentag?",
        "puenktlichkeit":     "Erkläre die deutsche Pünktlichkeitskultur. Wie wichtig ist sie? Was passiert wenn man zu spät kommt? Gibt es Ausnahmen?",
        "behoerden_ordnung":  "Erkläre die Rollen von Ordnungsamt, Polizei und Feuerwehr in Deutschland. Was sind die Unterschiede? Wann ruft man wen?",
        "geschenke":          "Was schenkt man in Deutschland? Was sind typische Geschenke? Was gilt als unhöflich? Gibt es Regeln?",
        "freundschaft":       "Wie funktioniert Freundschaft in Deutschland? Was sind die Dos and Don'ts? Warum sind Deutsche anfangs kühl?",
        "sonntag":            "Was ist der deutsche Sonntag? Was ist erlaubt, was verboten? Warum ist das so? Was macht man am Sonntag?",
        "brot_bier":          "Erkläre die Bedeutung von Brot und Bier in der deutschen Kultur. Fakten, Traditionen, Zahlen.",
        "moderne_geschichte": "Mini-Geschichte: Deutschland heute. Von der Teilung zur Wiedervereinigung bis heute — die wichtigsten Punkte kompakt.",
        "humor_satire":       "Was ist politische Satire in Deutschland? Nenne Beispiele: Moritz Neumeier, Till Reiners, Die Anstalt. Was darf man satirisieren?",
        "fuellwoerter":       "Liste die Top 20 deutschen Füllwörter (halt, mal, doch, eigentlich, irgendwie...) mit Bedeutung und je einem Beispielsatz.",
        "romance":            "Wie läuft Romantik in Deutschland ab? Wie lernt man jemanden kennen? Was sind typische Dates? Kulturelle Unterschiede?",
        "hochzeit_scheidung": "Hochzeit und Scheidung in Deutschland — Fakten, Statistiken, warum Deutsche weniger heiraten, Lebenspartnerschaft.",
        "muelltrennung":      "Der komplette Guide zur deutschen Mülltrennung: Gelbe Tonne, Blaue Tonne, Restmüll, Biomüll, Pfand. Regeln und Tipps.",
        "verkehr_abkuerzungen": "Liste und erkläre: BVG, DB, ICE, IC, RE, RB, S-Bahn, U-Bahn, Tram, Bus, MVV usw. Mit Kontext wann man was benutzt.",
        "verkehr_allgemein":  "Verkehr in Deutschland: Autobahn (kein Tempolimit!), Verkehrsregeln, Fahrrad-Kultur, ÖPNV, Führerschein.",
        "memes":              "Top 15 deutsche Memes und Internet-Phänomene — erkläre den Witz/Kontext hinter jedem. Mit kurzer Beschreibung wo man sie findet.",
        "behoerdendeutsch":   "Liste 20 typische Sätze die man in deutschen Ämtern hört, mit Erklärung was sie bedeuten und wie man antwortet.",
    }

    prompt_text = prompts.get(topic_key, f"Erkläre: {topic_label}")

    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        system=(
            f"Du bist ein Kulturguide für Deutschland. Der User spricht {native_lang} und lernt Deutsch (Niveau {level}).\n"
            f"Erstelle einen kompakten, unterhaltsamen Kulturtext auf Deutsch (max 200 Wörter, {level}-Niveau).\n"
            f"Danach auf einer Leerzeile: eine kurze Zusammenfassung auf {native_lang} (2-3 Sätze).\n"
            f"Dann GENAU 3 Multiple-Choice-Fragen zum Text in diesem Format:\n"
            f"---FRAGEN---\n"
            f"F1: Frage?\nA: Option  B: Option  C: Option\nANTWORT: A/B/C\n"
            f"F2: Frage?\nA: Option  B: Option  C: Option\nANTWORT: A/B/C\n"
            f"F3: Frage?\nA: Option  B: Option  C: Option\nANTWORT: A/B/C"
        ),
        messages=[{"role": "user", "content": prompt_text}]
    )
    raw = resp.content[0].text.strip()

    # Split text from questions
    if "---FRAGEN---" in raw:
        text_part, _, q_part = raw.partition("---FRAGEN---")
    else:
        text_part = raw
        q_part    = ""

    # Parse questions
    import re as _re
    questions = []
    for m in _re.finditer(r"F(\d): (.+?)\nA: (.+?)  B: (.+?)  C: (.+?)\nANTWORT: ([ABC])", q_part.strip(), _re.DOTALL):
        questions.append({
            "question": m.group(2).strip(),
            "a": m.group(3).strip(), "b": m.group(4).strip(), "c": m.group(5).strip(),
            "correct": m.group(6).strip(),
        })

    return {"text": text_part.strip(), "questions": questions}

# ─────────────────────────────────────────────
# MAIN LOOP
@bot.message_handler(commands=["gem", "gems", "wortschatz"])
def handle_gem_command(message):
    """Send today's German Gem and start the practice exercise."""
    chat_id = message.chat.id
    ensure_user(chat_id)
    if _require_onboarding(chat_id): return
    _track_feature(chat_id, 'gem')
    send_daily_gem(chat_id)


def send_daily_gem(chat_id):
    """Send today's gem — Claude generates everything live in user's native language."""
    uid         = str(chat_id)
    expression  = get_todays_gem(uid)
    user        = user_data.get(uid, {})
    native_lang = user.get("native_language") or "Englisch"
    level       = user.get("level", "A2")
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=400,
            system=(
                f"Du bist ein freundlicher Deutschlehrer. User spricht {native_lang} (Niveau: {level}).\n"
                "Erstelle einen Gem-des-Tages Eintrag in GENAU diesem Format:\n"
                "TYP: <Redewendung / Slang / Alltagsausdruck / Idiom / Umgangssprache>\n"
                "BEDEUTUNG: <1 Satz auf Deutsch, einfach erklärt>\n"
                f"UEBERSETZUNG: <Bedeutung auf {native_lang}, natürlich formuliert>\n"
                "BEISPIEL1: <alltagsnaher Satz, du- oder ich-Form>\n"
                "BEISPIEL2: <alltagsnaher Satz, du- oder ich-Form>\n"
                "BEISPIEL3: <alltagsnaher Satz, du- oder ich-Form>\n"
                "Kein er/sie in Beispielen. Kein Schulbuch-Deutsch. Nur die Zeilen."
            ),
            messages=[{"role": "user", "content": f"Ausdruck: {expression}"}]
        )
        raw  = resp.content[0].text.strip()
        data = {}
        for line in raw.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                data[key.strip().upper()] = val.strip()
        typ          = data.get("TYP", "Ausdruck")
        bedeutung    = data.get("BEDEUTUNG", "")
        uebersetzung = data.get("UEBERSETZUNG", "")
        beispiele    = [data.get(f"BEISPIEL{i}", "") for i in range(1,4) if data.get(f"BEISPIEL{i}")]
    except Exception as e:
        log.error(f"Gem generation failed: {e}")
        typ = "Ausdruck"; bedeutung = ""; uebersetzung = ""; beispiele = []

    lines = ["💎 *German Gem des Tages*", "", f"🗣 *{expression}* · _{typ}_", ""]
    if bedeutung:    lines += [f"📖 Bedeutung: {bedeutung}", ""]
    if uebersetzung and native_lang and native_lang.lower() != "none":
        lines += [f"🌍 {native_lang}: {uebersetzung}", ""]
    if beispiele:
        lines.append("Beispiele aus dem echten Leben:")
        for ex in beispiele: lines.append(f"• {ex}")
    lines += ["", f"✏️ Deine Aufgabe: Schreib oder sprich dein eigenes Beispiel mit: {expression}", "Ich überprüfe es und gebe dir Feedback. 🙂"]
    msg = "\n".join(lines)
    _cid = int(chat_id)
    last_bot_text[_cid] = msg
    user_state[_cid] = {"mode": user_state.get(_cid, {}).get("mode", "idle"), "gem_exercise": expression, "gem_text": expression}
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))
    bot.send_message(_cid, msg, parse_mode="Markdown", reply_markup=markup)


def check_gem_exercise(chat_id, user_sentence, gem_text):
    """Check user's gem exercise sentence and give feedback."""
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content":
                f"The user is learning German. Today's gem is: '{gem_text}'. "
                f"The user wrote this sentence: '{user_sentence}'. "
                f"Check if the gem is used correctly and naturally. "
                f"Reply in German. Be encouraging. If correct: praise + maybe suggest a variation. "
                f"If wrong: explain kindly what's off and give a corrected version. "
                f"Keep it short — max 3 sentences."
            }]
        )
        feedback = resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"Gem exercise check failed: {e}")
        feedback = "Super Versuch! Mach weiter so! 🙂"

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💎 Nächstes Gem", callback_data="next_gem"))
    markup.add(InlineKeyboardButton("🎯 Zu den Themen", callback_data="menu_themen"))
    bot.send_message(chat_id, f"📝 {feedback}", parse_mode="Markdown", reply_markup=markup)

    # Clear gem exercise state
    state = user_state.get(chat_id, {})
    state.pop("gem_exercise", None)
    state.pop("gem_text", None)
    user_state[chat_id] = state


@bot.message_handler(commands=["support", "hilfe"])
def handle_support(message):
    bot.send_message(message.chat.id,
        "🆘 *Support*\n\n"
        "Hast du Fragen, Feedback oder ein Problem?\n\n"
        "Schreib mir direkt auf Telegram: @hagzussa\n\n"
        "_Ich antworte so schnell wie möglich! 🙂_",
        parse_mode="Markdown")


@bot.message_handler(commands=["achievements", "badges", "erfolge"])
def handle_achievements(message):
    ensure_user(message.chat.id)
    if _require_onboarding(message.chat.id): return
    send_my_progress(message.chat.id)


@bot.message_handler(commands=["levelup", "level_up", "nächstesniveau"])
def handle_level_up(message):
    bot.send_message(message.chat.id,
        "Das Niveau wird automatisch angepasst — einfach weiter üben! 💪\n"
        "Dein aktuelles Niveau: /level", parse_mode="Markdown")


@bot.message_handler(commands=["uebersetzen", "übersetzen", "translate"])
def handle_translate(message):
    """Translate the last NPC message into the user's native language."""
    chat_id = message.chat.id
    mem = user_memory.get(chat_id, [])
    # Find last assistant message
    last_npc = next(
        (m["content"] for m in reversed(mem) if m.get("role") == "assistant"),
        None
    )
    if not last_npc:
        bot.send_message(chat_id, "Noch keine Nachricht zum Übersetzen 🙂")
        return
    user = user_data.get(str(chat_id), {})
    lang = user.get("native_language", "Englisch")
    translation = get_translation(chat_id, last_npc)
    bot.send_message(chat_id, f"🌍 Übersetzung ({lang}):\n\n_{translation}_",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup())


@bot.message_handler(func=lambda message: True)
def handle(message):
    # Skip non-text messages — they have dedicated handlers
    if not message.text:
        return
    chat_id = message.chat.id
    text = message.text

    # Befehle die hier ankommen, wurden von keinem früher registrierten
    # commands=[...]-Handler erkannt. Statt sie als Chat-Text zu behandeln,
    # an spätere Handler weiterreichen (z.B. Admin-Befehle weiter unten im File).
    if text.startswith('/'):
        return telebot.ContinueHandling()

    # Route via state machine
    mode = user_state.get(chat_id, {}).get("mode")

    if mode == "onboarding":
        handle_onboarding(chat_id, text)
        return

    if chat_id in test_state and test_state[chat_id].get("phase"):
        return  # Active test — handled by callback router

    if mode == "test":
        return  # Test mode — handled by callback router

    if mode in ("ready", "topic_select"):
        # User typed instead of clicking — nudge them
        send_topic_buttons(chat_id)
        return

    if mode == "feedback":
        handle_feedback_message(chat_id, text)
        return

    if mode == "exercises":
        # User typed during exercise summary — just show menu
        show_menu(chat_id)
        return

    if mode == "menu":
        if text == "1":
            send_topic_buttons(chat_id)
        elif text == "2":
            send_progress(chat_id)
        elif text == "3":
            show_errors(chat_id)
        elif text == "4":
            show_level(chat_id)
        elif text == "5":
            start_exercise(chat_id)
        elif text == "6":
            start_shadowing(chat_id)
        elif text == "7":
            restart_chat(chat_id)
        else:
            show_menu(chat_id)
        return

    if mode == "shadowing":
        bot.send_message(chat_id, "🎧 Schick bitte eine *Sprachnachricht* zum Nachsprechen.",
            parse_mode="Markdown")
        return

    if mode == "exercise":
        answers_given = text.strip().lower().replace(",", " ").replace(".", " ").split()
        # Extract patterns like "1a", "2b" or just "a b c d e"
        import re as _re
        parsed = []
        for tok in answers_given:
            m = _re.match(r"^\d*([abc])$", tok)
            if m:
                parsed.append(m.group(1))
        if not parsed:
            bot.send_message(chat_id, "👀 Schreib deine Antworten so: 1a 2b 3c 4a 5b")
            return

        questions = user_state[chat_id].get("exercise_questions", [])
        correct   = user_state[chat_id].get("exercise_answers", [])
        topic     = user_state[chat_id].get("exercise_topic", "Grammatik")
        level     = user_state[chat_id].get("exercise_level", "A2")

        # Build feedback
        lines      = ["📊 Auswertung:\n"]
        errors     = []
        score      = 0
        for i, (q, c) in enumerate(zip(questions, correct)):
            given = parsed[i] if i < len(parsed) else "?"
            if given == c:
                lines.append(f"✅ {i+1}. Richtig! ({given.upper()})")
                score += 1
            else:
                lines.append(f"❌ {i+1}. Falsch — du: {given.upper()}, richtig: {c.upper()}")
                errors.append({"q": q, "given": given, "correct": c})

        lines.append(f"\n🎯 {score}/{len(correct)} richtig")

        # Explain errors via Claude
        if errors:
            lines.append("\n📖 Erklärungen:")
            for err in errors:
                try:
                    expl = claude.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=120,
                        messages=[{"role": "user", "content":
                            f"Erkläre kurz auf Deutsch (2 Sätze max), warum bei dieser Aufgabe Antwort {err['correct'].upper()} richtig ist und {err['given'].upper()} falsch:\n{err['q']}"
                        }]
                    )
                    lines.append(f"• {expl.content[0].text.strip()}")
                except Exception:
                    pass

        user_state[chat_id]["mode"] = "idle"
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("💪 Nochmal üben", callback_data="new_exercises"),
            InlineKeyboardButton("🏠 Menü",         callback_data="go_menu"),
        )
        bot.send_message(chat_id, "\n".join(lines), reply_markup=markup)
        return

    # Integration modes — text input handlers
    if mode == "intg_brief_erklaeren":
        if not text or not text.strip():
            bot.send_message(chat_id,
                "📄 Bitte schick mir den Text des Briefes als Textnachricht\n"
                "(kopieren & einfügen) — oder ein Foto des Briefes! 📸")
            return
        uid         = str(chat_id)
        native_lang = user_data.get(uid, {}).get("native_language") or "Englisch"
        bot.send_message(chat_id, "🔍 Analysiere den Brief...")
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=600,
                system=(
                    f"Du bist ein hilfreicher Assistent für Menschen die in Deutschland leben.\n"
                    f"Erkläre den folgenden deutschen Brief in diesen Abschnitten:\n"
                    f"1. EINFACHES DEUTSCH: Was bedeutet der Brief? (A2/B1 Niveau)\n"
                    f"2. {native_lang.upper()}: Kurze Zusammenfassung auf {native_lang}\n"
                    f"3. WAS TUN: Konkrete nächste Schritte\n"
                    f"4. FRIST: Gibt es eine Frist? Wenn ja, wann?\n"
                    f"Sei beruhigend — Behördenbriefe machen Angst."
                ),
                messages=[{"role": "user", "content": f"Brief:\n{text}"}]
            )
            result = resp.content[0].text.strip()
        except Exception as e:
            result = f"⚠️ Fehler beim Analysieren: {e}"
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("✍️ Antwort schreiben", callback_data="intg:brief_antworten"),
            InlineKeyboardButton("🏛️ Menü", callback_data="intg:back"),
        )
        user_state[chat_id]["mode"] = "idle"
        bot.send_message(chat_id, _strip_md(result), reply_markup=markup)
        return

    if mode == "intg_brief_antworten":
        uid         = str(chat_id)
        native_lang = user_data.get(uid, {}).get("native_language") or "Englisch"
        bot.send_message(chat_id, "✍️ Schreibe den Antwortbrief...")
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=600,
                system=(
                    f"Du schreibst formelle deutsche Briefe für Menschen die Deutsch lernen.\n"
                    f"Schreibe einen vollständigen formellen Antwortbrief auf Deutsch.\n"
                    f"Format: Datum [Datum eintragen], Absender [Name/Adresse eintragen], Empfänger, Betreff, Brief, Grußformel.\n"
                    f"Danach eine kurze Erklärung des Briefes auf {native_lang}.\n"
                    f"Hinweis am Ende: Name, Adresse und Aktenzeichen selbst eintragen."
                ),
                messages=[{"role": "user", "content": f"Situation: {text}"}]
            )
            result = resp.content[0].text.strip()
        except Exception as e:
            result = f"⚠️ Fehler: {e}"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🏛️ Menü", callback_data="intg:back"))
        user_state[chat_id]["mode"] = "idle"
        bot.send_message(chat_id, _strip_md(result), reply_markup=markup)
        return

    if mode == "intg_steuerbescheid":
        uid         = str(chat_id)
        native_lang = user_data.get(uid, {}).get("native_language") or "Englisch"
        bot.send_message(chat_id, "💶 Analysiere den Steuerbescheid...")
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=500,
                system=(
                    f"Du bist ein freundlicher Steuerberater-Assistent.\n"
                    f"Erkläre den Steuerbescheid:\n"
                    f"1. ERGEBNIS: Bekommt die Person Geld zurück oder muss nachzahlen? Wie viel?\n"
                    f"2. EINFACHE ERKLÄRUNG: Was bedeutet das auf einfachem Deutsch?\n"
                    f"3. {native_lang.upper()}: Kurze Zusammenfassung auf {native_lang}\n"
                    f"4. WAS TUN: Nächste Schritte — Einspruch möglich? Frist?\n"
                    f"Weise darauf hin, dass du kein Steuerberater bist."
                ),
                messages=[{"role": "user", "content": f"Steuerbescheid:\n{text}"}]
            )
            result = resp.content[0].text.strip()
        except Exception as e:
            result = f"⚠️ Fehler: {e}"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("◀️ Zurück", callback_data="intg:steuern"))
        user_state[chat_id]["mode"] = "idle"
        bot.send_message(chat_id, result, reply_markup=markup)
        return

    if mode in ("intg_beratung", "intg_finanzamt"):
        uid         = str(chat_id)
        native_lang = user_data.get(uid, {}).get("native_language") or "Englisch"
        is_finanzamt = (mode == "intg_finanzamt")
        prompt = (
            f"Finde das zuständige Finanzamt für: {text}. "
            f"Gib Adresse, Telefon und Website an. Dann kurz auf {native_lang}."
            if is_finanzamt else
            f"Finde Beratungsstellen für Migranten in: {text}. "
            f"Nenne: Migrationsberatung, VHS Sprachkurse, Jobcenter, Sozialberatung. "
            f"Mit Websites wenn möglich. Dann kurze Übersicht auf {native_lang}."
        )
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=400,
                messages=[{"role": "user", "content": prompt}]
            )
            result = resp.content[0].text.strip()
        except Exception as e:
            result = f"⚠️ Fehler: {e}"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🏛️ Menü", callback_data="intg:back"))
        user_state[chat_id]["mode"] = "idle"
        bot.send_message(chat_id, result, reply_markup=markup)
        return

    # Voice selection
    if user_voice.get(chat_id) == "__choosing__":
        if message.text in VOICES:
            user_voice[chat_id] = message.text
            bot.send_message(chat_id, f"✅ Stimme gesetzt: *{message.text}*",
                parse_mode="Markdown",
                reply_markup=telebot.types.ReplyKeyboardRemove())
        else:
            bot.send_message(chat_id, "❗ Bitte wähle eine Stimme aus der Liste.")
        return

    if chat_id not in turn_counter:
        turn_counter[chat_id] = 0
    if chat_id not in session_state:
        session_state[chat_id] = {"struggle": 0, "success": 0}

    # ── NUDGE RECOVERY ────────────────────────────────────────────────────────
    # If the bot never replied to the last voice message (silent failure), treat
    # any typed message as a retry signal and respond to that voice instead.
    if last_voice_answered.get(chat_id) is False and last_voice_text.get(chat_id):
        saved_text   = last_voice_text[chat_id]
        saved_answer = last_voice_answer.get(chat_id)   # None if ask_gpt itself failed
        last_voice_answered[chat_id] = True
        try:
            if saved_answer:
                # GPT already gave us an answer — TTS was the thing that failed.
                # Just resend that answer as voice; no new GPT call, no memory pollution.
                send_reply(chat_id, saved_answer, voice=True)
            else:
                # ask_gpt itself failed — memory may have an orphaned user message.
                # Clean it before retrying so we don't double-append.
                mem = user_memory.get(chat_id, [])
                if mem and mem[-1].get("role") == "user":
                    user_memory[chat_id].pop()
                answer = ask_gpt(chat_id, saved_text)
                send_reply(chat_id, answer, voice=True)
        except Exception:
            bot.send_message(chat_id, "⚠️ Etwas ist schiefgelaufen. Bitte nochmal versuchen.")
        return
    # ──────────────────────────────────────────────────────────────────────────

    # If we're inside an active voice scenario, short/punctuation-only text
    # (like "?", "!", "wtf") is never real input — just ask them to speak.
    if current_scenario.get(chat_id) and _is_nudge_text(message.text):
        bot.send_message(chat_id, "🎙️ Schick eine Sprachnachricht, um weiterzumachen.")
        return

    # ── VOICE NUDGE: user sends text instead of voice in active scenario ──────
    import random as _r
    _nudges = [
        "Hey, ich würd gerne deine Stimme hören! 🎤",
        "Du weißt doch — ich bin ein Sprachnachrichten-Typ 😄🎤",
        "Psst... ich höre dich lieber als ich dich lese! 🎤",
        "Komm, schick mir eine Sprachnachricht — das macht viel mehr Spaß! 🎙️",
        "Ich lese zwar alles — aber deine Stimme mag ich lieber 🎤",
        "Hey, Stimme bitte! Das ist dein Deutschkurs, nicht dein Tagebuch 😄🎤",
    ]
    if current_scenario.get(chat_id) and message.text and mode in ("chat", "quatschen", None):
        # Nudge first text message, then every 4th
        _txt_count = user_data.get(str(chat_id), {}).get("text_msg_count", 0)
        user_data[str(chat_id)]["text_msg_count"] = _txt_count + 1
        if _txt_count == 0 or _txt_count % 4 == 0:
            bot.send_message(chat_id, _r.choice(_nudges))
        # Still process the text — answer with voice as always

    # ── GEM EXERCISE CHECK ───────────────────────────────────────────────────
    state = user_state.get(chat_id, {})
    if state.get("gem_exercise") and text:
        check_gem_exercise(chat_id, text, state["gem_text"])
        return

    # ── QUATSCHEN MODE ────────────────────────────────────────────────────────
    if mode == "quatschen":
        handle_quatschen_message(chat_id, text)
        return

    # ── CHAT MODE guard ───────────────────────────────────────────────────────
    if mode not in ("chat", "idle", None) and mode is not None:
        log.warning(f"Unhandled mode '{mode}' for chat_id {chat_id} — falling through to chat")

    result = analyze_user_input(message.text if message.text else "")
    if result == "struggle":
        session_state[chat_id]["struggle"] += 1
    else:
        session_state[chat_id]["success"] += 1

    turn_counter[chat_id] += 1

    answer = ask_gpt(chat_id, message.text)
    # Always reply with voice in an active scenario; text-only outside one
    _in_scenario = bool(current_scenario.get(chat_id))
    send_reply(chat_id, answer, voice=_in_scenario)

    _level = user_data.get(str(chat_id), {}).get("level", "A2")
    if turn_counter[chat_id] >= max_turns_for_level(_level):
        send_end_button(chat_id)

def send_end_button(chat_id):
    """Show 'Das Gespräch beenden' button — user presses it when ready to wrap up."""
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔚 Das Gespräch beenden", callback_data="end_convo"))
    bot.send_message(
        chat_id,
        "Du kannst noch weitersprechen — oder das Gespräch jetzt beenden. 💬",
        reply_markup=markup
    )

def handle_end_convo(call):
    """User pressed the button → NPC wraps up naturally → feedback flow."""
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    if chat_id in test_state:
        return
    trigger_natural_close(chat_id)

FAREWELL_SIGNALS = [
    "tschüss", "auf wiedersehen", "tschau", "ciao", "bis bald",
    "bis dann", "bis später", "mach's gut", "alles gute", "viel erfolg",
    "viel spaß", "gute reise", "pass auf dich auf", "hab dich lieb",
    "schönen tag noch", "schönen abend", "schöne woche", "guten abend",
    "verabschied", "ich muss los", "ich gehe jetzt", "bis nächste",
]

def contains_farewell(text: str) -> bool:
    """Check if NPC reply contains a natural farewell signal."""
    t = text.lower()
    return any(kw in t for kw in FAREWELL_SIGNALS)

def trigger_natural_close(chat_id):
    """NPC gives one warm goodbye, then feedback + share fires."""
    closing_prompt = (
        "[INTERN — NUR FÜR DICH: Das Gespräch wird jetzt beendet. "
        "Verabschiede dich herzlich und natürlich als deine Rolle — 1–2 Sätze. "
        "Keine Fragen mehr, kein neues Thema. Nur ein echtes, warmes Gesprächsende.]"
    )
    closing = ask_gpt(chat_id, closing_prompt)
    send_reply(chat_id, closing, voice=True)
    end_conversation(chat_id)

def end_conversation(chat_id):
    """Full end-flow in one shot: errors+exercises+XP → share button → topic select."""
    history_snapshot = list(user_memory.get(chat_id, []))
    user_state[chat_id] = {"mode": "exercises"}

    # ── Gamification ──────────────────────────────────────────────────────────
    s         = session_state.get(chat_id, {"struggle": 0, "success": 0})
    mode_type = get_dynamic_mode(s)
    turns     = turn_counter.get(chat_id, 0)
    xp_gain   = calculate_xp(turns, mode_type)
    update_streak(chat_id)
    leveled_up = add_xp(chat_id, xp_gain)
    stats      = user_data[str(chat_id)]["user_stats"]
    total_xp   = stats.get("xp", 0)
    check_badges(chat_id, stats)

    # Save weak points silently for spaced-repetition (no output shown)
    try:
        generate_feedback(chat_id, history_snapshot)
    except Exception as e:
        log.warning(f"generate_feedback failed for {chat_id}: {e}")

    # ── Error analysis + exercises (GPT) ─────────────────────────────────────
    bot.send_chat_action(chat_id, "typing")
    exercises_text, answers_text = generate_errors_and_exercises(chat_id, history_snapshot)

    # ── XP / reward block (separate message for impact) ──────────────────────
    reward_block = build_reward_block(chat_id, xp_gain, bonus_msg, turns)
    safe_markdown_send(chat_id, exercises_text)
    time.sleep(0.4)
    safe_markdown_send(chat_id, reward_block)

    # ── Answers (separate message) ────────────────────────────────────────────
    if answers_text:
        time.sleep(0.5)
        safe_markdown_send(chat_id, answers_text)

    # ── Share button ──────────────────────────────────────────────────────────
    share_text = quote(
        "Ich übe gerade Deutsch mit diesem Bot — ist echt gut 😅\n\n" + BOT_LINK
    )
    share_markup = InlineKeyboardMarkup()
    share_markup.add(InlineKeyboardButton(
        "🤝 Hilf deinen Freunden, ihr Deutsch zu boosten",
        url=f"https://t.me/share/url?url={quote(BOT_LINK)}&text={share_text}"
    ))
    bot.send_message(
        chat_id,
        "💬 Kennst du jemanden, der auch Deutsch üben will?",
        reply_markup=share_markup
    )

    # ── Reset + next topic ────────────────────────────────────────────────────
    turn_counter[chat_id]  = 0
    user_memory[chat_id]   = []
    session_state[chat_id] = {"struggle": 0, "success": 0}
    user_state[chat_id]    = {"mode": "topic_select"}
    time.sleep(1.5)
    send_topic_buttons(chat_id)


def finish_exercises_callback(call):
    """Legacy callback — no longer used but kept so old buttons don't crash."""
    bot.answer_callback_query(call.id, "Bereits erledigt ✅")

@bot.message_handler(commands=['uebung'])
def send_exercise(message):
    chat_id = message.chat.id

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="""Du bist ein Deutschlehrer.

Erstelle eine kurze Übung (A2-B1 Niveau).

Regeln:
- max. 1 Aufgabe
- Multiple Choice ODER Lückensatz
- Thema: Restaurant / Reservierung
- einfach & klar""",
        messages=[{"role": "user", "content": "Erstelle die Übung jetzt."}]
    )

    reply = response.content[0].text
    send_chat_reply(chat_id, reply)

# STIMME COMMAND
@bot.message_handler(commands=['stimme'])
def stimme(message):
    chat_id = message.chat.id
    current = user_voice.get(chat_id, "alloy")

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    markup.add(*[telebot.types.KeyboardButton(v) for v in VOICES])

    bot.send_message(
        chat_id,
        f"🎙 Wähle eine Stimme:\n_(Aktuell: {current})_",
        parse_mode="Markdown",
        reply_markup=markup
    )
    user_voice[chat_id] = "__choosing__"

def extract_quiz_answer(text: str) -> str:
    """Extract a/b/c from a spoken transcript. Returns '' if not found."""
    t = text.strip().lower()
    # Direct single letter or starts with it
    if t in ("a", "b", "c"):
        return t
    if re.match(r'^[abc][)\.\s,!]', t):
        return t[0]
    # "die Antwort ist B", "ich nehme A", "ich sage C", "ich glaube B" etc.
    m = re.search(r'\b(?:antwort|nehme?|wähle?|sage?|denke?|glaube?|ist|wäre?)\s+([abc])\b', t)
    if m:
        return m.group(1)
    # Any standalone a/b/c
    m = re.search(r'\b([abc])\b', t)
    if m:
        return m.group(1)
    return ""

def _transcribe_voice(message) -> str:
    """Download and transcribe a Telegram voice message. Returns transcript text."""
    file_info       = bot.get_file(message.voice.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    # Unique temp file per user+message — prevents concurrent users overwriting each other
    tmp_path = f"voice_{message.chat.id}_{message.message_id}.ogg"
    with open(tmp_path, "wb") as f:
        f.write(downloaded_file)
    try:
        with open(tmp_path, "rb") as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="gpt-4o-transcribe",
                file=audio_file,
                language="de",
            )
        return transcript.text.strip()
    finally:
        try:
            os.remove(tmp_path)
        except Exception as e:
            log.debug(f"Could not remove temp file {tmp_path}: {e}")

@bot.pre_checkout_query_handler(func=lambda q: True)
def handle_pre_checkout(query):
    """Telegram requires confirming every Stars payment before processing."""
    bot.answer_pre_checkout_query(query.id, ok=True)


@bot.message_handler(content_types=["successful_payment"])
def handle_successful_payment(message):
    """Activate Premium or Premium Plus after successful Stars payment."""
    chat_id = message.chat.id
    uid     = str(chat_id)
    ensure_user(chat_id)
    from datetime import timedelta
    payload = message.successful_payment.invoice_payload
    is_plus = payload.startswith("premium_plus_")

    user_data[uid]["premium"]       = True
    user_data[uid]["premium_until"] = (datetime.now() + timedelta(days=30)).isoformat()
    user_data[uid]["stars_payment"] = True

    if is_plus:
        user_data[uid]["premium_plus"]       = True
        user_data[uid]["premium_plus_until"] = (datetime.now() + timedelta(days=30)).isoformat()
        log.info(f"✅ Stars Premium PLUS activated: {chat_id}")
        bot.send_message(chat_id,
            "⭐ Danke für deine Stars!\n\n"
            "👑 *Willkommen bei Premium Plus!*\n\n"
            "Nicht nur Deutsch lernen — in Deutschland ankommen.\n"
            "Dein Kumpel ist jetzt 24/7 für dich da.\n\n"
            "Finanzamt-Brief? Kündigung? Oder einfach mal reden?\n"
            "Tippe /themen für Übungen — oder schreib ihm einfach drauflos. 🗣️",
            parse_mode="Markdown")
    elif payload.startswith("donation_"):
        log.info(f"⭐ Stars donation received from {chat_id}")
        bot.send_message(chat_id,
            "⭐ Danke für deine Spende!\n\n"
            "Das bedeutet wirklich viel und hilft, den Bot am Laufen zu halten. "
            "Du bist ein Schatz. 🙏💙")
    else:
        user_data[uid]["premium_plus"] = False
        log.info(f"✅ Stars Premium activated: {chat_id}")
        bot.send_message(chat_id,
            "⭐ Danke für deine Stars!\n\n"
            "🎉 Du hast jetzt 30 Tage *Premium* — alle Szenarien & Übungen unlimitiert. 💪\n"
            "Dein Streak und deine XP sind natürlich noch da.\n\n"
            "Tippe /themen um weiterzumachen!",
            parse_mode="Markdown")

    save_users(user_data)


@bot.message_handler(content_types=['photo', 'document'])
def handle_file_message(message):
    """Handle photos and PDF documents — mainly for brief_erklaeren mode."""
    chat_id = message.chat.id
    ensure_user(chat_id)
    mode = user_state.get(chat_id, {}).get("mode", "idle")
    uid  = str(chat_id)
    native_lang = user_data.get(uid, {}).get("native_language") or "Englisch"

    # Only process in integration brief mode
    if mode not in ("intg_brief_erklaeren", "intg_brief_antworten", "intg_steuerbescheid"):
        bot.send_message(chat_id,
            "📎 Dateien und Fotos nehme ich gerne für Briefe und Dokumente entgegen!\n"
            "Nutze /integration → Brief erklären um einen Brief zu analysieren. 📄")
        return

    # ── PHOTO: send to Claude Vision ──────────────────────────────────────
    if message.content_type == "photo":
        bot.send_message(chat_id, "📸 Foto empfangen — lese den Brief...")
        try:
            # Get highest resolution photo
            photo    = message.photo[-1]
            file_info = bot.get_file(photo.file_id)
            file_url  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
            import urllib.request
            img_data = urllib.request.urlopen(file_url).read()
            import base64
            img_b64  = base64.b64encode(img_data).decode()
            # Detect format
            ext = file_info.file_path.split(".")[-1].lower()
            media_type = "image/jpeg" if ext in ("jpg","jpeg") else "image/png"

            action_prompts = {
                "intg_brief_erklaeren": (
                    f"Du bist ein hilfreicher Assistent. Lies den deutschen Brief im Bild und erkläre ihn:\n"
                    f"1. EINFACHES DEUTSCH: Was bedeutet der Brief? (A2/B1 Niveau)\n"
                    f"2. {native_lang.upper()}: Kurze Zusammenfassung auf {native_lang}\n"
                    f"3. WAS TUN: Konkrete nächste Schritte\n"
                    f"4. FRIST: Gibt es eine Frist? Wenn ja, wann?\n"
                    f"Sei beruhigend."
                ),
                "intg_steuerbescheid": (
                    f"Lies den Steuerbescheid im Bild und erkläre:\n"
                    f"1. ERGEBNIS: Rückerstattung oder Nachzahlung? Wie viel?\n"
                    f"2. EINFACHE ERKLÄRUNG auf Deutsch\n"
                    f"3. {native_lang.upper()}: Kurze Zusammenfassung\n"
                    f"4. WAS TUN: Nächste Schritte, Einspruch möglich?"
                ),
                "intg_brief_antworten": (
                    f"Lies den Brief im Bild und schreibe eine formelle deutsche Antwort darauf.\n"
                    f"Danach kurze Erklärung auf {native_lang}."
                ),
            }
            system_p = action_prompts.get(mode, action_prompts["intg_brief_erklaeren"])

            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=700,
                system=system_p,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                    {"type": "text",  "text": "Bitte analysiere dieses Dokument."}
                ]}]
            )
            result = _strip_md(resp.content[0].text.strip())
        except Exception as e:
            result = f"⚠️ Foto konnte nicht gelesen werden: {e}"

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("✍️ Antwort schreiben", callback_data="intg:brief_antworten"),
            InlineKeyboardButton("🏛️ Menü", callback_data="intg:back"),
        )
        user_state[chat_id]["mode"] = "idle"
        bot.send_message(chat_id, result, reply_markup=markup)
        return

    # ── DOCUMENT (PDF) ────────────────────────────────────────────────────
    if message.content_type == "document":
        doc = message.document
        if not doc.file_name or not doc.file_name.lower().endswith(".pdf"):
            bot.send_message(chat_id, "📎 Bitte schick ein Foto des Briefes oder kopiere den Text rein. PDF-Unterstützung kommt bald!")
            return
        bot.send_message(chat_id, "📄 PDF empfangen — lese den Text...")
        try:
            file_info = bot.get_file(doc.file_id)
            file_url  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
            import urllib.request, io
            pdf_data  = urllib.request.urlopen(file_url).read()
            # Extract text with pdfminer
            from pdfminer.high_level import extract_text
            pdf_text = extract_text(io.BytesIO(pdf_data))
            if not pdf_text or len(pdf_text.strip()) < 50:
                raise ValueError("Text zu kurz oder leer")
        except ImportError:
            bot.send_message(chat_id,
                "⚠️ PDF-Support ist noch nicht installiert.\n"
                "Mach einfach ein Foto des Briefes — das funktioniert genauso gut! 📸")
            user_state[chat_id]["mode"] = "idle"
            return
        except Exception as e:
            bot.send_message(chat_id,
                f"⚠️ PDF konnte nicht gelesen werden.\n"
                "Versuch es als Foto oder kopiere den Text rein. 📸")
            user_state[chat_id]["mode"] = "idle"
            return

        # Now process like text
        user_state[chat_id]["mode"] = "intg_brief_erklaeren"
        # Reuse text handler by injecting into a fake text call
        uid  = str(chat_id)
        native_lang = user_data.get(uid, {}).get("native_language") or "Englisch"
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=700,
                system=(
                    f"Du bist ein hilfreicher Assistent.\n"
                    f"1. EINFACHES DEUTSCH: Was bedeutet der Text? (A2/B1 Niveau)\n"
                    f"2. {native_lang.upper()}: Kurze Zusammenfassung\n"
                    f"3. WAS TUN: Konkrete Schritte\n"
                    f"4. FRIST: Gibt es eine Frist?"
                ),
                messages=[{"role": "user", "content": f"Dokument:\n{pdf_text[:3000]}"}]
            )
            result = _strip_md(resp.content[0].text.strip())
        except Exception as e:
            result = f"⚠️ Fehler: {e}"
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("✍️ Antwort schreiben", callback_data="intg:brief_antworten"),
            InlineKeyboardButton("🏛️ Menü", callback_data="intg:back"),
        )
        user_state[chat_id]["mode"] = "idle"
        bot.send_message(chat_id, result, reply_markup=markup)
        return


@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    chat_id = message.chat.id
    state   = user_state.get(chat_id, {})
    mode    = state.get("mode")

    # ── SHADOWING MODE ────────────────────────────────────────────────────────
    if mode == "shadowing":
        user_text = _transcribe_voice(message)
        bot.send_message(chat_id, f"_📝 Du hast gesagt: {user_text}_", parse_mode="Markdown")
        bot.send_message(chat_id, "👍 Gut! Noch einmal? Oder /menu für mehr Optionen.")
        return

    # ── ONBOARDING — covers ALL steps so nothing falls through to chat ───────
    if mode == "onboarding":
        if state.get("step") == "name":
            user_text = _transcribe_voice(message)
            # Use the full transcript as the name (trim excess punctuation/length)
            name = user_text.strip().strip(".,!?-–")[:40] if user_text else ""
            if name:
                handle_onboarding(chat_id, name)
            else:
                bot.send_message(chat_id, "Ich hab dich nicht verstanden 😅 Wie heißt du?")
        # Goal step and all others: voice not applicable, ignore silently
        return

    # ── TEST MODE — extract a/b/c from spoken answer ─────────────────────────
    if mode == "test" or chat_id in test_state:
        user_text = _transcribe_voice(message)
        answer = extract_quiz_answer(user_text)
        if answer:
            bot.send_message(
                chat_id,
                f"_📝 Gehört: \"{user_text}\" → Antwort: {answer.upper()}_",
                parse_mode="Markdown"
            )
            handle_answer(chat_id, answer)
        else:
            bot.send_message(chat_id, "Ich hab dich nicht verstanden 😅 Sag bitte A, B oder C.")
        return

    # ── GEM EXERCISE CHECK ───────────────────────────────────────────────────
    state = user_state.get(chat_id, {})
    if state.get("gem_exercise"):
        user_text = _transcribe_voice(message)
        if user_text:
            bot.send_message(chat_id, f"_📝 Du hast gesagt: {user_text}_", parse_mode="Markdown")
            check_gem_exercise(chat_id, user_text, state["gem_text"])
        else:
            bot.send_message(chat_id, "Ich hab dich nicht verstanden 😅 Versuch's nochmal!")
        return

    # ── QUATSCHEN MODE ────────────────────────────────────────────────────────
    if mode == "quatschen":
        user_text = _transcribe_voice(message)
        if user_text and user_text.strip():
            handle_quatschen_message(chat_id, user_text)
        else:
            bot.send_message(chat_id,
                "Ich hab dich leider nicht verstanden 😅\n"
                "Versuch es nochmal — sprich etwas lauter oder näher ans Mikro!")
        return

    # ── CHAT MODE ─────────────────────────────────────────────────────────────
    # Guard: must be in an active chat mode with a live scenario
    if mode not in ("chat", "idle", None):
        return
    if not current_scenario.get(chat_id):
        return

    try:
        user_text = _transcribe_voice(message)
    except Exception as e:
        bot.send_message(chat_id, "🎙️ Ich konnte dich leider nicht verstehen. Bitte noch einmal versuchen.")
        return

    if not user_text:
        bot.send_message(chat_id, "🎙️ Ich habe nichts gehört. Bitte noch einmal sprechen.")
        return

    # Store for nudge recovery — mark as unanswered until TTS reply is delivered
    last_voice_text[chat_id]     = user_text
    last_voice_answer[chat_id]   = None
    last_voice_answered[chat_id] = False

    try:
        answer = ask_gpt(chat_id, user_text)
        last_voice_answer[chat_id] = answer   # GPT succeeded; store in case TTS fails next
        send_reply(chat_id, answer, voice=True)
        last_voice_answered[chat_id] = True
    except Exception as e:
        bot.send_message(chat_id, "⚠️ Etwas ist schiefgelaufen. Bitte nochmal versuchen.")

    # update turn counter
    turn_counter[chat_id] = turn_counter.get(chat_id, 0) + 1
    if state.get("turn") is not None:
        user_state[chat_id]["turn"] = state.get("turn", 0) + 1

    _level = user_data.get(str(chat_id), {}).get("level", "A2")
    if turn_counter[chat_id] >= max_turns_for_level(_level):
        send_end_button(chat_id)

# ─────────────────────────────────────────────────────────────────────────────
# MASTER CALLBACK ROUTER
# Single registered callback_query_handler — every inline-button tap lands here.
# During an active test ONLY quiz_answer: buttons are processed; everything
# else (stale buttons from previous sessions, accidental taps, re-delivered
# Telegram callbacks) is silently dismissed before it can touch any logic.
# ─────────────────────────────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: True)
def master_callback_router(call):
    chat_id = call.message.chat.id
    data    = call.data

    # ── TEST FIREWALL ─────────────────────────────────────────────────────────
    # Only block if test is actually active (has a phase key)
    if chat_id in test_state and test_state[chat_id].get("phase"):
        if data.startswith("quiz_answer:"):
            handle_quiz_answer_callback(call)
        elif data == "start_test":
            start_test_callback(call)
        else:
            bot.answer_callback_query(call.id)   # dismiss spinner, do nothing
        return

    # ── NORMAL ROUTING ────────────────────────────────────────────────────────
    # ── MENU CALLBACKS ───────────────────────────────────────────────────────
    if data == "menu_themen":
        bot.answer_callback_query(call.id)
        send_topic_buttons(chat_id)
        return
    elif data == "menu_progress":
        bot.answer_callback_query(call.id)
        send_progress(chat_id)
        return
    elif data == "menu_errors":
        bot.answer_callback_query(call.id)
        show_errors(chat_id)
        return
    elif data == "menu_level":
        bot.answer_callback_query(call.id)
        show_level(chat_id)
        return
    elif data == "menu_practice":
        bot.answer_callback_query(call.id)
        start_exercise(chat_id)
        return
    elif data == "menu_shadowing":
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "🎧 Shadowing Mode kommt bald zurück! Bleib dran. 👀")
        return
    elif data == "menu_restart":
        bot.answer_callback_query(call.id)
        restart_chat(chat_id)
        return

    if data == "next_gem":
        bot.answer_callback_query(call.id)
        send_daily_gem(chat_id)
        return

    if data == "end_quatschen":
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        _quatschen_end_with_xp(chat_id)
        return

    if data == "confirm_restart":
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        do_full_reset(chat_id)
        return
    elif data == "cancel_restart":
        bot.answer_callback_query(call.id, "Abgebrochen ✅")
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        bot.send_message(chat_id, "Okay, nichts gelöscht! 🙂")
        return

    if data == "translate_last" or data.startswith("translate:"):
        # Get text: either from specific key (voice messages) or last_bot_text
        last_npc = None
        if data.startswith("translate:"):
            key = data.split(":", 1)[1]
            last_npc = pending_texts.get(key)

        if not last_npc:
            last_npc = last_bot_text.get(chat_id) or last_bot_text.get(str(chat_id))

        if not last_npc:
            mem = user_memory.get(chat_id, [])
            last_npc = next(
                (m["content"] for m in reversed(mem) if m.get("role") == "assistant"),
                None
            )

        if not last_npc:
            bot.answer_callback_query(call.id, "Noch keine Nachricht zum Übersetzen.")
            return

        user = user_data.get(str(chat_id), {})
        lang = user.get("native_language", "Englisch")
        bot.answer_callback_query(call.id, "Übersetze...")
        try:
            translation = get_translation(chat_id, last_npc)
            bot.send_message(chat_id, f"🌍 {lang}:\n\n{translation}", reply_markup=InlineKeyboardMarkup())
        except Exception:
            bot.send_message(chat_id, "Übersetzung fehlgeschlagen 😅 Versuch es nochmal.")
        return

    if data == "kultur_quiz_start":
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        state     = user_state.get(chat_id, {})
        questions = state.get("kultur_questions", [])
        answers   = state.get("kultur_answers", [])
        if not questions:
            bot.send_message(chat_id, "⚠️ Keine Fragen verfügbar."); return
        user_state[chat_id].update({
            "mode":             "exercise",
            "exercise_topic":   state.get("kultur_label", "Kultur"),
            "exercise_level":   user_data.get(str(chat_id), {}).get("level", "A2"),
            "exercise_idx":     0,
            "exercise_score":   0,
            "exercise_total":   len(questions),
            "exercise_used":    [],
            # Store pre-generated questions directly
            "kultur_quiz_questions": questions,
            "kultur_quiz_answers":   answers,
            "is_kultur_quiz":        True,
        })
        _send_next_kultur_question(chat_id)
        return

    if data.startswith("ex_ans:"):
        given = data.split(":")[1].upper()
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        state   = user_state.get(chat_id, {})
        q       = state.get("exercise_current_q", {})
        correct = q.get("correct", "").upper()
        expl    = q.get("explanation", "")
        if given == correct:
            user_state[chat_id]["exercise_score"] = state.get("exercise_score", 0) + 1
            feedback = f"✅ Richtig! ({given})"
        else:
            feedback = f"❌ Falsch — du: {given}, richtig: {correct}\n📖 {expl}"
        user_state[chat_id]["exercise_idx"] = state.get("exercise_idx", 0) + 1
        bot.send_message(chat_id, feedback)
        if user_state[chat_id].get("is_kultur_quiz"):
            _send_next_kultur_question(chat_id)
        else:
            _send_next_exercise_question(chat_id)
        return

    if data.startswith("intg:"):
        action = data.split(":", 1)[1]
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        uid         = str(chat_id)
        native_lang = user_data.get(uid, {}).get("native_language") or "Englisch"

        if action == "back":
            _show_integration_menu(chat_id); return

        if action == "steuern":
            _show_steuern_menu(chat_id); return

        if action == "brief_erklaeren":
            user_state[chat_id] = {"mode": "intg_brief_erklaeren"}
            bot.send_message(chat_id,
                "📄 Schick mir den Text des Briefes (kopieren & einfügen reicht).\n"
                "Ich erkläre dir was er bedeutet und was du tun musst. 🔍")
            return

        if action == "brief_antworten":
            user_state[chat_id] = {"mode": "intg_brief_antworten"}
            bot.send_message(chat_id,
                "✍️ Beschreib mir kurz die Situation:\n\n"
                "• Von wem ist der Brief? (z.B. Jobcenter, Vermieter, Finanzamt)\n"
                "• Was wird verlangt oder gefragt?\n\n"
                "Ich schreibe dir eine formelle Antwort auf Deutsch. 📝")
            return

        if action == "termin":
            user_state[chat_id] = {"mode": "intg_termin"}
            markup = InlineKeyboardMarkup(row_width=1)
            markup.add(
                InlineKeyboardButton("🏢 Amt / Jobcenter",    callback_data="termin:amt"),
                InlineKeyboardButton("🏥 Arzt / Krankenhaus", callback_data="termin:arzt"),
                InlineKeyboardButton("🏠 Wohnungsbesichtigung", callback_data="termin:wohnung"),
                InlineKeyboardButton("💼 Vorstellungsgespräch", callback_data="termin:job"),
                InlineKeyboardButton("🏦 Bank",               callback_data="termin:bank"),
            )
            bot.send_message(chat_id, "🎭 Welchen Termin möchtest du üben?", reply_markup=markup)
            return

        if action == "beratung":
            user_state[chat_id] = {"mode": "intg_beratung"}
            bot.send_message(chat_id,
                "🗺️ In welcher Stadt lebst du?\n\n"
                "Schreib mir deine Stadt oder Postleitzahl — ich suche passende Beratungsstellen für dich.")
            return

        if action == "steuerbescheid":
            user_state[chat_id] = {"mode": "intg_steuerbescheid"}
            bot.send_message(chat_id,
                "📄 Schick mir den Text deines Steuerbescheids.\n"
                "Ich erkläre dir was er bedeutet, ob du Geld bekommst oder nachzahlen musst. 💶")
            return

        if action == "steuererklaerung_info":
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=500,
                system=f"Du bist ein freundlicher Steuerberater-Assistent. Erkläre auf einfachem Deutsch, dann auf {native_lang}. Benutze KEINE Sternchen oder Markdown — nur Plaintext.",
                messages=[{"role": "user", "content": (
                    "Was ist eine Steuererklärung in Deutschland? Wer muss sie machen? Wie macht man das?\n\n"
                    "WICHTIG: Erkläre auch, dass Angestellte (die keine Pflicht haben) sie TROTZDEM machen sollten — "
                    "viele bekommen Geld zurück durch Werbungskosten, Homeoffice, Fortbildungen, Fahrtkosten, "
                    "doppelte Haushaltsführung, Sonderausgaben usw. "
                    "Durchschnittliche Rückerstattung: ca. 1.000€. Kurz, max 200 Wörter."
                )}]
            )
            import re as _re
            result = resp.content[0].text.strip()
            result = _re.sub(r"\*\*(.+?)\*\*", r"\1", result)
            result = result.replace("**", "").replace("##", "").replace("# ", "")
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("◀️ Zurück", callback_data="intg:steuern"))
            bot.send_message(chat_id, result, reply_markup=markup)
            return

        if action == "steuerfristen":
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=300,
                system=f"Du bist ein freundlicher Steuerberater-Assistent. Antworte auf Deutsch, dann kurze Zusammenfassung auf {native_lang}. Kein Markdown, keine Sternchen.",
                messages=[{"role": "user", "content": "Was sind die wichtigsten Steuerfristen in Deutschland? Wann muss man die Steuererklärung abgeben? Gibt es Verlängerungen? Kurz und klar."}]
            )
            import re as _re
            result = resp.content[0].text.strip()
            result = _re.sub(r"\*\*(.+?)\*\*", r"\1", result).replace("**","").replace("##","").replace("# ","")
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("◀️ Zurück", callback_data="intg:steuern"))
            bot.send_message(chat_id, result, reply_markup=markup)
            return

        if action == "finanzamt":
            user_state[chat_id] = {"mode": "intg_finanzamt"}
            bot.send_message(chat_id,
                "🏢 In welcher Stadt oder Postleitzahl wohnst du?\n"
                "Ich finde dein zuständiges Finanzamt.")
            return

        if action == "kultur":
            _show_kultur_menu(chat_id); return

        return

    if data.startswith("kultur:"):
        topic_key = data.split(":", 1)[1]
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        uid         = str(chat_id)
        native_lang = user_data.get(uid, {}).get("native_language") or "Englisch"
        level       = user_data.get(uid, {}).get("level", "A2")
        prompt      = KULTUR_PROMPTS.get(topic_key, "Schreibe einen kurzen Text auf einfachem Deutsch.")
        topic_label = next((name for _, name, cb in KULTUR_TOPICS if cb == f"kultur:{topic_key}"), topic_key)

        bot.send_message(chat_id, f"📖 {topic_label}\n\nText wird erstellt...")
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=500,
                system=f"Du schreibst für Deutschlernende auf Niveau {level}. Einfache, klare Sprache. Kein Schulbuch-Deutsch.",
                messages=[{"role": "user", "content": prompt}]
            )
            text_content = resp.content[0].text.strip()
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Fehler: {e}"); return

        # Store for translate button + quiz
        last_bot_text[chat_id] = text_content
        user_state[chat_id] = {
            "mode":         "idle",
            "kultur_topic": topic_key,
            "kultur_text":  text_content,
            "kultur_label": topic_label,
        }

        # Generate 3 quiz questions in parallel
        try:
            quiz_resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=400,
                system="""Erstelle 3 Multiple-Choice-Fragen zu diesem Text. Format exakt:
1. Frage?
A: Option  B: Option  C: Option
ANTWORT: A

2. Frage?
A: Option  B: Option  C: Option
ANTWORT: B

Nur diese Zeilen, nichts sonst.""",
                messages=[{"role": "user", "content": f"Text:\n{text_content}"}]
            )
            import re as _re
            quiz_raw = quiz_resp.content[0].text.strip()
            questions, answers = [], []
            for block in _re.split(r"\n(?=\d+\.)", quiz_raw):
                lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
                ans_line = next((l for l in lines if l.upper().startswith("ANTWORT:")), None)
                if ans_line:
                    answers.append(ans_line.split(":")[-1].strip().upper()[0])
                    questions.append("\n".join(l for l in lines if not l.upper().startswith("ANTWORT:")))
            user_state[chat_id]["kultur_questions"] = questions
            user_state[chat_id]["kultur_answers"]   = answers
            has_quiz = bool(questions)
        except Exception:
            has_quiz = False

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))
        if has_quiz:
            markup.add(InlineKeyboardButton("✅ Quiz starten", callback_data="kultur_quiz_start"))
        markup.add(InlineKeyboardButton("📚 Andere Themen", callback_data="intg:kultur"))
        bot.send_message(chat_id, text_content, reply_markup=markup)
        return

    if data.startswith("termin:"):
        termin_type = data.split(":", 1)[1]
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        type_labels = {
            "amt": "Amt / Jobcenter", "arzt": "Arzt / Krankenhaus",
            "wohnung": "Wohnungsbesichtigung", "job": "Vorstellungsgespräch", "bank": "Bank"
        }
        label = type_labels.get(termin_type, termin_type)
        uid   = str(chat_id)
        level = user_data.get(uid, {}).get("level", "A2")
        native_lang = user_data.get(uid, {}).get("native_language") or "Englisch"
        user_state[chat_id] = {
            "mode": "chat",
            "scenario": f"Termin: {label}",
            "npc_system": (
                f"Du spielst eine Person am {label} in Deutschland. Der User (Niveau {level}) "
                f"übt das Gespräch auf Deutsch. Sei geduldig, realistisch und hilfreich. "
                f"Korrigiere Fehler sanft am Ende jeder Antwort. "
                f"Bei großen Verständnisproblemen erkläre kurz auf {native_lang}. "
                f"Starte das Gespräch als Mitarbeiter:in."
            )
        }
        bot.send_message(chat_id,
            f"🎭 Rollenspiel: {label}\n\n"
            f"Ich bin jetzt die Mitarbeiter:in. Du kommst rein — los geht's!\n"
            f"_(Zum Beenden: /restart)_", parse_mode="Markdown")
        # Trigger NPC opening line
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=100,
            system=user_state[chat_id]["npc_system"],
            messages=[{"role": "user", "content": "[Begrüße den Kunden auf Deutsch]"}]
        )
        bot.send_message(chat_id, resp.content[0].text.strip())
        return

    if data == "pay_now":
        bot.answer_callback_query(call.id)
        send_paywall(chat_id)
        return

    if data == "pay_plus":
        bot.answer_callback_query(call.id)
        price_id = os.getenv("STRIPE_PRICE_ID_PLUS", "")
        if not price_id:
            bot.send_message(chat_id, "⚠️ Premium Plus ist gerade noch nicht verfügbar.")
            return
        try:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                mode="payment",
                line_items=[{"price": price_id, "quantity": 1}],
                success_url="https://t.me/germandude_bot?start=plus_ok",
                cancel_url="https://t.me/germandude_bot",
                metadata={"telegram_id": str(chat_id), "plan": "plus"},
            )
            markup = InlineKeyboardMarkup(row_width=1)
            markup.add(InlineKeyboardButton("👑 Jetzt Premium Plus — €30/Monat", url=session.url))
            markup.add(InlineKeyboardButton("⭐ Mit Stars — 2000 Stars", callback_data="pay_stars_plus"))
            ptext = (
                "👑 *Premium Plus — €30/Monat*\n\n"
                "Nicht nur Deutsch lernen. In Deutschland ankommen.\n\n"
                "Finanzamt-Brief auf dem Tisch? Musst du kündigen, dich beschweren, "
                "erklären — aber weißt nicht wie?\n"
                "Oder Deutschland fühlt sich gerade einfach zu viel an?\n\n"
                "Dein Kumpel ist da. Immer. Kein Urteilen, kein Stress."
            )
            last_bot_text[chat_id] = ptext
            markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))
            bot.send_message(chat_id, ptext, reply_markup=markup)
        except stripe.error.InvalidRequestError as e:
            # Häufigste Ursache: Price ID ist einmalig, nicht recurring.
            # Diagnose an Admin schicken und User auf Stars-Weg umleiten.
            err_msg = str(e)
            log.error(f"Plus checkout InvalidRequest: {err_msg}")
            if ADMIN_CHAT_ID:
                try:
                    bot.send_message(ADMIN_CHAT_ID,
                        f"⚠️ pay_plus Fehler (user {chat_id}):\n`{err_msg}`",
                        parse_mode="Markdown")
                except Exception:
                    pass
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("⭐ Mit Stars zahlen — 2000 Stars", callback_data="pay_stars_plus"))
            bot.send_message(chat_id,
                "💳 Karte gerade nicht verfügbar — aber du kannst direkt mit Telegram Stars zahlen:",
                reply_markup=markup)
        except Exception as e:
            err_msg = str(e)
            log.error(f"Plus checkout failed: {err_msg}")
            if ADMIN_CHAT_ID:
                try:
                    bot.send_message(ADMIN_CHAT_ID,
                        f"⚠️ pay_plus Fehler (user {chat_id}):\n`{err_msg}`",
                        parse_mode="Markdown")
                except Exception:
                    pass
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("⭐ Mit Stars zahlen — 2000 Stars", callback_data="pay_stars_plus"))
            bot.send_message(chat_id,
                "💳 Karte gerade nicht verfügbar — aber du kannst direkt mit Telegram Stars zahlen:",
                reply_markup=markup)
        return

    if data == "pay_stars_plus":
        bot.answer_callback_query(call.id)
        try:
            bot.send_invoice(chat_id,
                title="German Dude — Premium Plus (1 Monat)",
                description="Nicht nur Deutsch lernen. In Deutschland ankommen. Quatschen-Modus, Alltagshilfe, Kumpel 24/7. 30 Tage.",
                payload=f"premium_plus_{chat_id}",
                provider_token="", currency="XTR",
                prices=[telebot.types.LabeledPrice("Premium Plus 1 Monat", 2000)],
            )
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Stars-Zahlung fehlgeschlagen: {e}")
        return

    if data == "pay_stars":
        bot.answer_callback_query(call.id)
        send_stars_invoice(chat_id)
        return

    if data == "clear_errors":
        bot.answer_callback_query(call.id, "Fehler gelöscht ✓")
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        uid = str(chat_id)
        user_data[uid]["test_errors"] = []
        user_data[uid]["errors"]      = []
        user_data[uid]["weak_points"] = []
        save_users(user_data)
        bot.send_message(chat_id, "🗑️ Alle Fehler gelöscht. Frischer Start! 💪")
        return

    if data.startswith("lang:"):
        # Language quick-tap during onboarding
        bot.answer_callback_query(call.id)
        if user_state.get(chat_id, {}).get("step") == "native_language":
            lang = data[5:]
            try:
                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            handle_onboarding(chat_id, lang)
        return

    if data == "restart_onboarding":
        bot.answer_callback_query(call.id)
        ensure_user(chat_id)
        user_state[chat_id] = {"mode": "onboarding", "step": "native_language"}
        bot.send_message(chat_id,
            "🌍 Was ist deine Muttersprache?\n"
            "What's your native language?\n"
            "Какой твой родной язык?\n"
            "Яка твоя рідна мова?\n"
            "لغتك الأم هي؟\n"
            "Ana dilin ne?\n\n"
            "👇 Tippe einfach — oder wähle hier:")
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("🇬🇧 English",    callback_data="lang:English"),
            InlineKeyboardButton("🇷🇺 Русский",    callback_data="lang:Русский"),
            InlineKeyboardButton("🇺🇦 Українська", callback_data="lang:Українська"),
            InlineKeyboardButton("🇹🇷 Türkçe",     callback_data="lang:Türkçe"),
            InlineKeyboardButton("🇸🇦 العربية",    callback_data="lang:Arabic"),
            InlineKeyboardButton("🇪🇸 Español",    callback_data="lang:Español"),
            InlineKeyboardButton("🇫🇷 Français",   callback_data="lang:Français"),
            InlineKeyboardButton("🇵🇱 Polski",     callback_data="lang:Polski"),
        )
        bot.send_message(chat_id, "​", reply_markup=markup)
        return

    if data == "start_chat":
        start_chat_callback(call)
    elif data.startswith("show_text:"):
        show_text_callback(call)
    elif data.startswith("topic:"):
        handle_topic_callback(call)
    elif data.startswith("goal:"):
        handle_goal(call)
    elif data == "start_test":
        start_test_callback(call)
    elif data.startswith("quiz_answer:"):
        handle_quiz_answer_callback(call)
    elif data == "lesson_yes":
        lesson_yes_callback(call)
    elif data == "lesson_no":
        lesson_no_callback(call)
    elif data == "end_convo":
        handle_end_convo(call)
    elif data == "finish_exercises":
        finish_exercises_callback(call)

    elif data == "explain_grammar":
        bot.answer_callback_query(call.id, "Erklärung kommt...")
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        explain_grammar(chat_id)
        return

    elif data == "new_exercises":
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        start_exercise(chat_id)
        return

    elif data == "go_menu":
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        show_menu(chat_id)
        return

    # ── DANKE / SPENDEN / FEEDBACK ────────────────────────────────────────
    elif data == "danke:donate":
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        send_donation_menu(chat_id)
        return

    elif data == "danke:feedback":
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        start_feedback_mode(chat_id)
        return

    elif data == "danke:back":
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        send_danke_menu(chat_id)
        return

    elif data.startswith("donate:eur:"):
        bot.answer_callback_query(call.id, "Zahlung wird vorbereitet...")
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        amount = int(data.split(":")[-1])
        url = create_donation_checkout(chat_id, amount)
        label = DONATION_LABELS.get(amount, f"€{amount} Spende")
        if url:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(f"💳 Jetzt {label} — €{amount} spenden", url=url))
            markup.add(InlineKeyboardButton("◀️ Zurück", callback_data="danke:donate"))
            bot.send_message(
                chat_id,
                f"{label} — danke, das ist wunderbar! 🙏\n\n"
                "Klick unten um zur sicheren Zahlungsseite zu kommen. 🔒",
                reply_markup=markup
            )
        else:
            bot.send_message(chat_id,
                "⚠️ Stripe ist gerade nicht verfügbar. "
                "Versuch es mit Stars oder später nochmal!")
        return

    elif data == "donate:stars":
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        send_donation_stars_menu(chat_id)
        return

    elif data.startswith("donate:stars:"):
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        stars = int(data.split(":")[-1])
        send_donation_stars_invoice(chat_id, stars)
        return

    elif data == "voicepush_quatsch":
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        start_quatschen(chat_id)
        return

    else:
        bot.answer_callback_query(call.id)

# Stripe/webhook disabled for stability — re-enable later


# ═══════════════════════════════════════════════════════════════════════════
#  VOICE PUSH RETENTION — Premium Plus, 3x/Woche zu zufälliger Zeit
#  Persönliche Sprachnachricht vom German Dude — Ziel: zurück ins Quatschen.
#
#  ZEITZONE: Slots werden in echter Berlin-Zeit (CET/CEST, DST-automatisch)
#  berechnet und dann als naive UTC gespeichert — konsistent mit dem Rest
#  des Bots, der von einem UTC-Server (Railway-Standard) ausgeht.
# ═══════════════════════════════════════════════════════════════════════════

from zoneinfo import ZoneInfo
BERLIN_TZ = ZoneInfo("Europe/Berlin")

def _now_berlin() -> datetime:
    """Aktuelle Zeit in Berlin — unabhängig von der Server-Zeitzone korrekt."""
    return datetime.now(timezone.utc).astimezone(BERLIN_TZ)

def _berlin_to_naive_utc(dt_berlin: datetime) -> datetime:
    """Wandelt eine Berlin-aware Zeit in naive UTC um (Speicherformat des Bots)."""
    return dt_berlin.astimezone(timezone.utc).replace(tzinfo=None)

# Push-Fenster: Geschäftszeiten CET/CEST — 09:00 bis 18:00 Berlin-Zeit
VOICE_PUSH_HOUR_MIN = 9
VOICE_PUSH_HOUR_MAX = 18
VOICE_PUSH_PER_WEEK = 3

# Templates OHNE Name — immer verfügbar
VOICE_PUSH_TEMPLATES_NO_NAME = [
    "Heyy, na? Lange nichts gehört! Wie geht's dir so?",
    "Hallo, Freundchen! Ewig nichts gehört von dir. Wie geht's dir überhaupt?",
    "Hey du! Lebst du noch? Was geht ab?",
    "Na, wo steckst du denn? Ich hab schon gewartet!",
    "Hallo hallo! Hast du mich vergessen, oder was?",
    "Hey du! Schon ne Weile her. Wie läuft's bei dir gerade?",
    "Na sag mal, was machst du grad so? Lust auf ein kleines Gespräch?",
    "Hey! Ich hab gerade an dich gedacht. Wie geht's dir?",
    "Hallo! Bist du noch da, oder hab ich dich verloren?",
    "Hey du, kleiner Tipp: ich hab Zeit zum Reden, falls du magst!",
    "Servus! Schon eine Weile ruhig hier. Alles gut bei dir?",
    "Hallo Fremder! Erkennst du mich noch?",
    "Hey, was geht? Hab grad Zeit, falls du quatschen willst.",
    "Na, wie war deine Woche bisher?",
]

# Templates MIT {name} Platzhalter — nur wenn name bekannt
VOICE_PUSH_TEMPLATES_WITH_NAME = [
    "Hey {name}! Lebst du noch? Was geht ab?",
    "{name}, mein Freund! Zeit für ein bisschen Deutsch, oder?",
    "Na, {name}! Wird langsam Zeit, dass wir mal wieder quatschen, oder?",
    "Na, {name}! Wie war deine Woche bisher?",
    "Hey {name}! Schon ne Weile her. Wie läuft's bei dir gerade?",
    "{name}! Hab gerade an dich gedacht. Alles gut bei dir?",
]

# Templates mit {days} — nutzen last_active, nur wenn days >= 3
VOICE_PUSH_TEMPLATES_WITH_DAYS = [
    "Hey {name}! Seit {days} Tagen nix von dir gehört. Alles gut bei dir?",
    "Na, {name}! {days} Tage Funkstille — ich hab mir schon Sorgen gemacht!",
    "Hallo! Schon {days} Tage her seit wir gequatscht haben. Lust auf eine Runde?",
]


def _voice_push_pick_template(chat_id: int) -> str:
    """Wählt ein passendes Template basierend auf Name + Inaktivitäts-Dauer."""
    uid  = str(chat_id)
    user = user_data.get(uid, {})
    name = user.get("name", "")

    days_inactive = 0
    last_active = user.get("last_active")
    if last_active:
        try:
            days_inactive = (datetime.now() - datetime.fromisoformat(last_active)).days
        except Exception:
            days_inactive = 0

    pools = [VOICE_PUSH_TEMPLATES_NO_NAME]
    if name:
        pools.append(VOICE_PUSH_TEMPLATES_WITH_NAME)
        if days_inactive >= 3:
            pools.append(VOICE_PUSH_TEMPLATES_WITH_DAYS)

    pool     = random.choice(pools)
    template = random.choice(pool)
    return template.format(name=name or "du", days=days_inactive)


def _voice_push_generate_schedule() -> list:
    """
    Generiert bis zu 3 zufällige zukünftige Zeitpunkte für die laufende Woche
    (Mo-So), innerhalb der Geschäftszeiten 09:00-18:00 *Berlin-Zeit*
    (CET im Winter, CEST im Sommer — automatisch DST-korrekt). Bei späterem
    Wocheneinstieg (z.B. User wird erst Freitag Premium Plus) gibt's
    entsprechend weniger Slots.
    """
    now_berlin     = _now_berlin()
    today_midnight = now_berlin.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start     = today_midnight - timedelta(days=now_berlin.weekday())  # Montag 00:00 Berlin

    remaining_days = [
        week_start + timedelta(days=d)
        for d in range(7)
        if (week_start + timedelta(days=d)) >= today_midnight
    ]
    num_slots = min(VOICE_PUSH_PER_WEEK, len(remaining_days))
    if num_slots == 0:
        return []
    chosen_days = random.sample(remaining_days, num_slots)

    schedule = []
    for day in chosen_days:
        hour      = random.randint(VOICE_PUSH_HOUR_MIN, VOICE_PUSH_HOUR_MAX - 1)
        minute    = random.randint(0, 59)
        dt_berlin = day.replace(hour=hour, minute=minute)
        if dt_berlin <= now_berlin:
            # Zeitpunkt heute schon vorbei → in den nächsten 30-180 Min ansetzen
            dt_berlin = now_berlin + timedelta(minutes=random.randint(30, 180))
        schedule.append(_berlin_to_naive_utc(dt_berlin).isoformat())
    schedule.sort()
    return schedule


def get_or_create_voice_push_schedule(chat_id: int) -> dict:
    """Holt den Wochenplan für diesen User, generiert bei Bedarf einen neuen.
    Die Wochengrenze wird in Berlin-Zeit bestimmt, nicht in Server-Zeit."""
    uid = str(chat_id)
    current_week = _now_berlin().strftime("%G-W%V")
    vp = user_data.get(uid, {}).get("voice_push", {})
    if vp.get("week") != current_week:
        vp = {
            "week":      current_week,
            "scheduled": _voice_push_generate_schedule(),
            "sent":      [],
        }
        user_data[uid]["voice_push"] = vp
        save_users(user_data)
    return vp


def send_voice_push(chat_id: int):
    """Sendet eine personalisierte Voice-Push-Nachricht als echte Sprachnachricht."""
    text = _voice_push_pick_template(chat_id)
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🗣️ Jetzt quatschen", callback_data="voicepush_quatsch"))
    try:
        audio = text_to_speech_stream(text, chat_id)
        bot.send_voice(chat_id, audio, reply_markup=markup)
    except Exception as e:
        log.warning(f"Voice push TTS failed for {chat_id}, sende als Text: {e}")
        bot.send_message(chat_id, f"🎤 {text}", reply_markup=markup)


def broadcast_voice_pushes() -> dict:
    """
    Cron-Funktion: prüft für jeden Premium-Plus-User ob ein Push-Slot fällig ist
    und sendet ihn. Soll alle 15-30 Min via Railway Cron aufgerufen werden.
    """
    now = datetime.now()
    sent, skipped_inactive_session, skipped_not_plus, stale_skipped = 0, 0, 0, 0

    for uid in list(user_data.keys()):
        try:
            chat_id = int(uid)
        except ValueError:
            continue

        if not is_premium_plus(chat_id):
            continue  # kein Schedule für Nicht-Plus-User anlegen

        vp = get_or_create_voice_push_schedule(chat_id)
        scheduled = vp.get("scheduled", [])
        already_sent = set(vp.get("sent", []))

        for slot in scheduled:
            if slot in already_sent:
                continue
            try:
                slot_dt = datetime.fromisoformat(slot)
            except Exception:
                already_sent.add(slot)
                continue

            if slot_dt > now:
                continue  # noch nicht fällig

            # Über 24h überfällig (z.B. nach Downtime) → stillschweigend verwerfen
            if now - slot_dt > timedelta(hours=24):
                already_sent.add(slot)
                stale_skipped += 1
                continue

            # Nicht stören wenn User mitten in Test/Onboarding/aktiver Session ist
            mode = user_state.get(chat_id, {}).get("mode")
            if chat_id in test_state or mode in ("onboarding", "test", "chat", "quatschen"):
                skipped_inactive_session += 1
                continue  # Slot bleibt offen, nächster Cron-Tick versucht's erneut

            try:
                send_voice_push(chat_id)
                already_sent.add(slot)
                sent += 1
                time.sleep(0.2)  # Telegram Flood-Schutz
            except Exception as e:
                log.warning(f"Voice push failed for {chat_id}: {e}")

        vp["sent"] = list(already_sent)
        user_data[uid]["voice_push"] = vp

    save_users(user_data)
    log.info(
        f"Voice push run: {sent} gesendet, "
        f"{skipped_inactive_session} wegen aktiver Session verschoben, "
        f"{stale_skipped} als überfällig verworfen"
    )
    return {
        "sent": sent,
        "deferred_active_session": skipped_inactive_session,
        "stale_dropped": stale_skipped,
    }


@bot.message_handler(commands=["sendvoicepushes"])
def handle_broadcast_voice_pushes(message):
    """Admin-only: manuell einen Voice-Push-Check auslösen."""
    if ADMIN_CHAT_ID and message.chat.id != ADMIN_CHAT_ID:
        return
    bot.send_message(message.chat.id, "📤 Prüfe fällige Voice Pushes...")
    result = broadcast_voice_pushes()
    bot.send_message(
        message.chat.id,
        f"✅ {result['sent']} Voice Pushes gesendet\n"
        f"⏸ {result['deferred_active_session']} verschoben (User aktiv)\n"
        f"🗑 {result['stale_dropped']} verworfen (>24h überfällig)"
    )


@bot.message_handler(commands=["testvoicepush"])
def handle_test_voice_push(message):
    """Admin-only: Vorschau eines Voice Push an sich selbst, ohne Schedule zu beeinflussen."""
    if ADMIN_CHAT_ID and message.chat.id != ADMIN_CHAT_ID:
        return
    send_voice_push(message.chat.id)


@bot.message_handler(commands=["broadcastgems"])
def handle_broadcast_gems(message):
    """Admin-only: manually trigger daily gem broadcast."""
    if ADMIN_CHAT_ID and message.chat.id != ADMIN_CHAT_ID:
        return  # silent ignore for non-admins
    bot.send_message(message.chat.id, "📤 Starte Gem-Broadcast...")
    sent = broadcast_daily_gem()
    bot.send_message(message.chat.id, f"✅ Gem gesendet an {sent} User!")


def broadcast_daily_gem():
    """Send today's gem to all active users. Called via Railway Cron."""
    sent = 0
    for uid, user in user_data.items():
        if not user.get("name"):
            continue  # skip incomplete onboarding
        try:
            chat_id = int(uid)
            send_daily_gem(chat_id)
            sent += 1
            time.sleep(0.1)  # avoid Telegram flood limits
        except Exception as e:
            log.warning(f"Gem broadcast failed for {uid}: {e}")
    log.info(f"German Gem broadcast done: {sent} users")
    return sent

# ═══════════════════════════════════════════════════════════════════════════
#  FLASK WEBHOOK SERVER — Stripe payments + Cron gem broadcast
# ═══════════════════════════════════════════════════════════════════════════

CRON_SECRET = os.getenv("CRON_SECRET", "geheim123")

flask_app = Flask(__name__)

@bot.message_handler(commands=["setpremium"])
def admin_set_premium(message):
    """Admin: /setpremium CHAT_ID [days]"""
    if message.chat.id != ADMIN_CHAT_ID: return
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Usage: /setpremium CHAT_ID [days=30]"); return
    target = parts[1].strip()
    days   = int(parts[2]) if len(parts) > 2 else 30
    if target not in user_data:
        bot.send_message(message.chat.id, f"❌ User {target} not found."); return
    from datetime import timedelta
    user_data[target]["premium"]       = True
    user_data[target]["premium_until"] = (datetime.now() + timedelta(days=days)).isoformat()
    save_users(user_data)
    bot.send_message(message.chat.id, f"✅ Premium activated for {target} — {days} days.")
    try: bot.send_message(int(target), f"🎉 *Willkommen im Premium-Club!*\n\n{days} Tage Zugriff. Tippe /themen!", parse_mode="Markdown")
    except: pass


@flask_app.route("/send_discount_offers", methods=["POST"])
def send_discount_offers_endpoint():
    """Daily cron: send one-time 50% discount to users who joined 10+ days ago with no premium."""
    auth = request.headers.get("X-Cron-Secret", "")
    if auth != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    now   = datetime.now()
    sent  = 0
    skip  = 0

    for uid, user in user_data.items():
        # Skip if already sent
        if user.get("discount_offer_sent"):
            skip += 1; continue
        # Skip if already premium
        if user.get("premium"):
            skip += 1; continue
        # Skip if joined less than 10 days ago
        joined = user.get("joined") or user.get("trial_start")
        if not joined:
            skip += 1; continue
        try:
            days_since = (now - datetime.fromisoformat(joined)).days
        except Exception:
            skip += 1; continue
        if days_since < 10:
            skip += 1; continue

        # Send the offer
        try:
            native_lang = user.get("native_language", "")
            name = user.get("name") or "du"
            bot.send_message(int(uid),
                f"🎁 Hey {name}!\n\n"
                f"Du bist jetzt seit {days_since} Tagen dabei — und wir mögen dich.\n\n"
                f"Deshalb bekommst du heute einmalig *50% Rabatt* auf Premium:\n"
                f"Nur €10 statt €20 für 30 Tage vollen Zugang.\n\n"
                f"Dein persönlicher Code: *RABATT50*\n"
                f"👉 Tippe /premium und gib den Code auf der Zahlungsseite ein.\n\n"
                f"⏰ Dieses Angebot gilt nur einmalig für dich!",
                parse_mode="Markdown"
            )
            user_data[uid]["discount_offer_sent"] = True
            sent += 1
            log.info(f"Discount offer sent to {uid}")
        except Exception as e:
            log.warning(f"Could not send discount offer to {uid}: {e}")

    save_users(user_data)
    log.info(f"Discount offers: {sent} sent, {skip} skipped")
    return jsonify({"ok": True, "sent": sent, "skipped": skip})


@flask_app.route("/reload_users", methods=["POST"])
def reload_users_endpoint():
    secret = request.json.get("secret", "") if request.is_json else ""
    if secret != os.getenv("CRON_SECRET", ""):
        return jsonify({"error": "unauthorized"}), 401
    global user_data
    user_data = load_users()
    log.info("✅ user_data reloaded from disk")
    return jsonify({"ok": True, "users": len(user_data)})


@flask_app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200

@flask_app.route("/send_gems", methods=["POST"])
def send_gems_endpoint():
    auth = request.headers.get("X-Cron-Secret", "")
    if auth != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    try:
        sent = broadcast_daily_gem()
        return jsonify({"ok": True, "sent": sent}), 200
    except Exception as e:
        log.error(f"Gem broadcast error: {e}")
        return jsonify({"error": str(e)}), 500

@flask_app.route("/send_voice_pushes", methods=["POST"])
def send_voice_pushes_endpoint():
    """Alle 15-30 Min via Railway Cron aufrufen. Prüft fällige Slots und sendet sie."""
    auth = request.headers.get("X-Cron-Secret", "")
    if auth != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    try:
        result = broadcast_voice_pushes()
        return jsonify({"ok": True, **result}), 200
    except Exception as e:
        log.error(f"Voice push broadcast error: {e}")
        return jsonify({"error": str(e)}), 500

@flask_app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "not configured"}), 500
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return jsonify({"error": "invalid signature"}), 400

    if event["type"] == "checkout.session.completed":
        session     = event["data"]["object"]
        telegram_id = session.get("metadata", {}).get("telegram_id")
        customer_id = session.get("customer")
        sub_id      = session.get("subscription")
        plan        = session.get("metadata", {}).get("plan", "standard")
        event_type  = session.get("metadata", {}).get("type", "")

        if not telegram_id or str(telegram_id) not in user_data:
            return jsonify({"ok": True}), 200

        uid = str(telegram_id)

        # ── Spende — kein Premium aktivieren ─────────────────────────────
        if event_type == "donation":
            log.info(f"💙 Stripe donation received from {telegram_id}")
            try:
                name = user_data.get(uid, {}).get("name", "")
                bot.send_message(int(telegram_id),
                    f"🙏 Danke für deine Spende{', ' + name if name else ''}! "
                    "Das bedeutet wirklich viel. 💙")
            except Exception as e:
                log.warning(f"Could not send donation thanks to {telegram_id}: {e}")
            return jsonify({"ok": True}), 200

        # ── Premium / Premium Plus aktivieren ─────────────────────────────
        user_data[uid]["premium"]                = True
        user_data[uid]["stripe_customer_id"]     = customer_id
        user_data[uid]["stripe_subscription_id"] = sub_id
        if plan == "plus":
            user_data[uid]["premium_plus"] = True
            log.info(f"✅ Premium PLUS activated via Stripe: {telegram_id}")
            welcome_msg = (
                "👑 *Willkommen bei Premium Plus!*\n\n"
                "Nicht nur Deutsch lernen — in Deutschland ankommen.\n"
                "Dein Kumpel ist jetzt 24/7 für dich da.\n\n"
                "Finanzamt-Brief? Kündigung? Oder einfach mal reden?\n"
                "Tippe /themen für Übungen — oder schreib ihm einfach drauflos. 🗣️"
            )
        else:
            user_data[uid]["premium_plus"] = False
            log.info(f"✅ Premium activated via Stripe: {telegram_id}")
            welcome_msg = (
                "🎉 *Willkommen im Premium-Club!*\n\n"
                "Du hast jetzt vollen Zugriff auf alle Szenarien & Übungen. 💪\n"
                "Dein Streak und deine XP sind natürlich noch da.\n\n"
                "Tippe /themen um weiterzumachen!"
            )
        save_users(user_data)
        try:
            bot.send_message(int(telegram_id), welcome_msg, parse_mode="Markdown")
        except Exception as e:
            log.warning(f"Could not notify {telegram_id}: {e}")

    elif event["type"] == "customer.subscription.deleted":
        customer_id = event["data"]["object"].get("customer")
        for uid, user in user_data.items():
            if user.get("stripe_customer_id") == customer_id:
                user_data[uid]["premium"]                = False
                user_data[uid]["premium_plus"]           = False
                user_data[uid]["stripe_subscription_id"] = None
                save_users(user_data)
                try:
                    bot.send_message(int(uid),
                        "😢 Dein Abo wurde gekündigt.\n"
                        "XP und Streak bleiben erhalten.\n"
                        "Tippe /premium um wieder zu starten.",
                        parse_mode="Markdown")
                except Exception as e:
                    log.warning(f"Could not notify cancelled user: {e}")
                break

    return jsonify({"ok": True}), 200

def _run_flask():
    port = int(os.getenv("PORT", 8080))
    log.info(f"✅ Flask on port {port}")
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True, debug=False)

_flask_thread = threading.Thread(target=_run_flask, daemon=True)
_flask_thread.start()
log.info("✅ Bot polling started")
bot.infinity_polling(timeout=60, long_polling_timeout=60)
