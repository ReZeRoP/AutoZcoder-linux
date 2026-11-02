"""Environment overrides must obey the same finite stream limits as YAML."""
import pytest

from zcode_gateway.config import load_config


@pytest.mark.parametrize('field,value', [
    ('STREAM_MAX_CONCURRENT', '0'), ('STREAM_MAX_CONCURRENT', '-1'),
    ('STREAM_SEND_TIMEOUT_SECONDS', 'inf'), ('STREAM_SEND_TIMEOUT_SECONDS', '0'),
    ('STREAM_PROGRESS_TIMEOUT_SECONDS', 'nan'), ('STREAM_PROGRESS_TIMEOUT_SECONDS', '-1'),
    ('STREAM_VERIFY_TIMEOUT_SECONDS', 'inf'), ('STREAM_MAX_SENDS', '0'),
    ('STREAM_BUFFER_MAX_BYTES', '1000000000'), ('STREAM_DELIVERY', 'unknown'),
    ('STREAM_HEARTBEAT_SECONDS', 'not-a-number'),
])
def test_invalid_stream_environment_is_rejected(monkeypatch, tmp_path, field, value):
    monkeypatch.setenv('ZCODE_GATEWAY_UPSTREAM__' + field, value)
    path = tmp_path / 'config.yaml'
    path.write_text('upstream: {}\n')
    with pytest.raises(ValueError, match='invalid upstream'):
        load_config(path)


def test_valid_stream_environment_overrides_are_preserved(monkeypatch, tmp_path):
    monkeypatch.setenv('ZCODE_GATEWAY_UPSTREAM__STREAM_VERIFY_TIMEOUT_SECONDS', '1800')
    monkeypatch.setenv('ZCODE_GATEWAY_UPSTREAM__STREAM_MAX_CONCURRENT', '4')
    monkeypatch.setenv('ZCODE_GATEWAY_UPSTREAM__STREAM_DELIVERY', 'verified')
    path = tmp_path / 'config.yaml'
    path.write_text('upstream: {}\n')
    cfg = load_config(path)
    assert cfg.upstream.stream_verify_timeout_seconds == 1800
    assert cfg.upstream.stream_max_concurrent == 4
    assert cfg.upstream.stream_delivery == 'verified'


def test_decimal_environment_uses_schema_not_integer_default(monkeypatch, tmp_path):
    monkeypatch.setenv('ZCODE_GATEWAY_UPSTREAM__STREAM_CLEANUP_TIMEOUT_SECONDS', '0.2')
    path = tmp_path / 'config.yaml'
    path.write_text('upstream: {}\n')
    assert load_config(path).upstream.stream_cleanup_timeout_seconds == .2


def test_explicit_legacy_total_timeout_is_not_silently_extended(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('upstream:\n  stream_verify_timeout_seconds: 301\n')
    assert load_config(path).upstream.stream_verify_timeout_seconds == 301
