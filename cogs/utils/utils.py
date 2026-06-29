import os
import time
from pathlib import Path

from cogs.utils.config import load_config
from cogs.utils.constants import SECONDS_IN_HOUR


def get_pics_path():
    """Returns the path to the pictures folder """
    script_dir = os.path.dirname(__file__)
    return os.path.join(script_dir, 'pictures')


def load_credentials():
    """Return validated config as a dict for legacy callers."""
    return load_config().to_credentials_dict()


def calculate_hours_elapsed(last_time):
    return (time.time() - last_time) / SECONDS_IN_HOUR