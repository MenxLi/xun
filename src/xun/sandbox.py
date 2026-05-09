
import argparse
import os
import subprocess
import sys

from .config import BRAND

CONTAINER_CWD = "/workspace"


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the agent in a temporary Docker container.")
    parser.add_argument(
        "--image",
        type=str,
        help="The docker image to use.",
        default=os.getenv(f"{BRAND}_DOCKER_IMAGE"),
    )
    return parser


def _build_docker_command(
    image: str,
    forwarded_args: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdin_tty: bool | None = None,
    stdout_tty: bool | None = None,
) -> list[str]:
    current_dir = os.path.realpath(cwd or os.getcwd())
    current_env = env or dict(os.environ)
    command = ["docker", "run", "--rm"]

    input_is_tty = sys.stdin.isatty() if stdin_tty is None else stdin_tty
    output_is_tty = sys.stdout.isatty() if stdout_tty is None else stdout_tty
    if input_is_tty and output_is_tty:
        command.append("-it")
    else:
        command.append("-i")

    command.extend([
        "--network",
        "host",
        "-v",
        f"{current_dir}:{CONTAINER_CWD}",
        "-w",
        CONTAINER_CWD,
    ])

    for name in sorted(key for key in current_env if key.startswith(f"{BRAND}_")):
        command.extend(["-e", name])

    command.extend([image, "xun", *forwarded_args])
    return command


def main(argv: list[str] | None = None) -> int:
    parser = get_parser()
    args, forwarded_args = parser.parse_known_args(argv)
    if not args.image:
        parser.error(f"Docker image is required. Pass --image or set {BRAND}_DOCKER_IMAGE.")

    command = _build_docker_command(args.image, forwarded_args)
    try:
        completed = subprocess.run(command, check=False)
    except FileNotFoundError:
        parser.exit(status=127, message="docker is not installed or not available in PATH.\n")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())