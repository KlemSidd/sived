import sys

from app.bootstrap import main as bootstrap
from app.web import serve
from app.worker import main as work


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "web"
    if cmd == "web":
        serve()
    elif cmd == "work":
        work()
    elif cmd == "bootstrap":
        bootstrap()
    else:
        raise SystemExit("use web, work, or bootstrap")


if __name__ == "__main__":
    main()
