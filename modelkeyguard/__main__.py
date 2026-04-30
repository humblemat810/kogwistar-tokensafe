from __future__ import annotations

import argparse
from .gateway import serve
from .review_worker import main as review_main
from .cli import main as cli_main
from .graph_tools import init_graph, inspect_graph
from .registration import main as registration_main
from .tools import get_agent_token_main, usage_analysis_agent_main
from .remote_deploy import main as deploy_remote_main


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="modelkeyguard")
    sub = p.add_subparsers(dest="cmd")
    g = sub.add_parser("gateway")
    g.add_argument("--host", default="127.0.0.1")
    g.add_argument("--port", type=int, default=8789)
    g.add_argument("--policy", default="config/gateway_policy.json")
    sub.add_parser("review-once")
    sub.add_parser("scenario")
    sub.add_parser("init-graph")
    sub.add_parser("inspect-graph")
    sub.add_parser("registration")
    sub.add_parser("mint-token")
    sub.add_parser("usage-analysis-agent")
    sub.add_parser("deploy-remote")
    args, rest = p.parse_known_args(argv)
    if args.cmd == "gateway":
        serve(args.host, args.port, args.policy)
        return 0
    if args.cmd == "review-once":
        return review_main(rest)
    if args.cmd == "scenario":
        return cli_main(["scenario"] + rest)
    if args.cmd == "init-graph":
        return init_graph()
    if args.cmd == "inspect-graph":
        return inspect_graph()
    if args.cmd == "registration":
        return registration_main(rest)
    if args.cmd == "mint-token":
        return get_agent_token_main(rest)
    if args.cmd == "usage-analysis-agent":
        return usage_analysis_agent_main(rest)
    if args.cmd == "deploy-remote":
        return deploy_remote_main(rest)
    p.print_help()
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
