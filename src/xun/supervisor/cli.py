from __future__ import annotations

import argparse

from aiohttp import web
from rich.console import Console
from rich.table import Table

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


def _user_table(users: list) -> Table:
    table = Table(title="Users")
    table.add_column("USER", style="cyan", no_wrap=True)
    table.add_column("TOKEN", style="magenta")
    table.add_column("BASE PATH", style="green")
    for user in users:
        table.add_row(user.name, user.token, str(user.base_path))
    return table


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
    serve.add_argument("--env", type=str, help="Environment variables to pass into the container, can be a comma-separated wildcard list. Will always include XUN_*/_XUN_* by default.", default=[], nargs="+")
    serve.add_argument("--interval", type=float, default=2.0, help=argparse.SUPPRESS)
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    store = UserStore()
    console = Console()

    try:
        if args.command == "serve":
            if args.interval <= 0:
                parser.error("--interval must be greater than zero")
            env_patterns = ["XUN_*", "_XUN_*"] + [e.strip() for ev in args.env for e in ev.split(",") if e.strip()]
            containers = DockerManager(
                image=args.image,
                port_range=args.port_range,
                instance=instance_id(store.path),
                excluded_ports={args.port},
                env_patterns=env_patterns,
            )
            supervisor = Supervisor(store, containers, args.interval)
            web.run_app(Multiplexer(supervisor).app(), host=args.host, port=args.port)
        elif args.command == "user-add":
            user = store.add(args.username)
            console.print(f"[green]Added user[/green] [cyan]{user.name}[/cyan]")
            console.print(_user_table([user]))
        elif args.command == "user-del":
            if not store.delete(args.username):
                parser.error(f"user does not exist: {args.username}")
            else:
                console.print(f"[green]Deleted user[/green] [cyan]{args.username}[/cyan]")
        elif args.command == "user-list":
            users = store.list()
            if not users:
                console.print("[dim]No users.[/dim]")
            else:
                console.print(_user_table(users))
    except ValueError as error:
        parser.error(str(error))