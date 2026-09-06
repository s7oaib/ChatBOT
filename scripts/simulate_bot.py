"""
Interactive CLI Simulator for Camellia Artisanal Bot.
Allows testing Instagram (Ad vs Organic DMs) and WhatsApp messages locally.
"""
import sys
from camellia.database import db
from camellia.ad_gate import ad_gate
from camellia.responder import responder
from camellia.whatsapp_adapter import whatsapp_adapter
from camellia.escalator import escalator

# Ensure Unicode stdout compatibility on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

def main():
    print("=" * 60)
    print("CAMELLIA ARTISANAL — BOT INTERACTIVE SIMULATOR")
    print("=" * 60)
    print("Select Testing Mode:")
    print("1. Instagram DM (Ad-Originated Referral) [Auto-Reply Enabled]")
    print("2. Instagram DM (Organic Non-Ad) [Auto-Reply Blocked by Ad-Gate]")
    print("3. WhatsApp Business Message [Auto-Reply Enabled]")
    print("4. View Live Analytics & Recorded Escalations")
    print("Q. Quit")
    print("-" * 60)

    mode = "1"
    
    while True:
        try:
            choice = input(f"\n[Mode {mode}] Enter message (or 'mode 1/2/3/4', 'q' to quit): ").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not choice:
            continue

        lowered = choice.lower()
        if lowered in ["q", "quit", "exit"]:
            print("Exiting simulator. Good luck!")
            break

        if lowered in ["mode 1", "1"]:
            mode = "1"
            print("--> Switched to Mode 1: Instagram DM (Ad-Originated).")
            continue
        elif lowered in ["mode 2", "2"]:
            mode = "2"
            print("--> Switched to Mode 2: Instagram DM (Organic / Non-Ad).")
            continue
        elif lowered in ["mode 3", "3"]:
            mode = "3"
            print("--> Switched to Mode 3: WhatsApp Business Message.")
            continue
        elif lowered in ["mode 4", "4"]:
            print("\n📊 LIVE DATABASE ANALYTICS:")
            analytics = db.get_analytics()
            for k, v in analytics.items():
                print(f"  • {k}: {v}")
            
            print("\n🚨 RECENT HUMAN ESCALATIONS:")
            escalations = db.get_escalations(limit=5)
            if not escalations:
                print("  (No escalations recorded yet)")
            for esc in escalations:
                print(f"  #{esc['id']} [{esc['channel'].upper()}] User: {esc['user_id']} | Reason: {esc['reason']}")
                print(f"     Message: \"{esc['user_message']}\"")
            continue

        user_message = choice

        print("\n" + "-" * 50)
        if mode == "1":
            # Simulate Instagram Ad-originated DM
            event = {
                "sender": {"id": "simulated_ad_customer_77"},
                "message": {
                    "text": user_message,
                    "referral": {"source": "ADS", "ad_id": "sim_ad_101"}
                }
            }
            is_ad, sender, _, ad_id = ad_gate.inspect_and_gate_message(event)
            print(f"🔎 [AD-GATE]: Qualified={is_ad} (Source=ADS, Ad_ID={ad_id})")

            resp_text, was_matched, tmpl_id, img_url = responder.match_and_respond(user_message, sender, "instagram")
            db.log_conversation(sender, "instagram", user_message, resp_text, was_matched, tmpl_id)

            if not was_matched:
                esc_res = escalator.handle_escalation(sender, "instagram", user_message, "Unmatched Query", {"ad_id": ad_id})
                print(f"🚨 [ESCALATED TO HUMAN]: Escalation ID #{esc_res['escalation_id']}")
                print(f"🤖 [BOT HOLDING RESPONSE]:\n{resp_text}")
            else:
                print(f"✅ [MATCHED TEMPLATE]: '{tmpl_id}'")
                if img_url:
                    print(f"🖼️ [MENU PHOTO ATTACHED]: {img_url}")
                print(f"🤖 [BOT RESPONSE]:\n{resp_text}")

        elif mode == "2":
            # Simulate Organic Instagram DM (No Referral)
            event = {
                "sender": {"id": "simulated_organic_customer_88"},
                "message": {"text": user_message}
            }
            is_ad, sender, _, _ = ad_gate.inspect_and_gate_message(event)
            print(f"🛑 [AD-GATE]: Qualified={is_ad} (No Ad Referral found)")
            print("🚫 [BOT ACTION]: AUTO-REPLY BLOCKED! Message sits in Inbox for manual human reply.")

        elif mode == "3":
            # Simulate WhatsApp Message
            sender_phone = "+919876543210"
            result = whatsapp_adapter.process_incoming_message(sender_phone, user_message)
            print(f"📱 [WHATSAPP PROCESSOR]: Matched={result['was_matched']} (Template={result['template_id']})")
            if not result['was_matched']:
                print("🚨 [ESCALATED TO HUMAN]: Recorded in database & human alert dispatched.")
                print(f"🤖 [BOT HOLDING RESPONSE]:\n{result['response_text']}")
            else:
                print(f"🤖 [BOT RESPONSE]:\n{result['response_text']}")
        
        print("-" * 50)

if __name__ == "__main__":
    main()
