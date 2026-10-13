import json

from clients.importer_cli.__main__ import main


def test_json_failure_includes_key_for_reconciliation(tmp_path, capsys):
    code = main(['--url', 'https://importer.example', '--token', 'pf_example', '--json', 'submit', str(tmp_path / 'missing.zip'), '--repository', 'npm-hosted', '--idempotency-key', 'retry-key'])
    assert code == 4
    report = json.loads(capsys.readouterr().out)
    assert report['error']['code'] == 'API_OR_INPUT_ERROR'
    assert report['idempotency_key'] == 'retry-key'


def test_invalid_wait_does_not_submit(tmp_path, capsys, monkeypatch):
    requests = []
    monkeypatch.setattr('clients.importer_cli.__main__._request', lambda *a, **kw: requests.append(kw))
    code = main(['--url', 'https://importer.example', '--token', 'pf_example', '--json', 'submit', str(tmp_path / 'any.zip'), '--repository', 'npm-hosted', '--wait'])
    assert code == 4
    assert not requests
    assert json.loads(capsys.readouterr().out)['error']
