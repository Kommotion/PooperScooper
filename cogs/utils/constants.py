"""
Constants file
"""
import os
from pathlib import Path

VERSION = '2.5.0'
BOT_AUTHOR = 'kommotion'
POOPERSCOOPER_PICTURE = 'pooperscooper.jpg'
COOKS_AND_MOCHA = 'cooksandmocha.jpg'
COOKS = 'cooks.jpg'
GOOD_BOYS_AND_GIRLS = 'goodboysandgirls.jpg'

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

GAMETIME_JSON = str(DATA_DIR / "gametime.json")
BIRTHDAY_JSON = str(DATA_DIR / "birthday.json")
BINGO_JSON = str(DATA_DIR / "bingo.json")
GAME_PICKER_JSON = str(DATA_DIR / "game_picker.json")

LEGACY_DATA_FILES = {
    "gametime.json": Path(GAMETIME_JSON),
    "birthday.json": Path(BIRTHDAY_JSON),
    "bingo.json": Path(BINGO_JSON),
    "game_picker.json": Path(GAME_PICKER_JSON),
}

SECONDS_IN_HOUR = 3600
MINUTES_IN_HOUR = 60
THIRTY_SECONDS = 30
HOURS_IN_DAY = 24
ONE_HOUR_IN_SECONDS = 1 * SECONDS_IN_HOUR
SECONDS_IN_DAY = SECONDS_IN_HOUR * HOURS_IN_DAY
THUMBS_UP_EMOJI = '👍'
CUDA = "cuda"
PALWORLD_JSON = 'palworld.json'
base_dir = os.path.dirname(os.path.abspath(__file__))
PALWORLD_UTIL_PATH = os.path.join(base_dir, "palworld_utils")

YES = 'yes'
NO = 'No'