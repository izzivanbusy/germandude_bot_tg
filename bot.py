# ═══════════════════════════════════════════════════════════════════════════════
# GERMAN DUDE BOT — PATCH v3 (ZWEI-TIER-MODELL)
# Korrekte Tier-Logik: Premium (€20) vs. Premium Plus (€30)
# + Deutsch-Fingerabdruck + Free Daily Tier (3/Tag)
#
# TIER-ÜBERSICHT:
#   Free         → 3 Gespräche/Tag (Szenarien + Quatschen, gemeinsamer Pool)
#   Premium      → Szenarien unlimitiert | Quatschen NICHT enthalten
#   Premium Plus → Alles unlimitiert inkl. Quatschen
# ═══════════════════════════════════════════════════════════════════════════════


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 1 — ensure_user() anpassen
#
# Im initial-Dict (uid not in user_data) ergänzen:
#   "daily_convos":       {},
#   "premium_plus":       False,
#   "premium_plus_until": None,
#
# Im backfill-Block ergänzen:
#   user_data[uid].setdefault("daily_convos", {})
#   user_data[uid].setdefault("premium_plus", False)
#   user_data[uid].setdefault("premium_plus_until", None)
# ───────────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 2 — is_premium_plus() — direkt nach is_premium() einfügen
# ───────────────────────────────────────────────────────────────────────────────

def is_premium_plus(chat_id):
    """True wenn User Premium Plus hat (Quatschen + alles aus Premium, unlimitiert)."""
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
    # Trial mit Plus-Plan
    if user.get("trial_plan") == "plus":
        trial_start = user.get("trial_start")
        if not trial_start:
            return False
        trial_days = TRIAL_CODES.get(user.get("trial_code_used", ""), 3)
        days_used  = (datetime.now() - datetime.fromisoformat(trial_start)).days
        return days_used < trial_days
    return False

# HINWEIS: is_premium() bleibt unverändert.
# is_premium() = True für BEIDE Tiers (Premium + Premium Plus).
# Für "darf Quatschen?": is_premium_plus() OR free_with_remaining


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 3 — DAILY FREE TIER SYSTEM
# Einfügen nach der PAYWALL / SUBSCRIPTION SYSTEM Sektion (~Zeile 2270)
# ───────────────────────────────────────────────────────────────────────────────

FREE_DAILY_LIMIT = 3  # kostenlose Gespräche pro Tag (Szenarien + Quatschen, gemeinsam)


def _get_today() -> str:
    """Heutiges Datum als YYYY-MM-DD String."""
    return datetime.now().strftime("%Y-%m-%d")


def get_daily_convo_count(chat_id: int) -> int:
    """Wie viele Gespräche hat der User heute schon gestartet?"""
    uid = str(chat_id)
    dc  = user_data.get(uid, {}).get("daily_convos", {})
    if dc.get("date") != _get_today():
        return 0
    return dc.get("count", 0)


def increment_daily_convo(chat_id: int) -> int:
    """Zähler erhöhen. Gibt neuen Stand zurück."""
    uid   = str(chat_id)
    today = _get_today()
    dc    = user_data.get(uid, {}).get("daily_convos", {})
    count = dc.get("count", 0) if dc.get("date") == today else 0
    user_data[uid]["daily_convos"] = {"date": today, "count": count + 1}
    save_users(user_data)
    return count + 1


def has_free_convos_remaining(chat_id: int) -> bool:
    """True wenn User noch Gratis-Gespräche für heute hat."""
    return get_daily_convo_count(chat_id) < FREE_DAILY_LIMIT


def gate_scenario(chat_id: int) -> bool:
    """
    Gate für Szenarien (start_scenario / launch_scenario).
    Premium ODER Premium Plus → immer erlaubt.
    Free mit verbleibenden Gesprächen → erlaubt.
    Free limit erreicht → Paywall senden, False zurückgeben.

    VERWENDUNG am Anfang von start_scenario():
        if not gate_scenario(chat_id):
            return
        increment_daily_convo(chat_id)
    """
    if is_premium(chat_id):  # deckt Premium + Premium Plus ab
        return True
    if has_free_convos_remaining(chat_id):
        return True
    send_daily_limit_paywall(chat_id)
    return False


