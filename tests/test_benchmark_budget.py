"""The opt-in paid probe must reserve before every attempt and fail closed."""
import pytest
from scripts.quality_benchmark import Budget, BudgetStop


def payload(**kwargs):
    return {"model": "gpt-5.6-terra", "max_output_tokens": 2048, **kwargs}


def test_result_replacement_recovers_from_a_brief_windows_reader(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from scripts.quality_benchmark import write_evaluation_state
    target = tmp_path / "results.json"
    target.write_text('{"stage":"previous"}')
    original = Path.replace
    attempts = []
    def replace(path, destination):
        attempts.append(path)
        if len(attempts) == 1:
            assert json.loads(target.read_text())["stage"] == "previous"
            raise PermissionError("Windows sharing violation")
        return original(path, destination)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr("scripts.quality_benchmark.time.sleep", lambda delay: None)
    write_evaluation_state(tmp_path, {"stage": "current", "records": [1]})
    assert len(attempts) == 2
    assert json.loads(target.read_text()) == {"stage": "current", "records": [1]}


def test_persistent_result_write_failure_is_bounded_and_not_hidden(tmp_path, monkeypatch):
    from pathlib import Path
    from scripts.quality_benchmark import write_evaluation_state
    target = tmp_path / "results.json"
    target.write_text('{"stage":"previous"}')
    attempts = []
    def replace(*args):
        attempts.append(1)
        raise PermissionError("Persistent denial")
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr("scripts.quality_benchmark.time.sleep", lambda delay: None)
    with pytest.raises(PermissionError):
        write_evaluation_state(tmp_path, {"stage": "current"})
    assert len(attempts) == 5 and target.read_text() == '{"stage":"previous"}'
    assert (tmp_path / "results.tmp").exists()


def test_benchmark_reuses_saved_key_after_the_original_lease_is_closed():
    from types import SimpleNamespace
    from pixtagra_credential_policy import CredentialUseLease
    from scripts.quality_benchmark import BenchmarkCredentials

    class Vault:
        value = None

        def read(self, provider):
            assert provider == "openai"
            return SimpleNamespace(status="found" if self.value else "not_found", secret=self.value)

        def write(self, provider, secret, allow_replace=False):
            assert provider == "openai" and allow_replace
            self.value = secret
            return SimpleNamespace(status="stored", verified=True)

    vault = Vault()
    first_run = BenchmarkCredentials(vault)
    assert first_run.load() is None
    entered = CredentialUseLease("openai", "SYNTHETIC-test-only")
    assert first_run.remember(entered)
    entered.close()
    restarted = BenchmarkCredentials(vault).load()
    assert restarted.secret_for_authorized_use() == "SYNTHETIC-test-only"
    restarted.close()


def test_benchmark_does_not_report_a_failed_vault_write_as_saved():
    from types import SimpleNamespace
    from pixtagra_credential_policy import CredentialUseLease
    from scripts.quality_benchmark import BenchmarkCredentials

    vault = SimpleNamespace(write=lambda *a, **kw: SimpleNamespace(status="write_failed", verified=False))
    entered = CredentialUseLease("openai", "SYNTHETIC-test-only")
    assert not BenchmarkCredentials(vault).remember(entered)
    assert entered.secret_for_authorized_use() == "SYNTHETIC-test-only"
    entered.close()


def test_budget_refuses_dispatch_before_exceeding_ten_yuan_including_failures():
    budget = Budget()
    accepted = 0
    while True:
        try:
            budget.reserve("https://dragtokens.com/v1/responses", payload())
            accepted += 1
        except BudgetStop:
            break
    assert accepted > 0 and budget.charged_bound <= 10
    assert budget.charged_bound + Budget.RESERVE > 10
    before = budget.charged_bound
    budget.settle(budget.entries[0], None)
    budget.settle(budget.entries[0], {"input_tokens": -1, "output_tokens": 0})
    assert budget.charged_bound == before


def test_usage_reconciliation_and_duplicate_settlement_do_not_free_budget_twice():
    budget = Budget()
    entry = budget.reserve("https://dragtokens.com/v1/responses", payload())
    budget.settle(entry, {"input_tokens": 1000, "output_tokens": 500})
    assert budget.charged_bound == pytest.approx(.068)
    budget.settle(entry, {"input_tokens": 1000, "output_tokens": 500})
    assert budget.charged_bound == pytest.approx(.068)


@pytest.mark.parametrize("url,data", [
    ("https://dragtokens.com.evil.example/v1/responses", payload()),
    ("http://dragtokens.com/v1/responses", payload()),
    ("https://dragtokens.com/v1/responses", payload(model="other")),
    ("https://dragtokens.com/v1/responses", payload(max_output_tokens=5000)),
    ("https://dragtokens.com/v1/responses", payload(service_tier="priority")),
])
def test_endpoint_model_and_request_cap_are_checked_before_reserving(url, data):
    budget = Budget()
    with pytest.raises(BudgetStop):
        budget.reserve(url, data)
    assert budget.charged_bound == 0 and not budget.entries


def test_budget_cannot_be_silently_raised():
    with pytest.raises(ValueError):
        Budget(20)


def test_explicit_unlimited_mode_records_usage_without_the_old_money_ceiling():
    ledger = Budget(None)
    for _ in range(40):
        entry = ledger.reserve('https://dragtokens.com/v1/responses', payload())
        ledger.settle(entry, {'input_tokens': 10000, 'output_tokens': 2000})
    assert len(ledger.entries) == 40 and ledger.charged_bound > 10
    assert sum(e['input_tokens'] for e in ledger.entries) == 400000
    with pytest.raises(BudgetStop):
        ledger.reserve('https://other.example/v1/responses', payload())
    assert len(ledger.entries) == 40
    snapshot = {'limit_yuan':None, 'requests':ledger.entries,
                'conservative_bound_yuan':ledger.charged_bound}
    with pytest.raises(BudgetStop, match='explicit'):
        Budget.restore(snapshot)
    restored = Budget.restore(snapshot, unlimited=True)
    restored.reserve('https://dragtokens.com/v1/responses', payload())
    assert restored.limit is None and len(restored.entries) == 41


def test_account_usage_unauthorized_is_recorded_without_rejecting_model_token(monkeypatch):
    from types import SimpleNamespace
    import requests
    from scripts.quality_benchmark import fetch_billing_json
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=401)
    monkeypatch.setattr(requests, 'get', get)
    assert fetch_billing_json('/api/usage/token/', 'SYNTHETIC') == {'data': {'usage_lookup_unavailable': True}}
    assert calls[0][0].endswith('/api/usage/token/')
    assert calls[0][1]['allow_redirects'] is False
    with pytest.raises(BudgetStop):
        fetch_billing_json('/api/pricing')


def test_billing_redirect_never_forwards_a_token_to_a_new_host(monkeypatch):
    from types import SimpleNamespace
    import requests
    from scripts.quality_benchmark import fetch_billing_json
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: SimpleNamespace(status_code=301))
    with pytest.raises(BudgetStop):
        fetch_billing_json('/api/usage/token/', 'SYNTHETIC')


def test_restart_keeps_unknown_requests_reserved_and_never_resets_approval():
    budget = Budget()
    for _ in range(7):
        budget.reserve('https://dragtokens.com/v1/responses', payload())
    snapshot = {'limit_yuan': 10, 'requests': budget.entries, 'conservative_bound_yuan': budget.charged_bound}
    restarted = Budget.restore(snapshot)
    assert restarted.charged_bound == budget.charged_bound
    with pytest.raises(BudgetStop):
        restarted.reserve('https://dragtokens.com/v1/responses', payload())
