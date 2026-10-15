# Emlak Portföy Asistanı
import os
import time

# Türkiye saat dilimi (Europe/Istanbul - UTC+3)
os.environ.setdefault("TZ", "Europe/Istanbul")
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass

