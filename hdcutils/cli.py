# -*- coding: utf-8 -*-
"""``python -m hdcutils`` command line tool.

Usage examples::

    python -m hdcutils list [-v]
    python -m hdcutils -t <serial> shell param get const.product.model
    python -m hdcutils push local.hap /data/local/tmp/app.hap
    python -m hdcutils pull /data/local/tmp/x.jpeg x.jpeg
    python -m hdcutils screenshot screen.jpeg
    python -m hdcutils fport ls
    python -m hdcutils checkserver
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .core import HdcClient
from .exceptions import HdcError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hdcutils",
        description="Pure-python HarmonyOS hdc library (adbutils-style).",
    )
    parser.add_argument("-t", "--target", help="device connect key (serial)")
    parser.add_argument("--port", type=int, help="hdc server port (default 8710/env)")
    parser.add_argument("--host", default="127.0.0.1", help="hdc server host")
    parser.add_argument("--hdc-path", help="path to hdc binary (only for server auto-start)")
    parser.add_argument("--no-auto-start", action="store_true",
                        help="do not auto-start the hdc server")

    sub = parser.add_subparsers(dest="cmd")
    list_p = sub.add_parser("list", help="list targets")
    list_p.add_argument("-v", "--verbose", action="store_true")
    shell = sub.add_parser("shell", help="run a shell command on the device")
    shell.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("apps", help="list installed apps")
    push = sub.add_parser("push", help="push a local file to the device")
    push.add_argument("local")
    push.add_argument("remote")
    pull = sub.add_parser("pull", help="pull a device file to local")
    pull.add_argument("remote")
    pull.add_argument("local")
    install = sub.add_parser("install", help="install a hap/app package")
    install.add_argument("path")
    install.add_argument("extra", nargs="*")
    uninstall = sub.add_parser("uninstall", help="uninstall an app")
    uninstall.add_argument("package")
    shot = sub.add_parser("screenshot", help="take a screenshot")
    shot.add_argument("path", nargs="?", help="output path (jpeg)")
    fport = sub.add_parser("fport",
                           help="port forwarding: <local> <remote> | ls | rm <rule>")
    fport.add_argument("rules", nargs="*")
    tconn = sub.add_parser("tconn", help="connect a network device: tconn <ip:port> [-remove]")
    tconn.add_argument("addr")
    tconn.add_argument("extra", nargs="*")
    sub.add_parser("checkserver", help="show the hdc server version")
    sub.add_parser("start", help="start the hdc server")
    sub.add_parser("kill", help="kill the hdc server")
    sub.add_parser("version", help="show the hdcutils version")
    prop = sub.add_parser("prop", help="get a device property")
    prop.add_argument("name")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "version":
        from . import __version__

        print(__version__)
        return 0
    client = HdcClient(
        host=args.host,
        port=args.port,
        hdc_path=args.hdc_path,
        auto_start=not args.no_auto_start,
    )
    try:
        if args.cmd == "list":
            for t in client.list_targets(verbose=args.verbose):
                print(t)
        elif args.cmd == "shell":
            device = client.device(args.target)
            command = " ".join(args.command)
            if command.startswith("shell "):
                command = command[len("shell "):]
            print(device.shell(command))
        elif args.cmd == "apps":
            for app in client.device(args.target).list_apps():
                print(app)
        elif args.cmd == "push":
            client.device(args.target).send_file(args.local, args.remote)
        elif args.cmd == "pull":
            client.device(args.target).recv_file(args.remote, args.local)
        elif args.cmd == "install":
            client.device(args.target).install(args.path, *args.extra)
        elif args.cmd == "uninstall":
            client.device(args.target).uninstall(args.package)
        elif args.cmd == "screenshot":
            path = args.path or "screenshot.jpeg"
            client.device(args.target).screenshot(path)
            print("saved to %s" % path)
        elif args.cmd == "fport":
            device = client.device(args.target)
            rules = args.rules
            if not rules or rules == ["ls"]:
                for rule in device.fport_list():
                    print(rule)
            elif rules[0] == "rm" and len(rules) > 1:
                device.fport_remove(rules[1])
            elif len(rules) == 2:
                device.fport(rules[0], rules[1])
            else:
                parser.error("fport requires: <local> <remote> | ls | rm <rule>")
        elif args.cmd == "tconn":
            if args.extra and args.extra[0] == "-remove":
                print(client.disconnect(args.addr))
            else:
                print(client.connect(args.addr))
        elif args.cmd == "checkserver":
            print(client.server_version())
        elif args.cmd == "start":
            client.start_server()
            print("hdc server started on %s:%s" % (client.host, client.port))
        elif args.cmd == "kill":
            client.kill_server()
            print("hdc server killed")
        elif args.cmd == "prop":
            print(client.device(args.target).get_prop(args.name))
        else:
            parser.print_help()
            return 1
        return 0
    except HdcError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
