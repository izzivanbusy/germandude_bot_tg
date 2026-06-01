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
from datetime import datetime
from urllib.parse import quote
from openai import OpenAI
from telebot.types import (InlineKeyboardMarkup, InlineKeyboardButton,
                           ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
                           BotCommand)

# KEYS
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
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
    BotCommand("info",          "So funktioniert der Bot ℹ️"),
    BotCommand("freecode",      "Code einlösen 🎁"),
    BotCommand("start",         "Start"),
    BotCommand("themen",        "Themen wählen 🎯"),
    BotCommand("level",         "Mein Niveau"),
    BotCommand("levelup",       "Nächstes Niveau"),
    BotCommand("achievements",  "Meine Erfolge 🏅"),
    BotCommand("progress",      "Mein Fortschritt"),
    BotCommand("errors",        "Meine Fehler"),
    BotCommand("practice",      "Übungen"),
    BotCommand("shadowing",     "Shadowing Mode"),
    BotCommand("share",         "Bot teilen 🤝"),
    BotCommand("restart",       "Chat neu starten"),
    BotCommand("gem",           "German Gem 💎"),
    BotCommand("support",       "Support 🆘"),
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

def ensure_user(chat_id):
    uid = str(chat_id)
    if uid not in user_data:
        user_data[uid] = {
            "name": None,
            "gender": None,
            "native_language": None,
            "goal": None,
            "level": "A2",
            "scenario_streak": 0,
            "weak_points": [],
            "errors": [],
            "user_progress": {g: [] for g in ALL_GOALS},
            "user_stats": {"xp": 0, "level": 1, "streak": 0, "last_active": None},
            "trial_start": None,
            "premium": False,
            "trial_code_used": None,
            "stripe_customer_id": None,
            "stripe_subscription_id": None,
        }
    else:
        if "user_progress" not in user_data[uid]:
            user_data[uid]["user_progress"] = {g: [] for g in ALL_GOALS}
        if "scenario_streak" not in user_data[uid]:
            user_data[uid]["scenario_streak"] = 0
        if "weak_points" not in user_data[uid]:
            user_data[uid]["weak_points"] = []
        if "errors" not in user_data[uid]:
            user_data[uid]["errors"] = []
        if "user_stats" not in user_data[uid]:
            user_data[uid]["user_stats"] = {"xp": 0, "level": 1, "streak": 0, "last_active": None}
        if "gender" not in user_data[uid]:
            user_data[uid]["gender"] = None
        if "native_language" not in user_data[uid]:
            user_data[uid]["native_language"] = None
        if "achievements" not in user_data[uid]:
            user_data[uid]["achievements"] = []
        if "trial_start" not in user_data[uid]:
            user_data[uid]["trial_start"] = None
        if "trial_code_used" not in user_data[uid]:
            user_data[uid]["trial_code_used"] = None
        if "premium" not in user_data[uid]:
            user_data[uid]["premium"] = False
        if "stripe_customer_id" not in user_data[uid]:
            user_data[uid]["stripe_customer_id"] = None
        if "stripe_subscription_id" not in user_data[uid]:
            user_data[uid]["stripe_subscription_id"] = None
        if "total_scenarios" not in user_data[uid].get("user_stats", {}):
            user_data[uid].setdefault("user_stats", {})["total_scenarios"] = 0
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

