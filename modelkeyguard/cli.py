from __future__ import annotations

import argparse
from html import escape
from pathlib import Path

from .core import ModelKey, ModelKeyGuard, Principal, Request


def build_sample_guard() -> ModelKeyGuard:
    guard = ModelKeyGuard.create()
    guard.register_key(
        ModelKey(
            id="key:azure:gpt-5.3-prod",
            provider="azure-openai",
            models=("gpt-5.3", "gpt-5.3-mini"),
            secret_ref="vault://azure-openai/prod/gpt-5.3",
            display_name="Azure GPT-5.3 Production",
            monthly_budget_usd=25.0,
            approval_threshold_usd=2.0,
        )
    )
    guard.register_key(
        ModelKey(
            id="key:local:llama-dev",
            provider="local",
            models=("llama-dev",),
            secret_ref="env://LOCAL_LLM_ENDPOINT",
            display_name="Local developer model",
            monthly_budget_usd=9999.0,
            approval_threshold_usd=9999.0,
        )
    )

    # Kogwistar ACL modes encode different governance shapes.
    guard.grant(
        key_id="key:azure:gpt-5.3-prod",
        mode="scope",
        created_by="human:platform-admin",
        owner_id="human:platform-admin",
        namespace="tenant:kogwistar",
    )
    guard.grant(
        key_id="key:local:llama-dev",
        mode="group",
        created_by="human:platform-admin",
        owner_id="human:platform-admin",
        shared_with_groups=("dev", "agent-dev"),
    )
    return guard


def run_scenario(out_dir: Path) -> int:
    guard = build_sample_guard()
    requests = [
        Request(
            principal=Principal("agent:doc-ingestor", "agent", ("agent-dev",)),
            key_id="key:azure:gpt-5.3-prod",
            model="gpt-5.3-mini",
            namespace="tenant:kogwistar",
            estimated_cost_usd=0.18,
            reason="index new docs",
        ),
        Request(
            principal=Principal("agent:external-scraper", "agent", ()),
            key_id="key:azure:gpt-5.3-prod",
            model="gpt-5.3-mini",
            namespace="tenant:other",
            estimated_cost_usd=0.18,
            reason="wrong tenant should be blocked",
        ),
        Request(
            principal=Principal("human:alice", "human", ("finance",)),
            key_id="key:azure:gpt-5.3-prod",
            model="gpt-5.3",
            namespace="tenant:kogwistar",
            estimated_cost_usd=3.20,
            reason="expensive deep analysis needs approval",
        ),
        Request(
            principal=Principal("agent:unit-test", "agent", ("agent-dev",)),
            key_id="key:local:llama-dev",
            model="llama-dev",
            namespace="tenant:any",
            estimated_cost_usd=0.0,
            reason="local model for tests",
        ),
    ]

    print("Kogwistar ModelKeyGuard")
    print(f"ACL backend: {guard.adapter_info.backend} ({guard.adapter_info.detail})")
    print("-" * 88)
    for req in requests:
        decision = guard.check(req)
        guard.record_usage(decision, estimated_cost_usd=req.estimated_cost_usd, actual_cost_usd=req.estimated_cost_usd if decision.allowed else None)
        status = "ALLOW" if decision.allowed else "APPROVAL" if decision.requires_approval else "BLOCK"
        print(
            f"{status:8} principal={req.principal.id:24} key={req.key_id:26} "
            f"ns={req.namespace:17} acl={decision.acl_reason:14} reason={decision.reason}"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / "modelkeyguard_report.html"
    report.write_text(render_report(guard), encoding="utf-8")
    print("-" * 88)
    print(f"HTML report: {report}")
    return 0


def render_report(guard: ModelKeyGuard) -> str:
    rows = []
    for event in guard.audit:
        klass = "allow" if event.allowed else "approval" if event.requires_approval else "block"
        rows.append(
            "<tr class='{klass}'>"
            "<td>{type}</td><td>{principal}</td><td>{key}</td><td>{ns}</td>"
            "<td>{decision}</td><td>{acl}</td><td>{cost}</td>"
            "</tr>".format(
                klass=klass,
                type=escape(event.type),
                principal=escape(event.principal_id),
                key=escape(event.key_id),
                ns=escape(event.namespace),
                decision=escape(event.reason),
                acl=escape(event.acl_reason),
                cost=escape(str(event.estimated_cost_usd)),
            )
        )
    key_cards = []
    for key in guard.keys.values():
        key_cards.append(
            f"<li><b>{escape(key.display_name)}</b> — {escape(key.provider)} — "
            f"used ${key.used_usd:.2f} / ${key.monthly_budget_usd:.2f} — secret hidden as <code>{escape(key.secret_ref)}</code></li>"
        )
    return """<!doctype html>
<html><head><meta charset='utf-8'><title>Kogwistar ModelKeyGuard Report</title>
<style>
body{{font-family:system-ui,Segoe UI,sans-serif;margin:2rem;line-height:1.4}} table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #ddd;padding:.55rem}} th{{background:#f7f7f7;text-align:left}}.allow{{background:#eefaf0}}.block{{background:#fff0f0}}.approval{{background:#fff8e6}} code{{background:#f5f5f5;padding:.1rem .25rem;border-radius:.25rem}}
</style></head><body>
<h1>Kogwistar ModelKeyGuard</h1>
<p><b>ACL backend:</b> {backend} — {detail}</p>
<p>Model keys are governed graph entities. Humans and agents are principals. Namespace is evaluated as Kogwistar <code>security_scope</code>.</p>
<h2>Keys</h2><ul>{keys}</ul>
<h2>Audit trail</h2>
<table><thead><tr><th>Type</th><th>Principal</th><th>Key</th><th>Namespace</th><th>Decision</th><th>ACL reason</th><th>Estimated cost</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>""".format(
        backend=escape(guard.adapter_info.backend),
        detail=escape(guard.adapter_info.detail),
        keys="".join(key_cards),
        rows="".join(rows),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kogwistar ACL-backed model-key access control")
    sub = parser.add_subparsers(dest="cmd")
    scenario = sub.add_parser("scenario", help="run the 3-minute scenario")
    scenario.add_argument("--out", default="out", help="output directory")
    args = parser.parse_args(argv)
    if args.cmd in (None, "scenario"):
        return run_scenario(Path(getattr(args, "out", "out")))
    parser.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
