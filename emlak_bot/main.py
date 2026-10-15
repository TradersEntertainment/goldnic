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
    # httpx istek URL'sini (içinde bot token'ı var) INFO seviyesinde loglar; sızmasın
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config()

    db = Database(cfg.db_path)
    logging.getLogger("emlak_bot").info("Veritabanı: %s", cfg.db_path)

    # Sahibinden portföy ilk yükleme (64 ilan) & arka plan otomatik senkronizasyon (0 server maliyeti)
    from .scraper import seed_initial_listings, start_auto_sync_thread
    try:
        seed_res = seed_initial_listings(db)
        logging.getLogger("emlak_bot").info("Sahibinden portföy durumu: %s", seed_res)
        start_auto_sync_thread(db)
    except Exception as e:
        logging.getLogger("emlak_bot").warning("Sahibinden tohumlama/senkronizasyon hatası: %s", e)

    # Web yöneticisi env üzerinden verilmişse oluştur/güncelle
    if cfg.admin_username and cfg.admin_password:
        admin_id = db.seed_admin(cfg.admin_username, cfg.admin_password)
        logging.getLogger("emlak_bot").info("Web yöneticisi aktif: %s (id: %s)", cfg.admin_username, admin_id)
    merged = db.consolidate_duplicate_users()
    if merged:
        logging.getLogger("emlak_bot").info("%s mükerrer danışman kaydı gerçek hesaplarla birleştirildi", merged)

    if cfg.chat_id:
        logging.getLogger("emlak_bot").info("Telegram bildirim hedefi (CHAT_ID): %s", cfg.chat_id)

    if cfg.web_enabled:
        _start_web(db, cfg)

    # Saat dilimine duyarlı günlük görevler (sunucu UTC olsa da İstanbul saatine göre çalışır)
    import asyncio

    from . import jobs
    from .scheduler import Scheduler

    sched = Scheduler(db, cfg.tz)
    sched.add("tufe", cfg.tufe_refresh_hour, 5, lambda: jobs.tufe_job(db, cfg))
    if not cfg.bot_token:
        sched.add("daily", cfg.reminder_hour, 0,
                  lambda: asyncio.run(daily_job(db, messenger=None, inactivity_days=cfg.inactivity_days, cfg=cfg)))
    sched.start()

    if not cfg.bot_token:
        logging.getLogger("emlak_bot").warning(
            "TELEGRAM_BOT_TOKEN tanımlı değil. Web paneli ve WhatsApp hatırlatma servisi çalışıyor."
        )
        import time as _t

        try:
            while True:
                _t.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            return

    # Telegram botu mevcutsa başlat
    app = Application.builder().token(cfg.bot_token).build()
    messenger = TelegramMessenger(app.bot)
    app.bot_data.update(db=db, cfg=cfg, messenger=messenger)
    handlers.register(app)

    target_chat = cfg.chat_id or cfg.group_chat_id or None

    async def job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await daily_job(db, messenger, cfg.inactivity_days, target_chat, cfg=cfg)

    app.job_queue.run_daily(job, time=time(cfg.reminder_hour, 0, tzinfo=cfg.tz), name="daily")
    app.job_queue.run_once(job, when=15, name="startup-catchup")

    async def on_error(update, ctx):
        logging.getLogger("emlak_bot").exception("Hata", exc_info=ctx.error)

    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