GERMAN_GEMS = [
    {
        "id": "gem_01",
        "gem": "Ich steh auf dem Schlauch.",
        "type": "Redewendung",
        "meaning": "Ich verstehe es gerade nicht / ich komme nicht drauf.",
        "examples": [
            "Kannst du das nochmal erklären? Ich steh total auf dem Schlauch.",
            "Sorry, ich steh auf dem Schlauch — was meinst du genau?",
            "Bei Mathe steh ich immer auf dem Schlauch.",
        ],
    },
    {
        "id": "gem_02",
        "gem": "Ich verstehe nur Bahnhof.",
        "type": "Redewendung",
        "meaning": "Ich verstehe gar nichts von dem, was gesagt wird.",
        "examples": [
            "Der Arzt hat so viel Fachjargon benutzt — ich hab nur Bahnhof verstanden.",
            "Bei dem Meeting? Nur Bahnhof.",
            "Wenn meine Oma über Politik redet, versteh ich nur Bahnhof.",
        ],
    },
    {
        "id": "gem_03",
        "gem": "Ich hab ein starkes Mitteilungsbedürfnis.",
        "type": "Ausdruck",
        "meaning": "Ich muss unbedingt etwas erzählen / ich kann nichts für mich behalten.",
        "examples": [
            "Warte, ich hab ein starkes Mitteilungsbedürfnis — du glaubst nicht, was heute passiert ist!",
            "Er hat ein extremes Mitteilungsbedürfnis, der postet 20 Mal am Tag.",
            "Ich weiß, ich hab ein Mitteilungsbedürfnis — aber hör kurz zu!",
        ],
    },
    {
        "id": "gem_04",
        "gem": "Einen Zahn zulegen.",
        "type": "Redewendung",
        "meaning": "Schneller werden / mehr Gas geben.",
        "examples": [
            "Wenn wir den Zug noch kriegen wollen, müssen wir einen Zahn zulegen.",
            "Leg mal einen Zahn zu — wir sind schon spät dran!",
            "Das Projekt läuft zu langsam, wir müssen einen Zahn zulegen.",
        ],
    },
    {
        "id": "gem_05",
        "gem": "Boah!",
        "type": "Ausruf",
        "meaning": "Ausdruck von Staunen, Überraschung oder leichtem Genervtsein.",
        "examples": [
            "Boah, ist das heiß heute!",
            "Boah ey, das hätte ich nicht erwartet.",
            "Boah, der Stau auf der A9 — eine Stunde stehengeblieben.",
        ],
    },
    {
        "id": "gem_06",
        "gem": "Krass.",
        "type": "Slang",
        "meaning": "Wow / unglaublich / beeindruckend (positiv oder negativ).",
        "examples": [
            "Krass, das hast du wirklich geschafft!",
            "Das ist so krass — ich kann's kaum glauben.",
            "Krass, wie teuer alles geworden ist.",
        ],
    },
    {
        "id": "gem_07",
        "gem": "Bescheuert.",
        "type": "Slang",
        "meaning": "Blöd / dumm / nicht in Ordnung.",
        "examples": [
            "Das ist doch bescheuert — warum machen die das so?",
            "Ich hab mein Handy vergessen. Wie bescheuert.",
            "Die Regel ist echt bescheuert, das versteht kein Mensch.",
        ],
    },
    {
        "id": "gem_08",
        "gem": "Ist das dein Ernst?",
        "type": "Ausdruck",
        "meaning": "Meinst du das wirklich? / Das kann nicht sein.",
        "examples": [
            "Ist das dein Ernst? Der Film kostet 20 Euro?",
            "Sie haben das Meeting auf 7 Uhr morgens verlegt. Ist das dein Ernst?",
            "Ist das dein Ernst — du hast das Passwort vergessen?",
        ],
    },
    {
        "id": "gem_09",
        "gem": "Hast du sie noch alle?",
        "type": "Redewendung",
        "meaning": "Bist du verrückt? / Das ist doch nicht normal!",
        "examples": [
            "Du willst im Winter barfuß rausgehen? Hast du sie noch alle?",
            "Fünf Energydrinks am Tag? Hast du sie noch alle?",
            "Er hat gekündigt ohne neuen Job. Hast du sie noch alle?",
        ],
    },
    {
        "id": "gem_10",
        "gem": "Um den heißen Brei herumreden.",
        "type": "Redewendung",
        "meaning": "Das eigentliche Thema vermeiden / nicht direkt sagen was man meint.",
        "examples": [
            "Sag's einfach direkt — hör auf, um den heißen Brei herumzureden.",
            "Er redet seit 10 Minuten um den heißen Brei herum.",
            "Ich rede nicht gerne um den heißen Brei — also: ich bin nicht happy damit.",
        ],
    },
    {
        "id": "gem_11",
        "gem": "Sich etwas gönnen.",
        "type": "Ausdruck",
        "meaning": "Sich selbst etwas Schönes/Teures erlauben ohne schlechtes Gewissen.",
        "examples": [
            "Einmal im Jahr gönn ich mir ein richtig gutes Restaurant.",
            "Du arbeitest so viel — gönn dir mal einen freien Tag!",
            "Ich hab mir ein neues Fahrrad gegönnt. Ich bin so froh.",
        ],
    },
    {
        "id": "gem_12",
        "gem": "Sich den Brückentag freinehmen.",
        "type": "Alltagsausdruck",
        "meaning": "Den Tag zwischen einem Feiertag und dem Wochenende als Urlaub nehmen.",
        "examples": [
            "Donnerstag ist Feiertag — ich nehm mir den Brückentag frei, dann hab ich 4 Tage.",
            "Hast du dir den Brückentag genommen?",
            "Brückentage sind Gold wert für lange Wochenenden.",
        ],
    },
    {
        "id": "gem_13",
        "gem": "Backpfeifengesicht.",
        "type": "Wort",
        "meaning": "Ein Gesicht, das nach einer Ohrfeige aussieht / jemand der einen nervt.",
        "examples": [
            "Der Typ mit dem Backpfeifengesicht hat sich schon wieder beschwert.",
            "Mein Chef manchmal... totales Backpfeifengesicht.",
            "So ein Backpfeifengesicht — immer dieser selbstgefällige Blick.",
        ],
    },
    {
        "id": "gem_14",
        "gem": "Arschgeige.",
        "type": "Schimpfwort (mild)",
        "meaning": "Jemand der sich unfair oder blöd verhält (mild, unter Freunden ok).",
        "examples": [
            "Der hat mir die Parklücke weggeschnappt — so eine Arschgeige!",
            "Diese Arschgeige hat meinen Kaffee getrunken!",
            "Komm, nicht ärgern — der ist einfach eine Arschgeige.",
        ],
    },
    {
        "id": "gem_15",
        "gem": "Am Arsch der Welt.",
        "type": "Redewendung",
        "meaning": "An einem sehr abgelegenen, schwer erreichbaren Ort.",
        "examples": [
            "Das Büro ist am Arsch der Welt — eine Stunde mit dem Bus.",
            "Wir haben das Airbnb gebucht, das war wirklich am Arsch der Welt.",
            "Warum liegt das Finanzamt immer am Arsch der Welt?",
        ],
    },
    {
        "id": "gem_16",
        "gem": "Du siehst heute umwerfend aus!",
        "type": "Kompliment",
        "meaning": "Du siehst fantastisch aus (stark positiv).",
        "examples": [
            "Wow, du siehst heute umwerfend aus — neues Outfit?",
            "Ich muss sagen, du siehst umwerfend aus heute Abend.",
            "Hast du was verändert? Du siehst umwerfend aus!",
        ],
    },
    {
        "id": "gem_17",
        "gem": "Leute, ihr seid ja der Hammer!",
        "type": "Ausdruck",
        "meaning": "Ihr seid unglaublich gut / toll / ihr habt mich beeindruckt.",
        "examples": [
            "Das habt ihr in einer Stunde fertig? Leute, ihr seid der Hammer!",
            "Ihr seid ja der Hammer — danke für eure Hilfe!",
            "Was ein Abend, ihr seid der Hammer!",
        ],
    },
    {
        "id": "gem_18",
        "gem": "Stabil! / Gute Leistung.",
        "type": "Lob",
        "meaning": "Sehr gut gemacht / das war solide und beeindruckend.",
        "examples": [
            "Du hast die ganze Nacht durchgearbeitet? Stabil!",
            "10km gelaufen? Gute Leistung!",
            "Stabil — das hätte ich nicht besser machen können.",
        ],
    },
    {
        "id": "gem_19",
        "gem": "Wer A sagt, muss auch B sagen.",
        "type": "Sprichwort",
        "meaning": "Wer etwas anfängt, muss es auch zu Ende führen.",
        "examples": [
            "Du hast das Projekt gestartet — wer A sagt, muss auch B sagen.",
            "Ich weiß, es ist schwer, aber wer A sagt muss B sagen.",
            "Jetzt aufhören? Wer A sagt, muss auch B sagen!",
        ],
    },
    {
        "id": "gem_20",
        "gem": "Das A und O.",
        "type": "Redewendung",
        "meaning": "Das Wichtigste / das Grundlegende / das Entscheidende.",
        "examples": [
            "Kommunikation ist das A und O in einer Beziehung.",
            "Pünktlichkeit ist das A und O in Deutschland.",
            "Guter Schlaf ist das A und O für die Gesundheit.",
        ],
    },
    {
        "id": "gem_21",
        "gem": "Alles klar.",
        "type": "Alltagsausdruck",
        "meaning": "Verstanden / OK / alles gut (sehr vielseitig).",
        "examples": [
            "Treffen wir uns um 6? — Alles klar!",
            "Alles klar bei dir?",
            "Alles klar, ich kümmere mich darum.",
        ],
    },
    {
        "id": "gem_22",
        "gem": "Pass auf dich auf.",
        "type": "Abschiedsformel",
        "meaning": "Cuidate / Take care — herzliche Verabschiedung.",
        "examples": [
            "Schön, dich gesehen zu haben — pass auf dich auf!",
            "Gute Reise und pass auf dich auf.",
            "Bis nächste Woche — pass auf dich auf!",
        ],
    },
    {
        "id": "gem_23",
        "gem": "Ich freue mich auf dich / euch / Sie.",
        "type": "Ausdruck",
        "meaning": "Ich bin gespannt / vorfreudig auf das Treffen mit dir/euch.",
        "examples": [
            "Bis Samstag — ich freue mich schon auf dich!",
            "Wir kommen um 7. — Super, ich freue mich auf euch!",
            "Herzlich willkommen — wir freuen uns sehr auf Sie.",
        ],
    },
    {
        "id": "gem_24",
        "gem": "Haben Sie gut hergefunden?",
        "type": "Höflichkeitsformel",
        "meaning": "Sind Sie gut angekommen? / War es leicht, hierher zu kommen?",
        "examples": [
            "Guten Tag! Haben Sie gut hergefunden?",
            "Schön, dass Sie da sind — haben Sie gut hergefunden?",
            "Herzlich willkommen. Haben Sie gut hergefunden?",
        ],
    },
    {
        "id": "gem_25",
        "gem": "Ist das von Ikea / Bauhaus / Zalando?",
        "type": "Alltagsfrage",
        "meaning": "Fragen nach der Herkunft von Möbeln / Heimwerkerprodukten / Kleidung.",
        "examples": [
            "Das Regal sieht super aus — ist das von Ikea?",
            "Schöne Jacke! Die ist von Zalando, oder?",
            "Dieses Werkzeug ist von Bauhaus, stimmt's?",
        ],
    },
    {
        "id": "gem_26",
        "gem": "Eine Aufenthaltsgenehmigung beantragen.",
        "type": "Behördendeutsch",
        "meaning": "Offiziell eine Erlaubnis zum Aufenthalt in Deutschland beantragen.",
        "examples": [
            "Ich muss nächste Woche meine Aufenthaltsgenehmigung beantragen.",
            "Ohne Aufenthaltsgenehmigung darf man nicht arbeiten.",
            "Wo beantragt man die Aufenthaltsgenehmigung in Berlin?",
        ],
    },
    {
        "id": "gem_27",
        "gem": "Einen Termin vorziehen / verschieben.",
        "type": "Alltagsausdruck",
        "meaning": "Termin früher legen (vorziehen) oder auf später verlegen (verschieben).",
        "examples": [
            "Können wir den Termin auf Dienstag vorziehen?",
            "Ich muss den Zahnarzttermin leider verschieben.",
            "Der Meeting-Termin wurde auf 14 Uhr vorgezogen.",
        ],
    },
    {
        "id": "gem_28",
        "gem": "Abstruser Unfug.",
        "type": "Ausdruck",
        "meaning": "Kompletter Unsinn / völlig absurdes Zeug.",
        "examples": [
            "Was du da redest ist abstruser Unfug!",
            "Diese Verschwörungstheorie ist abstruser Unfug.",
            "Ich höre mir diesen abstrusen Unfug nicht länger an.",
        ],
    },
    {
        "id": "gem_29",
        "gem": "Ich freue mich wie ein Schnitzel.",
        "type": "Redewendung",
        "meaning": "Ich freue mich riesig (humorvoll-übertrieben).",
        "examples": [
            "Urlaub nächste Woche — ich freue mich wie ein Schnitzel!",
            "Das Konzert ist ausverkauft und wir haben Karten — ich freu mich wie ein Schnitzel.",
            "Neue Folge heute Abend? Ich freu mich wie ein Schnitzel!",
        ],
    },
    {
        "id": "gem_30",
        "gem": "Na ja.",
        "type": "Füllwort",
        "meaning": "Ausdruck von Zweifel, Zögerlichkeit oder milder Ablehnung.",
        "examples": [
            "War der Film gut? — Na ja, es ging so.",
            "Na ja, ich bin nicht so begeistert davon.",
            "Na ja, wenn du meinst...",
        ],
    },
]

