from app.telegram_bot import TelegramBot

if __name__ == "__main__":
    TelegramBot().send_test_message()
    print("Test message sent; token was not printed.")
