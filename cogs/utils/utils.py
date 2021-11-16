import os
import json


def get_pics_path():
    """Returns the path to the pictures folder """
    script_dir = os.path.dirname(__file__)
    return os.path.join(script_dir, 'pictures')


def load_credentials():
    with open('config.json') as f:
        return json.load(f)


def load_json(file_name):
    with open(file_name, 'r') as f:
        return json.load(f)


def dump_json(file_name, data):
    with open(file_name, 'w') as f:
        return json.dump(data, f)