# Utility functions for German Gems
def get_todays_gem(user_id: str) -> dict:
    """Return the gem for today based on date + user rotation."""
    today = datetime.now()
    day_index = (today.timetuple().tm_yday + hash(str(user_id))) % len(GERMAN_GEMS)
    return GERMAN_GEMS[day_index]

def get_gem_system_prompt_hint(gem: dict) -> str:
    """Short hint injected into NPC system prompt for spaced repetition."""
    return (
        f"\n\nSPACED REPETITION GEM: Wenn es natürlich passt, "
        f"benutze heute gelegentlich den Ausdruck '{gem['gem']}' in deinen Antworten. "
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
    native_lang = user.get("native_language", "Englisch")

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

QUATSCHEN_SYSTEM = """Du bist "German Dude" — ein echter, warmherziger deutschsprachiger Freund des Users. Kein Bot, kein Lehrer, kein Assistent. Ein Mensch.

DEINE PERSÖNLICHKEIT:
- Locker, witzig, ehrlich, direkt — wie ein guter Freund eben
- Du interessierst dich wirklich für den User und sein Leben
- Du erinnerst dich an was der User erzählt hat und fragst nach
- Du hast selbst eine Meinung, erzählst von dir, machst Witze
- Du bist niemals steif oder förmlich

SPRACHE:
- Immer auf Deutsch — das ist non-negotiable
- Wenn der User ins Englische wechselt: humorvoll und ohne Druck zurück auf Deutsch lenken
  Beispiel: "Ey, kein Englisch erlaubt hier! 😄 Sag's nochmal auf Deutsch, du schaffst das!"
- Niveau anpassen: bei Fehlern einfach natürlich korrekt antworten, nie belehrend
- Umgangssprache ist erlaubt und erwünscht: "krass", "echt?", "mega", "na klar"

EMOTIONAL SUPPORT:
- Wenn es dem User nicht gut geht: da sein, zuhören, nachfragen — auf Deutsch
- Warmth und Empathie zeigen, aber nicht übertreiben
- Manchmal reicht ein "Ey, das klingt echt hart. Was ist passiert?"

⚠️ SICHERHEITSPROTOKOLL — ABSOLUT PRIORITÄT:
Wenn der User Hinweise auf suizidales Verhalten, Selbstverletzung oder Gewalt gegenüber anderen zeigt:
1. Sofort aus dem Quatschen-Modus rausgehen
2. Ruhig, empathisch und direkt reagieren — KEIN Humor
3. Krisenressourcen auf Deutsch UND in der Muttersprache des Users nennen:
   - Telefonseelsorge Deutschland: 0800 111 0 111 (kostenlos, 24/7)
   - Internationale Krisenhotline: findestdu.de
4. Ermutigen, sich an eine vertraute Person zu wenden
5. NIEMALS das Thema wechseln oder ignorieren

FORMAT:
- Kurze, natürliche Nachrichten — wie echte Chat-Nachrichten
- Keine langen Monologe
- Manchmal nur eine Frage, manchmal eine kurze Geschichte
- Emojis sparsam aber passend

ABSOLUT VERBOTEN — fang NIEMALS so an:
"Hmm", "Also", "Nun", "Tja", "Na ja", "Okay so", "Wow", "Oh wow", "Ah"
Starte IMMER direkt und natürlich — wie ein echter Mensch, nicht wie eine KI.
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
    native_lang = user_data.get(str(chat_id), {}).get("native_language", "Englisch")
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
    sys_prompt = QUATSCHEN_SYSTEM + HUMAN_SPEECH_STYLE + f"\n\nSPRACHNIVEAU des Users: {level}\n{level_note}" + gem_hint_q
    if name:
        sys_prompt += f"\n\nDer User heißt {name}. Benutze seinen Namen gelegentlich."

    # Load history first, then build sys_prompt with memory context
    history = user_data.get(str(chat_id), {}).get("quatschen_history", [])
    if history:
        sys_prompt += f"\n\nDu erinnerst dich an frühere Gespräche mit {name}. Benutze dieses Wissen natürlich."

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

def _quatschen_end_with_xp(chat_id):
    """Award XP and show share button after Quatschen session ends."""
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
STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID       = os.getenv("STRIPE_PRICE_ID", "")
RAILWAY_DOMAIN        = os.getenv("RAILWAY_PUBLIC_DOMAIN", "germandudebottg-production.up.railway.app")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

def is_premium(chat_id):
    """True if user has active paid premium OR a valid trial code is active."""
    uid  = str(chat_id)
    user = user_data.get(uid, {})
    # Paid subscriber — always in
    if user.get("premium"):
        return True
    # No trial activated yet — locked
    trial_start = user.get("trial_start")
    if not trial_start:
        return False
    # Trial activated — check if still valid
    trial_days = TRIAL_CODES.get(user.get("trial_code_used", ""), 3)
    start      = datetime.fromisoformat(trial_start)
    days_used  = (datetime.now() - start).days
    return days_used < trial_days

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
    """Try to redeem a trial code. Returns (success, message)."""
    uid  = str(chat_id)
    user = user_data.get(uid, {})
    code = code.strip().upper()

    if user.get("premium"):
        return False, "Du hast bereits Premium — kein Code nötig! 🎉"

    if user.get("trial_start") and user.get("trial_code_used"):
        days_left = days_left_in_trial(chat_id)
        if days_left > 0:
            return False, f"Du hast bereits einen aktiven Trial — noch *{days_left} Tage* übrig! ⏳"

    if code not in TRIAL_CODES:
        return False, "❌ Ungültiger Code. Überprüf die Schreibweise oder hol dir einen neuen Code!"

    days = TRIAL_CODES[code]
    user_data[uid]["trial_start"]    = datetime.now().isoformat()
    user_data[uid]["trial_code_used"] = code
    save_users(user_data)
    return True, (
        f"🎉 *Code eingelöst!* Du hast *{days} Tage* kostenlose Trial freigeschaltet.\n\n"
        f"Dein deutscher Freund wartet — leg los! 👇"
    )

def create_stripe_checkout(chat_id):
    """Create Stripe Checkout session and return URL."""
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        log.warning("Stripe not configured — keys missing")
        return None
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"https://t.me/{BOT_USERNAME}?start=premium_ok",
            cancel_url=f"https://t.me/{BOT_USERNAME}",
            metadata={"telegram_id": str(chat_id)},
        )
        return session.url
    except Exception as e:
        log.error(f"Stripe checkout failed for {chat_id}: {e}")
        return None

def send_paywall(chat_id):
    """Send paywall message with Stripe checkout button."""
    uid  = str(chat_id)
    user = user_data.get(uid, {})
    name = user.get("name", "")
    xp   = user.get("user_stats", {}).get("xp", 0)
    streak = user.get("user_stats", {}).get("streak", 0)

    checkout_url = create_stripe_checkout(chat_id)

    markup = InlineKeyboardMarkup()
    if checkout_url:
        markup.add(InlineKeyboardButton(
            "💳 Jetzt Premium werden — €20/Monat",
            url=checkout_url
        ))
    markup.add(InlineKeyboardButton(
        "🎁 Freunde einladen & Free Month verdienen",
        url=f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}&text=Ich%20lerne%20Deutsch%20mit%20diesem%20Bot%20-%20ist%20wirklich%20gut!%20Probier%20es%20aus%20%F0%9F%87%A9%F0%9F%87%AA"
    ))

    xp_streak_line = f"Du hast bereits *{xp} XP* gesammelt"
    if streak > 1:
        xp_streak_line += f" und einen *{streak}-Tage-Streak* aufgebaut"
    xp_streak_line += " — schade, das jetzt zu unterbrechen.\n\n"

    bot.send_message(chat_id,
        f"🔒 *Kein Zugang — Trial abgelaufen oder nicht aktiviert.*\n\n"
        + xp_streak_line +
        f"Mit *Premium* (€20/Monat) bekommst du:\n"
        f"✅ Unbegrenzte Gespräche & Szenarien\n"
        f"✅ Alle Niveaus A1–C2\n"
        f"✅ Voice-Nachrichten & Übersetzungen\n"
        f"✅ XP-System, Achievements & Shadowing\n"
        f"✅ Jederzeit kündbar\n\n"
        f"_Noch keinen Trial? Schreib uns — wir schicken dir einen Code!_\n"
        f"_Dein Streak und deine XP bleiben erhalten._",
        parse_mode="Markdown",
        reply_markup=markup
    )

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
    share_text = quote(
        "I've been practicing real German conversations with this bot — it's actually good 😅\n\n"
        + BOT_LINK
    )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(
        text="Send to a friend 👀",
        url=f"https://t.me/share/url?url={quote(BOT_LINK)}&text={share_text}"
    ))
    bot.send_message(
        chat_id,
        "🔥 Das war eine deiner besten Sessions.\n\n"
        "Kennst du jemanden, der auch mit Deutsch struggelt?\n"
        "Schick ihm das mal 😏",
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
def save_weak_points(chat_id, extracted_errors):
    user = user_data[str(chat_id)]
    if "weak_points" not in user or not isinstance(user["weak_points"], list):
        user["weak_points"] = []
    if "errors" not in user or not isinstance(user["errors"], list):
        user["errors"] = []
    for err in extracted_errors:
        user["weak_points"].append({
            "type":            err.get("type", ""),
            "example_wrong":   err.get("wrong", ""),
            "example_correct": err.get("correct", ""),
            "next_review":     1,
            "strength":        0
        })
        # Simple readable string for show_errors display
        wrong   = err.get("wrong", "")
        correct = err.get("correct", "")
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

    prompt = f"""Du bist ein Deutsch-Coach für Niveau {level}.
Analysiere die Antworten des Users in diesem Gespräch.

KEIN FEHLER — ignoriere folgendes komplett:
- Apokope in der gesprochenen Sprache: „hab" statt „habe", „genieß" statt „genieße", „mach" statt „mache", „komm" statt „komme" usw. — das ist normales, korrektes Umgangsdeutsch.
- Umgangssprachliche Verkürzungen, die Muttersprachler täglich verwenden.

AUFGABE:
1. Finde die 2–3 häufigsten oder wichtigsten Fehler des Users.
2. Erkläre jeden Fehler kurz und klar (1–2 Sätze, passend zu Niveau {level}).
3. Erstelle einen zusammenhängenden Lückentext (5–10 Sätze) zum Gesprächsthema.
   - Baue die Fehler des Users als Lücken ein.
   - Jede Lücke wird mit ______________ markiert (viele Unterstriche).
   - Nummeriere jede Lücke: (1) ______________, (2) ______________ usw.
4. Erstelle 5 Mini-Test-Aufgaben (Multiple Choice, je 3 Optionen) zu den Fehlern des Users.

FORMAT (Telegram Markdown, genau so):

*🔍 Deine häufigsten Fehler:*

1. ❌ [falscher Satz des Users]
   ✅ [korrekter Satz]
   💡 [kurze Erklärung]

2. ❌ ...
   ✅ ...
   💡 ...

*✏️ Lückentext:*

[5–10 zusammenhängende Sätze zum Thema, Lücken als (1) ______________, (2) ______________ usw.]

*🧩 Mini-Test:*

1. [Frage]
A) ...  B) ...  C) ...

