"""Start here. Background modes don't load the window at all (less memory):
    DiscordMessageCleaner.exe              -> the app
    DiscordMessageCleaner.exe --logger     -> deleted-messages logger (no window)
    DiscordMessageCleaner.exe --run-rules  -> auto-clean rules (no window)
"""
import sys


def run():
    if "--logger" in sys.argv:
        import logger_service
        logger_service.main()
    elif "--run-rules" in sys.argv:
        import rules
        rules.run_headless()
    else:
        import app
        app.main()


if __name__ == "__main__":
    run()