def gate_quatschen(chat_id: int) -> bool:
    """
    Gate speziell für den Quatschen-Modus.
    Premium Plus → immer erlaubt (unlimitiert).
    Free mit verbleibenden Gesprächen → erlaubt (zählt als 1 der 3/Tag).
    Free limit erreicht → Paywall mit Plus-Fokus.
    Regular Premium (kein Plus) → Upgrade-Prompt, KEIN Zugang.

    VERWENDUNG am Anfang von start_quatschen():
        if not gate_quatschen(chat_id):
            return
        if not is_premium_plus(chat_id):
            increment_daily_convo(chat_id)  # nur Free-User zählen
    """
    if is_premium_plus(chat_id):
        return True
    if is_premium(chat_id):  # Premium aber kein Plus
        send_quatschen_upgrade_prompt(chat_id)
        return False
    # Free User
    if has_free_convos_remaining(chat_id):
        return True
    send_daily_limit_paywall(chat_id)
    return False


def send_daily_limit_paywall(chat_id: int):
    """Paywall nach 3 kostenlosen Gesprächen — zeigt beide Tiers."""
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
        f"🔒 Alle *{FREE_DAILY_LIMIT} kostenlosen Gespräche* für heute genutzt"
        f"{', ' + name if name else ''}!\n\n"
        f"{xp_line} — schad das jetzt zu stoppen.\n\n"
        "📅 *Morgen gibt's automatisch 3 neue.* Versprochen.\n\n"
        "Oder jetzt upgraden:\n\n"
        "💼 *Premium — €20/Monat*\n"
        "Szenarien unlimitiert + Übungen + Achievements\n\n"
        "🗣️ *Premium Plus — €30/Monat*\n"
        "Alles aus Premium + Quatschen-Modus unlimitiert\n\n"
        "_Hast du einen Code? /freecode DEINCODE_"
    )
    last_bot_text[chat_id] = text

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🗣️ Premium Plus — €30/Monat", callback_data="pay_plus"))
    markup.add(InlineKeyboardButton("💼 Premium — €20/Monat", url=checkout_url))
    markup.add(InlineKeyboardButton("⭐ Stars zahlen", callback_data="pay_stars"))
    markup.add(InlineKeyboardButton("🎁 Freunde einladen → 3 Tage gratis", url=share_url))
    markup.add(InlineKeyboardButton("🌍 übersetzen", callback_data="translate_last"))

    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)