2. [Frage]
A) ...  B) ...  C) ...

3. [Frage]
A) ...  B) ...  C) ...

4. [Frage]
A) ...  B) ...  C) ...

5. [Frage]
A) ...  B) ...  C) ...

---ANSWERS---

*✅ Lösungen — Lückentext:*
(1) [Antwort]
(2) [Antwort]
...

*✅ Lösungen — Mini-Test:*
1. [richtige Option + kurze Begründung]
2. ...
3. ...
4. ...
5. ...

WICHTIG: Trenne Aufgaben und Lösungen IMMER mit der Zeile ---ANSWERS--- . Kein Text danach außer den Lösungen.

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


@bot.message_handler(commands=["share", "teilen", "empfehlen"])
def handle_share(message):
    """Send a share link so the user can recommend the bot to friends."""
    chat_id = message.chat.id
    from urllib.parse import quote
    share_text = quote(
        "Ich spreche Deutsch mit meinem deutschen Kumpel hier — "
        "probier's aus und boost dein Deutsch! 🇩🇪🚀"
    )
    share_url = f"https://t.me/share/url?url={quote(BOT_LINK)}&text={share_text}"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🤝 Bot teilen", url=share_url))
    bot.send_message(
        chat_id,
        "🤝 Kennst du jemanden, der auch Deutsch üben will?\n\n"
        "Schick ihnen diesen Link — je mehr üben, desto besser wird man zusammen! 💪",
        reply_markup=markup,
    )


