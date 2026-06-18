from app.telegram_bot import TelegramBot

if __name__ == "__main__":
    for update in TelegramBot().get_updates():
        message = update.get("message", {})
        sender = message.get("from", {})
        chat = message.get("chat", {})
        print({"update_id": update.get("update_id"), "user_id": sender.get("id"), "chat_id": chat.get("id")})
