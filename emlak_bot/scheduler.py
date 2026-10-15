"""Saat dilimine duyarlı, yeniden başlatmaya dayanıklı günlük görev zamanlayıcı.

- Görevler ofisin saat dilimine göre (Europe/Istanbul) çalışır; sunucunun UTC olması önemsizdir.
- Son çalışma günü veritabanında tutulur: aynı gün ikinci kez çalışmaz (yeniden başlatma sonrası
  çift hatırlatma / çift yedek olmaz).
- Sunucu planlanan saatte kapalıysa, açıldığında o günün görevi hemen telafi edilir ("Sıfır Olay Kaçırma").
"""
from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, time
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class DailyTask:
    name: str
    at: time
    fn: Callable[[], object]


class Scheduler:
    def __init__(self, db, tz):
        self.db, self.tz = db, tz
        self.tasks: list[DailyTask] = []
        self._lock = threading.Lock()

    def add(self, name: str, hour: int, minute: int, fn: Callable[[], object]) -> None:
        self.tasks.append(DailyTask(name, time(hour % 24, minute % 60), fn))

    def last_run(self, name: str) -> str | None:
        return self.db.get_setting(f"sched_last_{name}")

    def tick(self, now: datetime | None = None) -> list[str]:
        """Vakti gelmiş ve bugün çalışmamış görevleri çalıştırır; çalışanların adlarını döner."""
        now = now.astimezone(self.tz) if now and now.tzinfo else (now or datetime.now(self.tz))
        ran: list[str] = []
        with self._lock:
            for t in self.tasks:
                if now.time() < t.at or self.last_run(t.name) == now.date().isoformat():
                    continue
                # Önce işaretle: görev hata verse bile aynı gün sonsuz döngüde tekrar denenmez
                self.db.set_setting(f"sched_last_{t.name}", now.date().isoformat())
                try:
                    t.fn()
                    ran.append(t.name)
                except Exception:  # noqa: BLE001 - bir görevin hatası diğerlerini durdurmamalı
                    log.exception("Zamanlanmış görev hata verdi: %s", t.name)
                    self.db.set_setting(f"sched_error_{t.name}", now.isoformat(timespec="seconds"))
        return ran

    def start(self, interval: int = 60, initial_delay: int = 20) -> threading.Thread:
        def loop():
            _time.sleep(initial_delay)
            while True:
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    log.exception("Zamanlayıcı döngüsü hatası")
                _time.sleep(interval)

        th = threading.Thread(target=loop, name="scheduler", daemon=True)
        th.start()
        log.info("Zamanlayıcı başlatıldı: %s", ", ".join(f"{t.name}@{t.at:%H:%M}" for t in self.tasks))
        return th