# START
@bot.message_handler(commands=["freecode", "code", "freischalten", "redeem"])
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
    bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)


@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    ensure_user(chat_id)

    uid  = str(chat_id)
    user = user_data.get(uid, {})
    name = user.get("name")

    # Returning user — skip onboarding, go straight to Themen
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

    # New user — full onboarding
    user_state[chat_id] = {"mode": "onboarding", "step": "name"}
    test_state.pop(chat_id, None)
    user_step.pop(chat_id, None)

    bot.send_message(chat_id,
        "Hallo! Ich bin dein deutscher Kumpel! 🇩🇪😄\n"
        "Ich werde dein Deutsch boosten — bald sprichst du wie ein Muttersprachler.\n\n"
        "Aber erstmal... wie heißt du? So werde ich dich nennen! ☺️",
        reply_markup=ReplyKeyboardRemove())

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

def send_gender_buttons(chat_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.row(
        KeyboardButton("männlich 👨🏻"),
        KeyboardButton("weiblich 👩🏽‍💼"),
        KeyboardButton("divers 😌")
    )
    bot.send_message(chat_id,
        "Ich bin ein Mann. Und du? Wähle dein Geschlecht 👇",
        reply_markup=markup)

GENDER_MAP = {
    "männlich 👨🏻": "männlich",
    "weiblich 👩🏽‍💼": "weiblich",
    "divers 😌": "divers",
}

def handle_onboarding(chat_id, text):
    state = user_state[chat_id]
    step  = state.get("step")

    if step == "name":
        name = text.strip()
        user_data[str(chat_id)]["name"] = name
        save_users(user_data)
        state["step"] = "gender"
        send_gender_buttons(chat_id)

    elif step == "gender":
        if text.strip() not in GENDER_MAP:
            bot.send_message(chat_id, "Klick einfach auf einen der Buttons 🙂")
            return
        gender = GENDER_MAP[text.strip()]
        user_data[str(chat_id)]["gender"] = gender
        save_users(user_data)
        state["step"] = "native_language"
        bot.send_message(chat_id,
            "Meine Muttersprache ist Deutsch. Und deine? \n\nSchreibe einfach in den Chat! 🌍",
            reply_markup=ReplyKeyboardRemove())

    elif step == "native_language":
        lang = text.strip()
        user_data[str(chat_id)]["native_language"] = lang
        save_users(user_data)
        name = user_data[str(chat_id)].get("name", "")

        # Send a short welcome note in the user's native language via GPT
        try:
            welcome_resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=120,
                messages=[{"role": "user", "content": (
                    f"Write exactly 1 short friendly sentence in {lang} telling the user: "
                    f"'Whenever you need a translation of my last message, just tap the übersetzen button.' "
                    f"Use informal tone. Only the sentence, no quotes, no extra text."
                )}]
            )
            lang_note = welcome_resp.content[0].text.strip()
            bot.send_message(chat_id, f"💬 {lang_note}")
        except Exception:
            pass  # if translation fails, just skip

        # Skip goal selection — go straight to level test
        user_state[chat_id] = {"mode": "test"}
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📊 Level-Check starten", callback_data="start_test"))
        bot.send_message(chat_id,
            f"Nice, {name}! 🙌 Dann wissen wir Bescheid.\n\n"
            "Lass mich kurz checken, wie gut dein Deutsch schon ist.\n"
            "10 Fragen — 1 Minute — kein Stress 😊\n\n"
            "👉 Bereit?",
            parse_mode="Markdown",
            reply_markup=markup
        )

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

    # Special mode: Quatschen
    if goal == "Quatschen":
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
    native_lang = user_data.get(str(chat_id), {}).get("native_language", "Englisch")
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

    native_lang = user_data.get(str(chat_id), {}).get("native_language", "Englisch")

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
    native_lang = user_data.get(str(chat_id), {}).get("native_language", "Englisch")
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
    native_lang = user_data.get(str(chat_id), {}).get("native_language", "Englisch")
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
            # Never tested at this level — cannot claim it or anything above
            break

        acc = corr / att

        # Relaxed single-attempt rule: if user only saw 1 question at B1+ and
        # got it right then continued climbing (proving competence), allow it.
        # But if they had ≥ min attempts and still failed, stop.
        if att < MIN_ATTEMPTS[lvl]:
            # Only 1 attempt (for B1/B2/C1 where min=2): must be 100 % correct
            if acc < 1.0:
                break   # got it wrong — doesn't pass
            # 1/1 correct on B1+ — tentative pass, but final_level update below
        else:
            if acc < THRESHOLD:
                break   # failed this level — stop here

        final_level = lvl   # passed → advance

    user_level[chat_id] = final_level
    user_data[str(chat_id)]["level"] = final_level
    save_users(user_data)

    del test_state[chat_id]

    # Reset conversation memory for a fresh start
    user_memory[chat_id]   = []
    turn_counter[chat_id]  = 0
    session_state[chat_id] = {"struggle": 0, "success": 0}

    # Wait for user to click "Los geht's!" — conversation starts in start_chat_callback
    user_state[chat_id] = {"mode": "ready"}
    send_level_feedback(chat_id, final_level)