def send_quatschen_upgrade_prompt(chat_id: int):
    """Für reguläre Premium-User die Quatschen probieren wollen."""
    uid  = str(chat_id)
    name = user_data.get(uid, {}).get("name", "")
    text = (
        f"🗣️ *Quatschen ist Teil von Premium Plus*{', ' + name if name else ''}.\n\n"
        "Dein persönlicher deutscher Kumpel — kein Skript, kein Szenario, "
        "einfach reden. Über alles was dich gerade beschäftigt. Jeden Tag.\n\n"
        "Du bist auf Premium. Ein Upgrade reicht:\n"
        "🗣️ *Premium Plus — €30/Monat*"
    )
    last_bot_text[chat_id] = text
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🗣️ Jetzt auf Premium Plus upgraden", callback_data="pay_plus"))
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
            "\n\n_Das war dein letztes kostenloses Gespräch heute. "
            "Morgen gibt's 3 neue — oder /premium für unlimitiert._"
        )
    return f"\n\n_💬 Noch *{remaining}* kostenloses{'e' if remaining > 1 else ''} Gespräch{'e' if remaining > 1 else ''} heute übrig._"


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 4 — STRIPE WEBHOOK anpassen
# Im Block "checkout.session.completed":
#
#   plan = session.get("metadata", {}).get("plan", "standard")
#   if plan == "plus":
#       user_data[uid]["premium"]             = True
#       user_data[uid]["premium_plus"]        = True
#   else:
#       user_data[uid]["premium"]             = True
#       user_data[uid]["premium_plus"]        = False
#
# Im Block "customer.subscription.deleted":
#   Nach user_data[uid]["premium"] = False hinzufügen:
#   user_data[uid]["premium_plus"] = False
# ───────────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 5 — STARS PAYMENT HANDLER anpassen
# In handle_successful_payment():
#
#   payload = message.successful_payment.invoice_payload
#   uid = str(chat_id)
#   if payload.startswith("premium_plus_"):
#       user_data[uid]["premium"]      = True
#       user_data[uid]["premium_plus"] = True
#   elif payload.startswith("premium_"):
#       user_data[uid]["premium"]      = True
#       user_data[uid]["premium_plus"] = False
# ───────────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 6 — start_scenario() — Gate oben einfügen
#
#   def start_scenario(chat_id, scenario):
#       if not gate_scenario(chat_id):       # ← NEU
#           return                            # ← NEU
#       increment_daily_convo(chat_id)        # ← NEU
#       # ... rest unverändert
# ───────────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 7 — start_quatschen() — Gate oben einfügen
#
#   def start_quatschen(chat_id):
#       if not gate_quatschen(chat_id):           # ← NEU
#           return                                 # ← NEU
#       if not is_premium_plus(chat_id):           # ← NEU: Free-User zählen
#           increment_daily_convo(chat_id)         # ← NEU
#       # ... rest unverändert
# ───────────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 8 — themen_cmd() — Premium-Check löschen
#
#   LÖSCHEN:  if not is_premium(message.chat.id): send_paywall(message.chat.id); return
#
#   Resultat: Topics immer sichtbar. Gate läuft in start_scenario().
# ───────────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 9 — end_conversation() — Hinweis am Ende (optional)
#
#   remaining_hint = get_remaining_convos_hint(chat_id)
#   if remaining_hint:
#       bot.send_message(chat_id, remaining_hint, parse_mode="Markdown")
#   time.sleep(1.5)
#   send_topic_buttons(chat_id)
# ───────────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 10 — /setpremiumplus Admin-Befehl hinzufügen (optional)
#
# @bot.message_handler(commands=["setpremiumplus"])
# def admin_set_premium_plus(message):
#     if message.chat.id != ADMIN_CHAT_ID: return
#     parts = message.text.strip().split()
#     if len(parts) < 2:
#         bot.send_message(message.chat.id, "Usage: /setpremiumplus CHAT_ID [days=30]"); return
#     target = parts[1].strip()
#     days   = int(parts[2]) if len(parts) > 2 else 30
#     if target not in user_data:
#         bot.send_message(message.chat.id, f"User {target} not found."); return
#     from datetime import timedelta
#     user_data[target]["premium"]             = True
#     user_data[target]["premium_plus"]        = True
#     user_data[target]["premium_plus_until"]  = (datetime.now() + timedelta(days=days)).isoformat()
#     user_data[target]["premium_until"]       = (datetime.now() + timedelta(days=days)).isoformat()
#     save_users(user_data)
#     bot.send_message(message.chat.id, f"Premium PLUS activated for {target} — {days} days.")
#     try:
#         bot.send_message(int(target),
#             "🎉 *Willkommen im Premium Plus Club!*\n\n"
#             f"{days} Tage vollen Zugang inkl. Quatschen-Modus. Los geht's! 🗣️",
#             parse_mode="Markdown")
#     except: pass
# ───────────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 11 — DEUTSCH-FINGERABDRUCK
# Einfügen nach dem GERMAN GEMS POOL Block (~Zeile 1128)
# In finish_test() die Zeile send_level_feedback(chat_id, final_level)
# ersetzen durch:
#   send_deutsch_fingerabdruck(chat_id, final_level, scores, attempts, wrong_answers)
# ───────────────────────────────────────────────────────────────────────────────

# Grammatische Themen pro Frage-ID
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

# 4-Wochen-Pläne pro Lernziel
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
        "Hotel: Check-in, Probleme melden, Wünsche & Sonderwünsche äußern",
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

