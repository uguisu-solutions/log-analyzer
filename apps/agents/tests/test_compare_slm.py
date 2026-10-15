"""scripts/compare_slm.py（クラウドと SLM の一括比較実行）のテスト。

docs/plan/slm_support.md §9-6：同じ入力をモデル構成ごとに N 回流し、解析履歴に保存して
比較 API に並べる。スクリプトは起動中のバックエンドに、画面と同じリクエストを HTTP で送る。
テストではバックエンドとの送受信を偽物に差し替え、LLM も HTTP も使わない。
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import compare_slm as cs  # noqa: E402


def test_plan_runs_each_config_n_times_grouped_by_config():
    """Pod ではモデルを 1 つずつ動かすため、構成ごとにまとめて流す（モデルの入れ替えを最小にする）。"""
    assert cs.plan(["config4", "user:3"], runs=2) == [
        ("config4", 1), ("config4", 2), ("user:3", 1), ("user:3", 2),
    ]


def _scenario(tmp_path):
    (tmp_path / "fw-01.conf").write_text("access-list x", encoding="utf-8")
    (tmp_path / "fw-01.log").write_text("DENY dst=10.0.20.5\n", encoding="utf-8")
    (tmp_path / "api-01.log").write_text("502 bad gateway\n", encoding="utf-8")
    (tmp_path / "questionnaire.json").write_text('{"event": "API が 502"}', encoding="utf-8")
    return cs.load_scenario(tmp_path)


def test_run_request_matches_what_the_ui_sends(tmp_path):
    """構成・ログ解析の画面と同じ項目で送る（1 段階・設定とログの両方・方針確認なし・監査なし）。"""
    body = cs.build_run_request(_scenario(tmp_path), config="user:3", rally_max_rounds=3)

    assert body["config"] == "user:3"
    assert body["rally_max_rounds"] == 3
    assert body["topology"] == {
        "nodes": [
            {"id": "api-01", "type": "Server", "label": "", "ip": ""},
            {"id": "fw-01", "type": "FW", "label": "", "ip": ""},
        ],
        "links": [],
    }
    assert body["node_logs"]["fw-01"] == [{"name": "fw-01.log", "content": "DENY dst=10.0.20.5\n"}]
    assert body["node_configs"] == {"fw-01": [{"name": "fw-01.conf", "content": "access-list x"}]}
    assert body["questionnaire_answers"] == {"event": "API が 502"}
    assert (body["analysis_mode"], body["single_source"]) == ("single", "both")
    assert body["require_policy_approval"] is False
    assert body["audit_after_integrator"] is False


class _FakeBackend:
    """起動中のバックエンドの代わり。stream はイベント列を返し、post は呼び出しを記録する。"""

    def __init__(self, events):
        self.events = events
        self.streamed: list[tuple[str, dict]] = []
        self.posted: list[tuple[str, dict]] = []

    def stream(self, path, body):
        self.streamed.append((path, body))
        yield from self.events

    def post(self, path, body):
        self.posted.append((path, body))
        return {"id": 1, "created": True}


_RESULT = {"confidence": 0.7, "root_cause_candidates": [], "recommended_actions": []}


def test_run_one_stops_at_round_limit_and_saves_history_like_the_ui(tmp_path):
    scenario = _scenario(tmp_path)
    backend = _FakeBackend([
        ("run_id_assigned", {"run_id": "r1"}),
        ("monitor_start", {}),
        ("await_confirmation", {"round": 3, "rally_max_rounds": 3}),
        ("final", {"result": _RESULT}),
    ])

    run_id = cs.run_one(backend, scenario, config="user:3", rally_max_rounds=3)

    assert run_id == "r1"
    assert backend.streamed[0][0] == "/api/runs/config-log-stream"
    # ラウンド上限では「停止（その時点で統合）」を返す（CLI と同じ。クラウド・SLM で条件を揃える）
    assert ("/api/runs/r1/decision", {"action": "stop"}) in backend.posted
    path, saved = backend.posted[-1]
    assert path == "/api/analysis-history"
    assert saved["run_id"] == "r1"
    assert saved["config_id"] == "user:3"
    assert saved["result"] == _RESULT
    assert (saved["kind"], saved["analysis_mode"], saved["single_source"], saved["stage_order"]) == (
        "config-log", "single", "both", "config_log",
    )
    assert saved["rally_max_rounds"] == 3
    assert saved["questionnaire_answers"] == {"event": "API が 502"}
    assert saved["topology"] == backend.streamed[0][1]["topology"]
    # 画面と同じく、ログ → 設定の順にファイル名だけを残す（本文は保存しない）
    assert saved["input_files"] == ["api-01.log", "fw-01.log", "fw-01.conf"]


def test_run_one_raises_on_error_event_without_saving(tmp_path):
    import pytest

    backend = _FakeBackend([
        ("run_id_assigned", {"run_id": "r2"}),
        ("error", {"stage": "monitor", "message": "Ollama がエラーを返した"}),
    ])

    with pytest.raises(RuntimeError, match="Ollama"):
        cs.run_one(backend, _scenario(tmp_path), config="config4", rally_max_rounds=3)
    assert not [p for p, _ in backend.posted if p == "/api/analysis-history"]


# ─── 本物のバックエンドとの送受信（localhost の擬似サーバで確かめる）─────────


def test_http_backend_reads_sse_and_posts_json():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: list[tuple[str, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, body))
            if self.path.endswith("-stream"):
                payload = (
                    'event: run_id_assigned\ndata: {"run_id": "r9"}\n\n'
                    'event: final\ndata: {"result": {"confidence": 0.5}}\n\n'
                ).encode()
                ctype = "text/event-stream"
            else:
                payload = b'{"id": 7, "created": true}'
                ctype = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        backend = cs.HttpBackend(f"http://127.0.0.1:{server.server_port}")
        events = list(backend.stream("/api/runs/config-log-stream", {"config": "config4"}))
        saved = backend.post("/api/analysis-history", {"run_id": "r9"})
    finally:
        server.shutdown()

    assert events == [("run_id_assigned", {"run_id": "r9"}), ("final", {"result": {"confidence": 0.5}})]
    assert saved == {"id": 7, "created": True}
    assert received == [
        ("/api/runs/config-log-stream", {"config": "config4"}),
        ("/api/analysis-history", {"run_id": "r9"}),
    ]


# ─── コマンドの入口 ────────────────────────────────────────────────


def _no_backend(*args, **kwargs):
    raise AssertionError("dry-run でバックエンドにつないではいけない")


def test_dry_run_prints_plan_without_touching_backend(tmp_path, monkeypatch, capsys):
    _scenario(tmp_path)
    monkeypatch.setattr(cs, "HttpBackend", _no_backend)

    code = cs.main([
        "--scenario", str(tmp_path), "--configs", "config4,user:3", "--runs", "2", "--dry-run",
    ])

    out = capsys.readouterr().out
    assert code == 0
    assert out.count("config4") == 2 and out.count("user:3") == 2
    assert "4 回" in out


def test_main_continues_after_a_failed_run_and_reports_it(tmp_path, monkeypatch, capsys):
    _scenario(tmp_path)
    monkeypatch.setattr(cs, "HttpBackend", lambda base_url: object())
    calls: list[tuple[str, int]] = []

    def fake_run_one(backend, scenario, *, config, rally_max_rounds):
        calls.append(config)
        if len(calls) == 2:
            raise RuntimeError("Ollama がエラーを返した")
        return f"run-{len(calls)}"

    monkeypatch.setattr(cs, "run_one", fake_run_one)

    code = cs.main(["--scenario", str(tmp_path), "--configs", "config4,user:3", "--runs", "2"])

    captured = capsys.readouterr()
    assert calls == ["config4", "config4", "user:3", "user:3"]  # 失敗しても最後まで流す
    assert code == 1  # 失敗があれば終了コード 1
    assert "失敗 1 件" in captured.out
    assert "Ollama がエラーを返した" in captured.out
