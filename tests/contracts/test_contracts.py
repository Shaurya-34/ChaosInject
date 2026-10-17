import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from app.contracts import *
from app.adapters.fixtures import load
from app.api.evidence import checked_evidence, summary_hash
from app.api.errors import PortError

@pytest.mark.parametrize('name,cls', [('topology',TopologySnapshot),('state',SystemStateSnapshot),('missing-state',SystemStateSnapshot),('stale-state',SystemStateSnapshot),('target',VerifiedTarget),('proposal',ExperimentProposal),('summary',RunSummary),('failed-cleanup',RunSummary),('analysis',AnalysisResult),('model-outage',AnalysisResult),('rejected-proposal',ValidationResult)])
def test_fixture_contracts(name,cls):
    obj=load(name,cls)
    assert obj.schema_version=='1.0'

def test_reject_invalid_summary_and_nonfinite_values():
    with pytest.raises(ValidationError): load('invalid-summary',RunSummary)
    with pytest.raises(ValidationError): AbortLimits(error_rate=float('nan'),p95_ms=1000,metric_max_age_seconds=5,recovery_timeout_seconds=120,minimum_samples=30)
    p=load('proposal',ExperimentProposal).model_dump(mode='json');p['command']='arbitrary'
    with pytest.raises(ValidationError): ExperimentProposal.model_validate(p)

def test_missing_requires_reason():
    s=load('state',SystemStateSnapshot).model_dump();s['missing_features']=[]
    with pytest.raises(ValidationError): SystemStateSnapshot.model_validate(s)

def test_evidence_rejects_changed_numbers_or_invalid_pointer():
    s,a=load('summary',RunSummary),load('analysis',AnalysisResult)
    checked_evidence(s,a)
    s.phase_summaries[1].operations['cart'].p95_ms=999
    with pytest.raises(PortError): checked_evidence(s,a)
    s=load('summary',RunSummary)
    a.evidence_refs[0].pointer='/absent'
    with pytest.raises(PortError): checked_evidence(s,a)

def test_fixture_scores_do_not_claim_measured_total():
    assert load('summary',RunSummary).score.total is None
    assert load('analysis',AnalysisResult).measured_facts == []