# Neurowissenschaftliche Erklärungen — werden nach Fehler-Leveln ausgewählt
_NEURO_EXPLANATIONS = [
    (
        ["A2", "B1"],  # levels mit Konjunktiv-Fehlern
        "Der Konjunktiv II ist kein Regelwerk — er ist ein Muster-Netzwerk. "
        "Weil du ihn selten im natürlichen Input hörst, hat dein Gehirn ihn noch nicht als "
        "automatischen Chunk gespeichert. Der Hippocampus braucht wiederholten, "
        "emotionalen Kontakt mit echten Sätzen — nicht mit Grammatiktabellen."
    ),
    (
        ["A1"],  # Relativpronomen, Infinitiv
        "A1-Strukturen wie Relativpronomen fordern dein Arbeitsgedächtnis besonders stark: "
        "Du musst zwei Satzteile gleichzeitig im Kopf halten — das ist kognitive Last. "
        "Dein Gehirn lernt das am schnellsten durch kurze Chunks: erst 'der Mann, der...' — "
        "dann der ganze Satz. Erst Muster, dann Regel."
    ),
    (
        ["B1", "B2"],  # Trennbare Verben, Vokabular
        "Trennbare Verben sind echte Gehirn-Fallen: Das Präfix landet am Satzende, "
        "aber keine andere Sprache kennt dieses Muster. Dein Gehirn sucht das vollständige Wort "
        "und findet es nicht. Lösung: 'anrufen' nie isoliert lernen — immer als Chunk: "
        "'ich rufe AN'. Das Bewegungsmuster muss sich festigen."
    ),
    (
        ["B2", "C1"],  # Wortbildung, akademisches Vokabular
        "Formelles und akademisches Vokabular aktiviert deinen semantischen Speicher — "
        "aber der ist noch dünn besetzt, weil du vermutlich mehr informales Deutsch hörst. "
        "Das Gehirn baut Bedeutungs-Netze durch wiederholten Kontakt im Kontext, nicht durch "
        "Vokabellisten. 15 Minuten authentische Texte täglich wirken stärker als 1 Stunde Pauken."
    ),
    (
        [],  # default
        "Dein Gehirn befindet sich in der Restrukturierungsphase — völlig typisch für dein Level. "
        "Neue deutsche Muster kämpfen mit alten Mustern aus deiner Muttersprache. "
        "Das ist keine Schwäche, das ist Neuroplastizität bei der Arbeit. "
        "10 Minuten tägliches aktives Sprechen ist wissenschaftlich die effektivste Form "
        "des Spracherwerbs — deutlich stärker als passives Lesen oder Grammatikübungen."
    ),
]


def _pick_neuro_explanation(wrong_levels: list) -> str:
    """Wählt die treffendste neurobiologische Erklärung basierend auf Fehler-Levels."""
    for levels_trigger, explanation in _NEURO_EXPLANATIONS[:-1]:
        if any(lvl in wrong_levels for lvl in levels_trigger):
            return explanation
    return _NEURO_EXPLANATIONS[-1][1]  # default


