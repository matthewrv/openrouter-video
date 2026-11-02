import argparse
from importlib.metadata import PackageNotFoundError, version


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="openrouter-video",
        description="GUI для генерации видео через OpenRouter.",
    )
    try:
        package_version = version("openrouter-video")
    except PackageNotFoundError:
        package_version = "development"
    parser.add_argument("--version", action="version", version="%(prog)s " + package_version)
    parser.parse_args(argv)

    from .gui import run

    return run()
