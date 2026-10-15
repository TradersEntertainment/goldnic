from __future__ import annotations

import logging
import threading
from datetime import time

from telegram.ext import Application, ContextTypes

from . import handlers
from .config import load_config
from .db import Database
from .messenger import TelegramMessenger
from .reminders import daily_job


def _start_web(db, cfg) -> None:
    from waitress import serve

    from .web import create_app

    flask_app = create_app(db, cfg)
    t = threading.Thread(
        target=lambda: serve(flask_app, host="0.0.0.0", port=cfg.port, threads=4),
        name="web", daemon=True,
    )
    t.start()
    logging.getLogger("emlak_bot").info("Web paneli port %s üzerinde çalışıyor", cfg.port)


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    cfg = load_config()
    if not cfg.bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN tanımlı değil.")
    if not cfg.owner_ids and not cfg.setup_code:
        raise SystemExit("OWNER_TELEGRAM_IDS veya SETUP_CODE değişkenlerinden en az biri tanımlı olmalı.")
    if not cfg.owner_ids:
        logging.getLogger("emlak_bot").warning(
            "Ofis sahibi tanımlı değil: sahip botta /kurulum KURULUM_KODU yazarak kaydolmalı.")

    app = Application.builder().token(cfg.bot_token).build()
    db = Database(cfg.db_path)
    logging.getLogger("emlak_bot").info("Veritabanı: %s", cfg.db_path)
    messenger = TelegramMessenger(app.bot)
    app.bot_data.update(db=db, cfg=cfg, messenger=messenger)
    handlers.register(app)

    async def job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await daily_job(db, messenger, cfg.inactivity_days, cfg.group_chat_id or None)

    # Her gün sabah; kaçırılan günler için açılışta da bir kez çalışır (tekrar göndermez)
    app.job_queue.run_daily(job, time=time(cfg.reminder_hour, 0, tzinfo=cfg.tz), name="daily")
    app.job_queue.run_once(job, when=15, name="startup-catchup")

    async def on_error(update, ctx):
        logging.getLogger("emlak_bot").exception("Hata", exc_info=ctx.error)

    app.add_error_handler(on_error)
    if cfg.web_enabled:
        _start_web(db, cfg)
    app.run_polling()


if __name__ == "__main__":
    main()
