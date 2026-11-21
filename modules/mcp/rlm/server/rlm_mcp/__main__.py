import argparse
import json
import signal
import sys

from . import refine_run, session
from .config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rlm-mcp")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    hook = sub.add_parser("hook")
    hook.add_argument("event")
    run = sub.add_parser("refine-run")
    run.add_argument("--conversation", required=True)
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.cmd == "hook":
        from .hooks import handle

        try:
            payload = json.load(sys.stdin)
        except ValueError:
            payload = {}
        print(json.dumps(handle(args.event, payload, settings, session.find_agy_pid())))
        return 0

    if args.cmd == "refine-run":
        sess = next(
            (
                json.loads(p.read_text())
                for p in (settings.data_dir / "sessions").glob("*.json")
                if json.loads(p.read_text()).get("conversationId") == args.conversation
            ),
            {},
        )
        for request in refine_run.pop_queue(settings.data_dir, args.conversation):
            refine_run.run_refinement(
                settings,
                args.conversation,
                sess.get("transcriptPath"),
                request,
                sess.get("modelName"),
            )
        return 0

    from .server import build_app
    from .service import RlmService

    service = RlmService(settings, session.find_agy_pid())
    service.serve_control()
    signal.signal(signal.SIGTERM, lambda *_: (service.shutdown(), sys.exit(0)))
    try:
        build_app(service).run()
    finally:
        service.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
