import configparser
import datetime
import logging
import random
import time
from pathlib import Path

import st7789
from gpiozero import Button
from PIL import Image, ImageDraw, ImageFont

from eversense_client import EversenseClient
from glucose_db import GlucoseDB

CONFIG_DIR = Path.home() / ".config" / "eversense-zero"
LOG_DIR = CONFIG_DIR / "logs"

if not CONFIG_DIR.exists():
    CONFIG_DIR.mkdir(parents=True)

if not LOG_DIR.exists():
    LOG_DIR.mkdir(parents=True)

DISPLAY_WIDTH = 320
DISPLAY_HEIGHT = 240
ROTATION = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",  # Format log messages
    handlers=[
        logging.StreamHandler(),  # Logs to the console
        logging.FileHandler(LOG_DIR / "eversense-zero.log", mode="a"),  # Logs to a file
    ],
)

class GlucoseApp:
    CONFIG_FILE = CONFIG_DIR / "config.ini"
    DB_FILE = CONFIG_DIR / "glucose.db"

    LOW_THRESHOLD = 4.0
    HIGH_THRESHOLD = 15.0
    NORMAL_THRESHOLD_MIN = 5.0
    NORMAL_THRESHOLD_MAX = 10.0
    FETCH_INTERVAL_SEC = 5 * 60

    try:
        FONT_BIG = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
        FONT_MED = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
        FONT_SMALL = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
    except Exception:
        FONT_BIG = ImageFont.load_default()
        FONT_MED = ImageFont.load_default()
        FONT_SMALL = ImageFont.load_default()

    def __init__(self):
        self.config = configparser.ConfigParser()
        self.config.read(self.CONFIG_FILE)
        if not self.config.has_section("auth"):
            print("No credentials found, please add the config file first.")
            exit(1)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = EversenseClient(self.config["auth"]["username"], self.config["auth"]["password"])
        self.db = GlucoseDB(self.DB_FILE)
        self.disp = self.enable_display()
        self.logger.debug("[GlucoseApp] App initialized")
        self.user_id = None
        self.current_glucose = None
        self.trend_arrow = "→"
        self.updated_ts = None
        self.button_a = Button(5)
        self.button_a.when_pressed = self.on_button_a

    @classmethod
    def calculate_trend_arrow(cls, data_points):
        if len(data_points) < 2:
            return "→"

        # Use the earliest value at least 15 minutes before the last one
        latest_time, latest_val = data_points[-1]
        for i in range(len(data_points) - 2, -1, -1):
            prev_time, prev_val = data_points[i]
            delta_minutes = (latest_time - prev_time).total_seconds() / 60
            if delta_minutes >= 15:
                break
        else:
            return "→"  # Not enough spacing

        delta_val = latest_val - prev_val
        rate = delta_val / delta_minutes  # mmol/L per minute

        if rate >= 0.167:
            return "↑↑"
        elif rate >= 0.111:
            return "↑"
        elif rate <= -0.167:
            return "↓↓"
        elif rate <= -0.111:
            return "↓"
        else:
            return "→"

    @classmethod
    def enable_display(cls):
        disp = st7789.ST7789(
            width=DISPLAY_WIDTH,
            height=DISPLAY_HEIGHT,
            rotation=ROTATION,
            port=0,
            cs=0,
            dc=9,
            backlight=13,
            spi_speed_hz=80_000_000,
            rst=19
        )
        disp.begin()
        return disp

    @classmethod
    def on_button_a(cls):
        exit(0)

    def display_blood_sugar(self):
        if self.current_glucose is None:
            return

        # Create a full-screen image
        image = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        current_glucose = str(self.current_glucose)

        # Center the glucose value
        w, h = draw.textbbox((0, 0), current_glucose, font=self.FONT_BIG)[2:]
        x = (DISPLAY_WIDTH - w) // 2
        y = (DISPLAY_HEIGHT - h) // 2 - 20
        draw.text((x, y), current_glucose, font=self.FONT_BIG, fill=self.glucose_color())

        # Trend arrow below the value
        aw, ah = draw.textbbox((0, 0), self.trend_arrow, font=self.FONT_MED)[2:]
        ax = (DISPLAY_WIDTH - aw) // 2
        ay = y + h + 10
        draw.text((ax, ay), self.trend_arrow, font=self.FONT_MED, fill=(200, 200, 200))

        # Timestamp at bottom
        if self.updated_ts:
            ts_text = self.updated_ts
            tw, th = draw.textbbox((0, 0), ts_text, font=self.FONT_SMALL)[2:]
            tx = (DISPLAY_WIDTH - tw) // 2
            ty = DISPLAY_HEIGHT - th - 8
            draw.text((tx, ty), ts_text, font=self.FONT_SMALL, fill=(150, 150, 150))

        # Push to display
        self.disp.display(image)

    def glucose_color(self) -> tuple:
        # Return an RGB color matching your tray semantics
        if self.current_glucose < self.LOW_THRESHOLD or self.current_glucose > self.HIGH_THRESHOLD:
            return 255, 0, 0
        elif self.current_glucose < self.NORMAL_THRESHOLD_MIN or self.current_glucose > self.NORMAL_THRESHOLD_MAX:
            return 255, 215, 0
        else:
            return 0, 200, 0

    def load_events(self):
        # Load last 24h glucose data from API
        now = datetime.datetime.now(datetime.timezone.utc)
        from_dt = now - datetime.timedelta(hours=24)
        glucose_data = self.client.fetch_glucose_data(from_dt, now)
        if glucose_data:
            # Parse glucose points: adapt if API returns differently, here assuming list of events in glucose_data
            # We'll expect glucose_data to be a list of dicts with 'EventDate' and 'convertedValue'
            readings = []
            for event in glucose_data:
                try:
                    ts = event.get("EventDate")
                    val = event.get("convertedValue")
                    if ts and val is not None:
                        # Convert timestamp string to ISO format (remove timezone info if present)
                        if ts.endswith("Z"):
                            ts = ts[:-1]
                        # Some timestamps might have timezone, ensure isoformat without tz for DB
                        dt = datetime.datetime.fromisoformat(ts)
                        readings.append((dt.isoformat(), float(val)))
                except Exception as e:
                    self.logger.error(f"[Parse] Error parsing event: {e}")
            if readings:
                self.db.add_readings(readings)
                self.db.prune_old()
                last_points = self.db.get_last_24h()
                if last_points:
                    latest_ts, self.current_glucose = last_points[-1]
                    self.trend_arrow = self.calculate_trend_arrow(last_points)
                    self.updated_ts = latest_ts.astimezone().strftime("%H:%M")
                    self.display_blood_sugar()

    def run(self):
        while True:
            try:
                # Login + get user id if missing
                if not self.client.access_token or self.user_id is None:
                    if not self.client.login():
                        self.logger.debug("[FetchLoop] Login failed, retrying in 60s")
                        time.sleep(60)
                        continue
                    self.user_id = self.client.fetch_user_id()
                    if self.user_id is None:
                        self.logger.debug("[FetchLoop] Failed to get user ID, retrying in 60s")
                        time.sleep(60)
                        continue
                    self.client.user_id = self.user_id

                self.load_events()

            except Exception as e:
                self.logger.error(f"[FetchLoop] Error: {e}")
            # Sleep with jitter
            time.sleep(self.FETCH_INTERVAL_SEC + random.uniform(-30, 30))