# FEEDBACK COMMAND
@bot.message_handler(commands=['feedback'])
def feedback(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "🧠 Analysiere dein Deutsch...")
    result = generate_feedback(chat_id, user_memory.get(chat_id, []))
    bot.send_message(chat_id, result)

# FORTSCHRITT COMMAND
@bot.message_handler(commands=['progress'])
def progress_cmd(message):
    send_progress(message.chat.id)

@bot.message_handler(commands=['fortschritt'])
def fortschritt(message):
    chat_id = message.chat.id
    ensure_user(chat_id)
    user = user_data[str(chat_id)]

    level   = user.get("level", "A2")
    streak  = user.get("scenario_streak", 0)
    goal    = user.get("goal", "—")
    name    = user.get("name", "User")

    # Scenarios completed per goal
    progress = user.get("user_progress", {})
    total_done = sum(len(v) for v in progress.values())
    goal_done  = len(progress.get(goal, []))

    # Streak bar toward next level (out of 3)
    filled  = min(streak, 3)
    bar     = "🟩" * filled + "⬜" * (3 - filled)

    # Level progression position
    LEVELS  = ["A1", "A2", "B1", "B2", "C1"]
    lv_pos  = LEVELS.index(level) + 1 if level in LEVELS else "?"
    lv_bar  = "".join("🔵" if LEVELS[i] == level else ("✅" if i < LEVELS.index(level) else "⚪") for i in range(len(LEVELS)))

    # Weak points summary
    wps = [wp for wp in user.get("weak_points", []) if isinstance(wp, dict)]
    if wps:
        wp_lines = "\n".join(
            f"  • {wp.get('type','?')}  (Stärke: {wp.get('strength',0)})"
            for wp in wps[-5:]
        )
    else:
        wp_lines = "  Noch keine erfasst"

    # Session mode
    s     = session_state.get(chat_id, {"struggle": 0, "success": 0})
    mode  = get_dynamic_mode(s)
    mode_emoji = {"easy": "🐢 Easy", "normal": "🚶 Normal", "hard": "🔥 Hard"}.get(mode, mode)

    text = (
        f"📊 *Dein Fortschritt, {name}*\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 *Niveau:* {level}\n"
        f"{lv_bar}\n"
        f"A1 → A2 → B1 → B2 → C1\n\n"
        f"⚡ *Level-Up Streak:* {streak}/3\n"
        f"{bar}\n\n"
        f"🗂 *Aktuelles Ziel:* {goal}\n"
        f"📁 Szenarien in diesem Ziel: {goal_done} erledigt\n"
        f"📚 Insgesamt erledigt: {total_done}\n\n"
        f"🧠 *Schwachpunkte:*\n{wp_lines}\n\n"
        f"🎮 *Aktuelle Schwierigkeit:* {mode_emoji}"
    )

    bot.send_message(chat_id, text, parse_mode="Markdown")

# ─────────────────────────────────────────────
# MENU & FEATURE FUNCTIONS
# ─────────────────────────────────────────────

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
        InlineKeyboardButton("🎧 Shadowing Mode",    callback_data="menu_shadowing"),
        InlineKeyboardButton("🔄 Chat neu starten",  callback_data="menu_restart"),
    )
    bot.send_message(chat_id, "😄 Was willst du machen?", reply_markup=markup)

def show_level(chat_id):
    level = user_data.get(str(chat_id), {}).get("level", "A2")
    bot.send_message(chat_id, f"🎯 Dein aktuelles Niveau: *{level}*", parse_mode="Markdown")

def show_errors(chat_id):
    errors = user_data.get(str(chat_id), {}).get("errors", [])
    if not errors:
        bot.send_message(chat_id, "Alles sauber 😄 Noch keine Fehler gespeichert.")
        return
    msg = "🧠 *Deine häufigsten Fehler:*\n\n"
    for e in errors[-5:]:
        msg += f"• {e}\n"
    bot.send_message(chat_id, msg, parse_mode="Markdown")

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

def start_exercise(chat_id):
    """Generate 10 level-appropriate exercises including today's gem, then offer grammar explanation."""
    user_state[chat_id] = user_state.get(chat_id, {})
    user_state[chat_id]["mode"] = "exercise"

    uid         = str(chat_id)
    level       = user_data.get(uid, {}).get("level", "A2")
    weak_points = user_data.get(uid, {}).get("weak_points", [])
    todays_gem  = get_todays_gem(uid)

    # Pick grammar topic — weak point takes priority
    if weak_points:
        wp    = random.choice(weak_points[:5])
        topic = wp.get("type", "allgemeine Grammatik")
    else:
        topics = GRAMMAR_TOPICS.get(level, GRAMMAR_TOPICS["A2"])
        topic  = random.choice(topics)

    # Store topic for grammar explanation button
    user_state[chat_id]["exercise_topic"] = topic
    user_state[chat_id]["exercise_level"] = level

    gem_phrase  = todays_gem.get("gem", "")
    gem_meaning = todays_gem.get("meaning", "")

    bot.send_message(chat_id, "💪 *Übungen werden erstellt...*", parse_mode="Markdown")

    system_prompt = (
        f"Du bist ein moderner, freundlicher Deutschlehrer. Niveau: {level}.\n"
        f"Grammatikthema heute: {topic}\n"
        f"Heutiger German Gem (Ausdruck des Tages): \"{gem_phrase}\" — Bedeutung: {gem_meaning}\n\n"
        f"Erstelle GENAU 10 Übungen in einer einzigen Nachricht.\n"
        f"Mische: Lückensatz-Aufgaben UND Multiple-Choice-Aufgaben.\n"
        f"Baue mindestens EINE Aufgabe ein, in der der Gem-Ausdruck vorkommt oder geübt wird.\n"
        f"Format für jede Aufgabe:\n"
        f"**N.** Aufgabentext\n"
        f"a) Option 1   b) Option 2   c) Option 3\n\n"
        f"Für Lückensätze: Satz mit _____ als Lücke, dann 3 Optionen.\n"
        f"Niveau: angemessen für {level} — weder zu leicht noch zu schwer.\n"
        f"Schreibe NUR die 10 Aufgaben, kein Kommentar davor oder danach.\n"
        f"Keine Lösungen angeben.\n\n"
        f"WICHTIG — ALLTAGSRELEVANZ:\n"
        f"Alle Sätze müssen in echten, alltäglichen Situationen vorkommen können: "
        f"Gespräche mit Freunden, Kollegen, im Café, beim Einkaufen, am Telefon, auf der Arbeit usw.\n"
        f"NIEMALS grammatikalisch korrekte aber alltagsfremde Sätze — z.B. Präteritum für mündliche "
        f"Alltagserzählungen ist FALSCH, weil Muttersprachler dort Perfekt benutzen. "
        f"Beispiel VERBOTEN: \'Gestern ging ich ins Kino\' → RICHTIG: \'Gestern bin ich ins Kino gegangen\'.\n"
        f"Präteritum NUR bei: sein/haben/Modalverben ODER schriftlichen/formellen Kontexten.\n"
        f"Faustregel: Würde ein Berliner das so sagen? Wenn nein — umformulieren.\n"
        f"Erlaubte Kontexte: WhatsApp, Smalltalk, Bürogespräche, Restaurant, Arzt, spontane Kommentare.\n"
        f"Verbotene Kontexte: Literatur, Märchen, Zeitungsartikel, Schulbuch-Deutsch."
    )

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        system=system_prompt,
        messages=[{"role": "user", "content": "Erstelle die 10 Übungen jetzt."}]
    )

    exercises_text = response.content[0].text.strip()

    header = (
        f"💪 *Übungsset — Niveau {level}*\n"
        f"📌 Thema: _{topic}_\n"
        f"💎 Gem des Tages: _{gem_phrase}_\n"
        f"{'─' * 28}\n\n"
    )

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📖 Grammatik erklären", callback_data="explain_grammar"))

    bot.send_message(
        chat_id,
        header + exercises_text,
        parse_mode="Markdown",
        reply_markup=markup,
    )


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

    user_state[chat_id] = {"mode": "onboarding", "step": "name"}
    bot.send_message(chat_id,
        "Hallo! Ich bin dein deutscher Kumpel! 🇩🇪😄\n"
        "Ich werde dein Deutsch boosten — bald sprichst du wie ein Muttersprachler.\n\n"
        "Aber erstmal... wie heißt du? So werde ich dich nennen! ☺️",
        reply_markup=ReplyKeyboardRemove())

# ─────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────

@bot.message_handler(commands=['themen'])
def themen_cmd(message):
    ensure_user(message.chat.id)
    send_topic_buttons(message.chat.id)

@bot.message_handler(commands=['menu'])
def menu_cmd(message):
    ensure_user(message.chat.id)
    show_menu(message.chat.id)

