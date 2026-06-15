# ═══════════════════════════════════════════════════════════════════════════════
# SCHRITTE 4 + 5 — Stripe Webhook + Stars Payment Handler
# Diese zwei Funktionen ersetzen die gleichnamigen in deiner bot.py.
# Einfach 1:1 finden & ersetzen.
# ═══════════════════════════════════════════════════════════════════════════════


# ── SCHRITT 4 — stripe_webhook() ───────────────────────────────────────────
# Ersetzt die bestehende stripe_webhook() Funktion komplett.
# Änderungen: plan-Erkennung aus Metadata + premium_plus Flag setzen.

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
        plan        = session.get("metadata", {}).get("plan", "standard")  # "plus" oder "standard"

        if telegram_id and str(telegram_id) in user_data:
            uid = str(telegram_id)
            user_data[uid]["premium"]                = True
            user_data[uid]["stripe_customer_id"]     = customer_id
            user_data[uid]["stripe_subscription_id"] = sub_id

            if plan == "plus":
                user_data[uid]["premium_plus"] = True
                log.info(f"✅ Premium PLUS activated via Stripe: {telegram_id}")
                welcome_msg = (
                    "🎉 *Willkommen im Premium Plus Club!*\n\n"
                    "Du hast jetzt vollen Zugang — inkl. Quatschen-Modus. 🗣️\n"
                    "Dein Streak und deine XP sind natürlich noch da.\n\n"
                    "Tippe /themen oder fang einfach an zu quatschen!"
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
                user_data[uid]["premium_plus"]           = False  # ← NEU
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


# ── SCHRITT 5 — handle_successful_payment() ────────────────────────────────
# Ersetzt die bestehende handle_successful_payment() Funktion komplett.
# Änderungen: payload-Erkennung für premium_plus_ Prefix.

@bot.message_handler(content_types=["successful_payment"])
def handle_successful_payment(message):
    """Activate Premium or Premium Plus after successful Stars payment."""
    chat_id = message.chat.id
    uid     = str(chat_id)
    ensure_user(chat_id)

    from datetime import timedelta
    payload   = message.successful_payment.invoice_payload
    is_plus   = payload.startswith("premium_plus_")

    user_data[uid]["premium"]       = True
    user_data[uid]["premium_until"] = (datetime.now() + timedelta(days=30)).isoformat()
    user_data[uid]["stars_payment"] = True

    if is_plus:
        user_data[uid]["premium_plus"]       = True
        user_data[uid]["premium_plus_until"] = (datetime.now() + timedelta(days=30)).isoformat()
        log.info(f"✅ Stars Premium PLUS activated: {chat_id}")
        bot.send_message(chat_id,
            "⭐ Danke für deine Stars!\n\n"
            "🎉 Du hast jetzt 30 Tage *Premium Plus* — inkl. Quatschen-Modus. 🗣️\n"
            "Dein Streak und deine XP sind natürlich noch da.\n\n"
            "Tippe /themen oder fang einfach an zu quatschen!",
            parse_mode="Markdown")
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
