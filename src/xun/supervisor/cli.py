from __future__ import annotations

import argparse

from aiohttp import web

from .runtime import DockerManager, instance_id
from .service import Multiplexer, Supervisor
from .users import UserStore


def _parse_port_range(value: str) -> range:
    try:
        start_text, end_text = value.split("-", 1)
        start, end = int(start_text), int(end_text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port range must be START-END") from error
    if not (1 <= start <= end <= 65535):
        raise argparse.ArgumentTypeError("ports must satisfy 1 <= START <= END <= 65535")
    return range(start, end + 1)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage multiplexed xun containers.")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("user-add", help="Add a user with a random token.")
    add.add_argument("username")

    delete = commands.add_parser("user-del", help="Delete a user.")
    delete.add_argument("username")

    commands.add_parser("user-list", help="List users.")

    serve = commands.add_parser("serve", help="Run the multiplexing server.")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=18960)
    serve.add_argument("--port-range", type=_parse_port_range, default=range(17960, 18959), metavar="START-END")
    serve.add_argument("--image", default="xun")
    serve.add_argument("--interval", type=float, default=2.0, help=argparse.SUPPRESS)
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    store = UserStore()

    try:
        if args.command == "serve":
            if args.interval <= 0:
                parser.error("--interval must be greater than zero")
            containers = DockerManager(
                image=args.image,
                port_range=args.port_range,
                instance=instance_id(store.path),
                excluded_ports={args.port},
            )
            supervisor = Supervisor(store, containers, args.interval)
            web.run_app(Multiplexer(supervisor).app(), host=args.host, port=args.port)
        elif args.command == "user-add":
            user = store.add(args.username)
            print(f"{user.name}\t{user.token}\t{user.base_path}")
        elif args.command == "user-del":
            if not store.delete(args.username):
                parser.error(f"user does not exist: {args.username}")
        elif args.command == "user-list":
            print("USER\tTOKEN\tBASE PATH")
            for user in store.list():
                print(f"{user.name}\t{user.token}\t{user.base_path}")
    except ValueError as error:
        parser.error(str(error))