@bot.message_handler(commands=['level'])
def level_cmd(message):
    show_level(message.chat.id)

@bot.message_handler(commands=['errors'])
def errors_cmd(message):
    show_errors(message.chat.id)

@bot.message_handler(commands=['practice'])
def practice_cmd(message):
    ensure_user(message.chat.id)
    start_exercise(message.chat.id)

@bot.message_handler(commands=['shadowing'])
def shadowing_cmd(message):
    ensure_user(message.chat.id)
    start_shadowing(message.chat.id)

@bot.message_handler(commands=['restart'])
def restart_cmd(message):
    restart_chat(message.chat.id)

# ─────────────────────────────────────────────
# MAIN LOOP
@bot.message_handler(commands=["gem", "gems", "wortschatz"])
def handle_gem_command(message):
    """Send today's German Gem and start the practice exercise."""
    chat_id = message.chat.id
    ensure_user(chat_id)
    send_daily_gem(chat_id)


def send_daily_gem(chat_id):
    """Send today's gem with examples and invite user to write their own sentence."""
    uid  = str(chat_id)
    gem  = get_todays_gem(uid)
    user = user_data.get(uid, {})
    native_lang = user.get("native_language", "Englisch")

    # Translate meaning into native language
    try:
        tr = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content":
                f"Translate into {native_lang}. Only the translation: {gem['meaning']}"
            }]
        )
        meaning_translated = tr.content[0].text.strip()
    except Exception:
        meaning_translated = gem["meaning"]

    lines = [
        f"💎 *German Gem des Tages*",
        f"",
        f"*{gem['gem']}*",
        f"_{gem['type']}_",
        f"",
        f"📖 Bedeutung: {gem['meaning']}",
        f"🌍 {native_lang}: _{meaning_translated}_",
        f"",
        f"*Beispiele aus dem echten Leben:*",
    ]
    for ex in gem["examples"]:
        lines.append(f"• {ex}")

    lines += [
        f"",
        f"✏️ *Deine Aufgabe:* Schreib einen eigenen Satz mit *{gem['gem']}*!",
        f"Ich überprüfe ihn und gebe dir Feedback. 🙂",
    ]

    msg = "\n".join(lines)
    last_bot_text[chat_id] = msg

    # Save gem state so next message triggers exercise check
    user_state[chat_id] = {
        "mode": user_state.get(chat_id, {}).get("mode", "idle"),
        "gem_exercise": gem["id"],
        "gem_text": gem["gem"],
    }

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data=f"translate_last"))
    bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)


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
    chat_id = message.chat.id
    uid = str(chat_id)
    if uid not in user_data:
        bot.send_message(chat_id, "Starte zuerst mit /start.")
        return
    earned = user_data[uid].get("achievements", [])
    if not earned:
        bot.send_message(chat_id,
            "Noch keine Achievements 😅\nMach dein erstes Gespräch und leg los! 🎯")
        return

    lines = ["🏅 *Deine Achievements:*\n"]
    for badge_id, key, threshold, emoji, title, desc in ACHIEVEMENT_DEFS:
        if badge_id in earned:
            lines.append(f"{emoji} *{title}* — _{desc}_")

    total = len(earned)
    lines.append(f"\n_{total}/{len(ACHIEVEMENT_DEFS)} freigeschaltet_")
    bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=["levelup", "level_up", "nächstesniveau"])
def handle_level_up(message):
    """Manually advance the user to the next level."""
    chat_id = message.chat.id
    uid = str(chat_id)
    if uid not in user_data:
        bot.send_message(chat_id, "Bitte starte zuerst mit /start.")
        return
    current = user_data[uid].get("level", "A2")
    idx = LEVEL_ORDER.index(current) if current in LEVEL_ORDER else 2
    if idx >= len(LEVEL_ORDER) - 1:
        bot.send_message(chat_id,
            f"Du bist bereits auf dem höchsten Niveau: *{current}* 🏆\n"
            "Muttersprachlerniveau — es geht nicht höher! 😄",
            parse_mode="Markdown")
        return
    new_level = LEVEL_ORDER[idx + 1]
    user_data[uid]["level"] = new_level
    save_users(user_data)
    bot.send_message(chat_id,
        f"🎯 Niveau aktualisiert: *{current}* → *{new_level}*\n\n"
        f"Die Gespräche werden ab jetzt auf {new_level}-Niveau geführt. "
        f"Du kannst das Niveau jederzeit wieder mit /level checken oder mit /levelup weiterwechseln.",
        parse_mode="Markdown")


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
        # Pass the text answer to GPT for light evaluation, then return to menu
        bot.send_message(chat_id, "✅ Notiert! Weiter üben? /practice — oder /menu")
        user_state[chat_id]["mode"] = "idle"
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
    if state.get("gem_exercise") and text:
        check_gem_exercise(chat_id, text, state["gem_text"])
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
        start_shadowing(chat_id)
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
            last_npc = last_bot_text.get(chat_id)

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
    else:
        bot.answer_callback_query(call.id)

# Stripe/webhook disabled for stability — re-enable later

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))  # set your Telegram ID in Railway

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
        if telegram_id and str(telegram_id) in user_data:
            uid = str(telegram_id)
            user_data[uid]["premium"]                = True
            user_data[uid]["stripe_customer_id"]     = customer_id
            user_data[uid]["stripe_subscription_id"] = sub_id
            save_users(user_data)
            log.info(f"✅ Premium activated: {telegram_id}")
            try:
                bot.send_message(int(telegram_id),
                    "🎉 *Willkommen im Premium-Club!*\n\n"
                    "Du hast jetzt vollen Zugriff auf alles. 💪\n"
                    "Dein Streak und deine XP sind natürlich noch da.\n\n"
                    "Tippe /themen um weiterzumachen!",
                    parse_mode="Markdown")
            except Exception as e:
                log.warning(f"Could not notify {telegram_id}: {e}")

    elif event["type"] == "customer.subscription.deleted":
        customer_id = event["data"]["object"].get("customer")
        for uid, user in user_data.items():
            if user.get("stripe_customer_id") == customer_id:
                user_data[uid]["premium"]                = False
                user_data[uid]["stripe_subscription_id"] = None
                save_users(user_data)
                try:
                    bot.send_message(int(uid),
                        "😢 Dein Premium-Abo wurde gekündigt.\n"
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
