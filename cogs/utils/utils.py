import os


def get_pics_path():
    """Returns the path to the pictures folder """
    script_dir = os.path.dirname(__file__)
    return os.path.join(script_dir, 'pictures')
