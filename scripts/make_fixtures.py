"""Explicit synthetic fixtures, generated from authoritative models."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from app.contracts import *
from app.api.evidence import digest, summary_hash

root = Path(__file__).resolve().parents[1]
out = root / "fixtures/v1"
t = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
def write(name, model): (out / (name + ".json")).write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
nodes = [TopologyNode(target_id=n, name=n, kind="Deployment", uid="fixture-"+n, selector={"app": n}, ports=[p], desired_replicas=1, stateful=n == "redis-cart", confidence=0.7) for n,p in [("frontend",8080),("checkoutservice",5050),("cartservice",7070),("redis-cart",6379)]]
topology = TopologySnapshot(source="fixture", graph_id="fixture-graph", upstream_sha=None, generated_at=t, sandbox_id="fixture-sandbox", namespace="boutique", nodes=nodes, edges=[TopologyEdge(caller=c, dependency=d, protocol="TCP", provenance="synthetic example; incomplete topology", confidence=0.7) for c,d in [("frontend","checkoutservice"),("frontend","cartservice"),("checkoutservice","cartservice"),("cartservice","redis-cart")]], manual_corrections=["Illustrative subset; not the complete storefront dependency closure"])
write("topology",topology)
write("target", VerifiedTarget(target_id="cartservice", sandbox_id="fixture-sandbox", namespace="boutique", kind="Deployment", name="cartservice", uid="fixture-cartservice", selector={"app":"cartservice"}, allowed_fault_ids=["EXP-00"], allowed_template_ids=[], dependency_metadata={}, route_metadata={}, provenance="synthetic; not enrolled"))
state = SystemStateSnapshot(source="fixture",state_id="fixture-state", captured_at=t, valid_until=t+timedelta(seconds=30), sandbox_id="fixture-sandbox",ready_by_workload={n.name:Readiness(ready=1,desired=1) for n in nodes},request_rate=8,error_rate=0.01,p95_ms=142,db_connections_used=None,db_connections_limit=None,missing_features=["db_connections_used: SQL lab disconnected","db_connections_limit: SQL lab disconnected","cpu_utilization: collector unavailable","memory_utilization: collector unavailable"],active_fault_id=None)
write("state",state)
write("missing-state",state.model_copy(update={"p95_ms":None,"error_rate":None,"missing_features":state.missing_features+["p95_ms: probe unavailable","error_rate: probe unavailable"]}))
write("stale-state",state)
proposal = ExperimentProposal(source="fixture",proposal_id="fixture-proposal",catalog_id="EXP-00",sandbox_id="fixture-sandbox",target_id="cartservice",fault_parameters=FaultParameters(duration_seconds=30),hypothesis="Cart availability falls during a service pause.",reason="Illustrate downstream impact on cart and checkout; synthetic proposal.",expected_blast_radius=["cartservice","frontend","checkoutservice"],load_profile_id="fixture-load",abort_limits=AbortLimits(error_rate=0.15,p95_ms=1000,metric_max_age_seconds=5,recovery_timeout_seconds=120,minimum_samples=30),policy_version="fixture-v1",expires_at=t+timedelta(seconds=60))
write("proposal",proposal)
profile = LoadProfile(load_profile_id="fixture-load",operation_mix={"cart":1},concurrency=2,requested_rate=8,timeout_seconds=2,warmup_seconds=10,measurement_seconds=30,generator_version="fixture-no-generator")
phases=[]
for i,(name,success,p95) in enumerate([("baseline",120,142),("fault",90,580),("recovery",120,155)]):
    metrics=OperationAggregate(attempted=120,succeeded=success,failed=120-success,error_categories={} if success==120 else {"unavailable":30},success_rate=success/120,p50_ms=p95/2,p95_ms=p95,percentile_method="synthetic nearest rank",sample_count=120,missing=[])
    phases.append(PhaseSummary(phase=name,started_at=t+timedelta(seconds=i*30),ended_at=t+timedelta(seconds=(i+1)*30),operations={"cart":metrics},offered_rate=8,achieved_rate=4,ready_min=0 if name=="fault" else 1,desired_max=1,quality_notes=["Synthetic example, not measured"],percentile_method="synthetic nearest rank"))
score=ScoreComponents(availability=None,latency=None,recovery=None,safety=None,total=None,configuration="Awaiting Navya's deterministic scoring module: 0.40A + 0.20L + 0.25R + 0.15S",missing=["Live measurement and scoring adapter not integrated"])
s = RunSummary(source="fixture",run_id="fixture-run",catalog_id="EXP-00",sandbox_id="fixture-sandbox",topology_hash=digest(topology),application_revision=None,load_profile=profile,phase_summaries=phases,timeline_markers=[TimelineMarker(timestamp=t+timedelta(seconds=60),state="CLEANING_UP",reason="Synthetic threshold stop")],stop_reason="synthetic threshold breach",cleanup_status="verified",recovery_status="verified",recovery_seconds=12,data_quality=["All values are synthetic fixtures","No live cluster, application revision or fault verified"],score=score,evidence_hash="")
s=s.model_copy(update={"evidence_hash":summary_hash(s)});write("summary",s)
failed=s.model_copy(update={"cleanup_status":"failed","recovery_status":"unknown","recovery_seconds":None,"evidence_hash":""});failed=failed.model_copy(update={"evidence_hash":summary_hash(failed)});write("failed-cleanup",failed)
ref=EvidenceRef(run_id=s.run_id,evidence_hash=s.evidence_hash,pointer="/phase_summaries/1/operations/cart/failed")
finding=Finding(title="Cart path depends on one service",explanation="The fixture contains 30 failed cart attempts out of 120 during the fault window. Verify this hypothesis on the actual deployment.",status="insufficient_evidence",priority=1,evidence_refs=[ref])
fix=Finding(title="Test bounded timeouts and graceful degradation",explanation="Review cart and checkout timeout handling, then repeat the same workload to measure any benefit. No numeric benefit is claimed.",status="predicted",priority=1,evidence_refs=[ref])
a=AnalysisResult(source="fixture",run_id=s.run_id,mode="fixture",measured_facts=[],hypotheses=[finding],alternatives=["Workload or transport failure could also affect cart behavior"],recommendations=[fix],evidence_refs=[ref],limitations=["Fixture-only illustration; no live measured facts","Priority rubric: safety first, affected business operations second, evidence strength third"]);write("analysis",a)
write("model-outage",a.model_copy(update={"mode":"deterministic_fallback","limitations":a.limitations+["Reasoning model unavailable"]}))
write("learning",LearningStatus(source="fixture",learner_version="fixture-not-learning",started_at=t,processed=0,updates=0,pending_count=0,dropped_events=0,drift_status="cold_start",prediction_error=None,queue_limit=256,pending_limit=128,metric_buffer_seconds=120,intent_ttl_seconds=60,model_limit_bytes=65536))
write("blocked-query",QueryDecision(source="fixture",decision_id="fixture-block",request_id="fixture-query",decision="BLOCK",rule_ids=["MODULE_NOT_INTEGRATED"],policy_version="fixture",state_id=state.state_id,plan_estimate=None,prediction=None,reasons=["SQL policy not connected"],recommendations=["Connect owner module"]))
write("rejected-proposal",ValidationResult(proposal_id=proposal.proposal_id,proposal_hash=digest(proposal),valid=False,checks=[ValidationCheck(name="expiry",passed=False,reason="Proposal expired")],rejection_codes=["EXPIRED"],sandbox_id=proposal.sandbox_id,target_id=proposal.target_id,policy_version=proposal.policy_version,expires_at=proposal.expires_at))
invalid=s.model_dump(mode="json");invalid["cleanup_status"]="done";(out/"invalid-summary.json").write_text(json.dumps(invalid,indent=2)+"\n",encoding="utf-8")