def send_deutsch_fingerabdruck(
    chat_id: int,
    final_level: str,
    scores: dict,
    attempts: dict,
    wrong_answers: list,
):
    """
    Generiert und sendet den personalisierten Deutsch-Fingerabdruck nach dem Level-Test.

    INTEGRATION: In finish_test() die Zeile
        send_level_feedback(chat_id, final_level)
    ERSETZEN durch:
        send_deutsch_fingerabdruck(chat_id, final_level, scores, attempts, wrong_answers)

    Die Funktion send_level_feedback() kann dann gelöscht oder behalten werden (wird nicht mehr aufgerufen).
    """
    uid  = str(chat_id)
    user = user_data.get(uid, {})
    name = user.get("name", "")
    goal = user.get("goal", "Selbstpräsentation")
    native_lang = user.get("native_language") or "Englisch"

    # ── Performance-Analyse ─────────────────────────────────────────────────
    strong_levels, weak_levels, wrong_levels = [], [], []
    for lvl in ["A1", "A2", "B1", "B2", "C1"]:
        att  = attempts.get(lvl, 0)
        corr = scores.get(lvl, 0)
        if att == 0:
            continue
        acc = corr / att
        if acc >= 0.75:
            strong_levels.append(lvl)
        if acc < 0.50:
            weak_levels.append(lvl)

    # Fehler-Levels aus wrong_answers ergänzen
    wrong_levels = list({wa.get("level", "") for wa in wrong_answers if wa.get("level")})
    wrong_levels = [lvl for lvl in wrong_levels if lvl]

    strong_str = "Niveau " + " & ".join(strong_levels) if strong_levels else "Grundlagen gut verankert"
    weak_str   = "Niveau " + " & ".join(weak_levels)   if weak_levels   else "keine kritischen Lücken erkannt"
    neuro_text = _pick_neuro_explanation(wrong_levels)

    # ── 4-Wochen-Plan ───────────────────────────────────────────────────────
    plan_items = WEEKLY_PLANS.get(goal, WEEKLY_PLANS["Selbstpräsentation"])
    plan_str   = "\n".join(f"Woche {i+1}: {p}" for i, p in enumerate(plan_items))

    # ── Claude generiert den Fingerabdruck ──────────────────────────────────
    bot.send_chat_action(chat_id, "typing")

    prompt = f"""Erstelle einen personalisierten "Deutsch-Fingerabdruck" für {name or 'den User'}.

Eingangsdaten:
- Name: {name or 'der User'}
- Niveau (Testergebnis): {final_level}
- Muttersprache: {native_lang}
- Lernziel: {goal}
- Starke Bereiche: {strong_str}
- Lernfelder (Verbesserungspotenzial): {weak_str}
- Neurobiologische Erklärung (EXAKT so einbauen, nur natürlicher formulieren):
  {neuro_text}
- 4-Wochen-Plan (EXAKT diese Wocheninhalte verwenden):
{plan_str}

Schreibe den Fingerabdruck in diesem Format (Telegram Markdown: nur *fett* und _kursiv_, keine ## Header, keine ---):

🧠 *DEIN DEUTSCH-FINGERABDRUCK{', ' + name if name else ''}*

📊 *Niveau: {final_level}*
✅ *Stärken:* [konkret und ehrlich — 1 Satz]
🔧 *Lernfelder:* [konkret, 1 Satz, kein schulmeisterlicher Ton]

🔬 *Was dein Gehirn gerade macht:*
[Die neurobiologische Erklärung oben einarbeiten — warm, verständlich, max. 3 Sätze, kein Fachjargon]

🗓️ *Dein 4-Wochen-Plan: {goal}*
Woche 1: [aus den Daten oben]
Woche 2: [aus den Daten oben]
Woche 3: [aus den Daten oben]
Woche 4: [aus den Daten oben]

⚡ *Deine Formel:*
10 Minuten täglich sprechen = dein Gehirn baut neue Verbindungen. Nicht lernen. Sprechen.

Ton: warm, direkt, kein Motivational-Kitsch. {name or 'Der User'} soll das Gefühl haben: "Dieser Bot kennt mich wirklich."
Länge: knapp und substanzreich. Jede Zeile muss etwas sagen.
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
        # Strukturierter Fallback ohne Claude
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

    # ── Voice-Hinweis in Muttersprache + CTA-Button ─────────────────────────
    try:
        tr = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content":
                f"Translate into {native_lang}. Only return the translation, nothing else:\n\n"
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
        f"Du hast *{FREE_DAILY_LIMIT} kostenlose Gespräche täglich* — "
        f"Szenarien und Quatschen-Modus inklusive. Kein Code nötig.\n\n"
        f"📱 _{voice_hint}_\n\n"
        f"Bereit? 👇"
    )
    last_bot_text[chat_id] = cta_text
    bot.send_message(chat_id, cta_text, parse_mode="Markdown", reply_markup=markup)


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 4 — MODIFIZIERTE FUNKTIONEN
# Diese Versionen ersetzen die bestehenden Funktionen in bot.py
# ───────────────────────────────────────────────────────────────────────────────


# ── finish_test() — nur die EINE Zeile am Ende ändern ──────────────────────
# Suche in der bestehenden finish_test() diese Zeile:
#
#     send_level_feedback(chat_id, final_level)
#
# Ersetzen durch:
#
#     send_deutsch_fingerabdruck(chat_id, final_level, scores, attempts, wrong_answers)
#
# FERTIG. wrong_answers ist bereits als `wrong_answers = state.get("wrong_answers", [])` verfügbar.


# ── themen_cmd() — Premium-Check entfernen ─────────────────────────────────
# Die bestehende Funktion:
#
#     @bot.message_handler(commands=['themen'])
#     def themen_cmd(message):
#         ensure_user(message.chat.id)
#         if _require_onboarding(message.chat.id): return
#         _track_feature(message.chat.id, 'themen')
#         if not is_premium(message.chat.id): send_paywall(message.chat.id); return  # ← DIESE ZEILE LÖSCHEN
#         send_topic_buttons(message.chat.id)
#
# Resultat: Jeder darf die Themen sehen. Der Gate läuft jetzt in start_scenario().


# ── start_scenario() — Gate + Counter am Anfang einfügen ───────────────────
# Die ERSTEN ZEILEN der bestehenden start_scenario() Funktion:
#
#     def start_scenario(chat_id, scenario):
#         # ... (was auch immer jetzt da steht)
#
# ERSETZEN durch:
#
#     def start_scenario(chat_id, scenario):
#         # ── FREE TIER GATE ───────────────────────────────────────────────
#         if not gate_conversation(chat_id):
#             return
#         increment_daily_convo(chat_id)
#         # ────────────────────────────────────────────────────────────────
#         # ... rest der Funktion unverändert


# ── start_quatschen() — Gate + Counter am Anfang einfügen ──────────────────
# Die ERSTEN ZEILEN der bestehenden start_quatschen() Funktion:
#
#     def start_quatschen(chat_id):
#         user  = user_data.get(str(chat_id), {})
#         ...
#
# ERSETZEN durch:
#
#     def start_quatschen(chat_id):
#         # ── FREE TIER GATE ───────────────────────────────────────────────
#         if not gate_conversation(chat_id):
#             return
#         increment_daily_convo(chat_id)
#         # ────────────────────────────────────────────────────────────────
#         user  = user_data.get(str(chat_id), {})
#         ... rest der Funktion unverändert


# ── end_conversation() — Remaining-Hint am Ende ────────────────────────────
# Optional: Nach dem Share-Button-Block in end_conversation(), vor send_topic_buttons():
#
#     # Hinweis auf verbleibende Gespräche (nur für Free-User)
#     remaining_hint = get_remaining_convos_hint(chat_id)
#     if remaining_hint:
#         bot.send_message(chat_id, remaining_hint, parse_mode="Markdown")
#
#     time.sleep(1.5)
#     send_topic_buttons(chat_id)


# ───────────────────────────────────────────────────────────────────────────────
# SCHRITT 5 — ZUSAMMENFASSUNG DER ÄNDERUNGEN
#
# Was sich ändert:
# 1. Jeder neue User bekommt 3 Gespräche/Tag GRATIS — kein Code nötig
# 2. Nach dem Level-Test: Deutsch-Fingerabdruck statt nacktem "Du bist B1"
# 3. /themen zeigt Topics ohne Paywall — Gate erst beim Start des Gesprächs
# 4. Trial-Codes funktionieren weiterhin (geben premium=True für N Tage → unlimitiert)
# 5. Premium = unlimitiert + /practice + /flashcards (unverändert)
#
# Was NICHT sich ändert:
# - is_premium() Logik (unverändert)
# - Trial-Code-System (unverändert, jetzt als Bonus-Tier für Partner)
# - /practice und /flashcards bleiben Premium-only
# - /gem, /integration bleiben kostenlos (unverändert)
# - Stripe + Stars Zahlungsfluss (unverändert)
# - Alle Szenarien, Prompts, Personas (unverändert)
#
# Neue User-Journey:
#   /start → Name → Sprache → Ziel → Test (12 Fragen) →
#   🧠 DEUTSCH-FINGERABDRUCK (neu!) →
#   Erstes Gespräch (1 von 3 täglich) →
#   ... 3 Gespräche genutzt →
#   Freundliche Paywall: "Morgen 3 neue / oder Premium"
# ───────────────────────────────────────────────────────────────────────────────
