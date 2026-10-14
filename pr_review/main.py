import sys

from .app import Application


def main(argv: list[str] | None = None) -> int:
    app = Application()
    return app.run(sys.argv if argv is None else argv)
