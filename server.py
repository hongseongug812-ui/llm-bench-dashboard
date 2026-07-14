"""
브라우저에서 "테스트 시작" 버튼으로 벤치마크를 직접 실행하는 로컬 서버.

benchmark_app.py를 매번 터미널에서 실행하는 대신, 이 서버를 한 번 띄워두면
대시보드의 "🚀 새 테스트 시작" 패널에서 바로 새 벤치마크를 실행할 수 있다.
실행할 때마다 타임스탬프가 붙은 새 라벨로 저장되어 과거 실행 기록을 덮어쓰지
않고 계속 쌓인다 (기존 CSV 라벨과 겹치지 않게 하기 위함).

사용법:
  python server.py --results-dir ./results --port 8899
  브라우저에서 http://localhost:8899 접속
"""

import argparse
import asyncio
import csv
import io
import os
import threading
import time
import traceback

from flask import Flask, jsonify, request, send_from_directory

from benchmark_app import (
    DEFAULT_PROMPT,
    detect_spec,
    generate_dashboard,
    generate_pdf_report,
    run_benchmark,
    save_csv,
    save_device_meta,
)

app = Flask(__name__)

STATE_LOCK = threading.Lock()
STATE = {"running": False, "label": None, "log": [], "error": None}


def _log(msg: str):
    STATE["log"].append(msg)
    STATE["log"] = STATE["log"][-200:]
    print(msg)


def _run_benchmark_job(results_dir, url, model, label, prompt, concurrency_levels, num_requests):
    try:
        _log(f"[시작] label={label} model={model} concurrency={concurrency_levels} num_requests={num_requests}")
        rows = asyncio.run(run_benchmark(url, model, prompt, concurrency_levels, num_requests))
        csv_path = save_csv(results_dir, label, rows)
        _log(f"결과 저장: {csv_path}")
        generate_dashboard(results_dir)
        _log("대시보드 갱신 완료. 새로고침하면 결과가 보인다.")
    except Exception as e:
        STATE["error"] = str(e)
        _log(f"[에러] {e}")
        _log(traceback.format_exc())
    finally:
        with STATE_LOCK:
            STATE["running"] = False
            STATE["label"] = None


@app.route("/")
def index():
    results_dir = app.config["RESULTS_DIR"]
    running_label = STATE["label"] if STATE["running"] else None
    generate_dashboard(results_dir, running_label=running_label)
    return send_from_directory(results_dir, "dashboard.html")


@app.route("/report.pdf")
def report_pdf():
    return send_from_directory(app.config["RESULTS_DIR"], "report.pdf")


@app.route("/assets/<path:filename>")
def assets(filename):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(os.path.join(base_dir, "assets"), filename)


@app.route("/api/status")
def api_status():
    return jsonify({
        "running": STATE["running"],
        "label": STATE["label"],
        "log": STATE["log"][-50:],
        "error": STATE["error"],
    })


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True, silent=True) or {}

    with STATE_LOCK:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "이미 다른 테스트가 실행 중입니다"}), 409
        STATE["running"] = True
        STATE["error"] = None
        STATE["log"] = []

        url = data.get("url") or "http://localhost:11434/v1/chat/completions"
        model = data.get("model") or "gemma4:12b"
        base_label = (data.get("label") or model).replace(":", "_").replace("/", "_").strip() or "run"
        label = f"{base_label}_{time.strftime('%Y%m%d_%H%M%S')}"
        prompt = data.get("prompt") or DEFAULT_PROMPT
        try:
            concurrency_levels = [int(x) for x in str(data.get("concurrency") or "1,5,10").split(",")]
            num_requests = int(data.get("num_requests") or 15)
        except ValueError:
            STATE["running"] = False
            return jsonify({"ok": False, "error": "동시성/요청 수는 숫자여야 합니다"}), 400

        STATE["label"] = label

    results_dir = app.config["RESULTS_DIR"]
    try:
        save_device_meta(results_dir, base_label, spec=detect_spec())
    except Exception as e:
        _log(f"[사양 감지 실패, 무시하고 진행] {e}")
    generate_dashboard(results_dir, running_label=label)

    thread = threading.Thread(
        target=_run_benchmark_job,
        args=(results_dir, url, model, label, prompt, concurrency_levels, num_requests),
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "label": label})


@app.route("/api/import-csv", methods=["POST"])
def api_import_csv():
    """대시보드의 '결과 요약' 붙여넣기 패널에서 온 CSV 텍스트를 results/{label}.csv로 저장.
    파일 전송이나 별도 터미널 명령 없이, 붙여넣은 결과를 그래프·PDF 보고서에 실제로 반영시키기 위함."""
    data = request.get_json(force=True, silent=True) or {}
    label = (data.get("label") or "").strip()
    csv_text = data.get("csv_text") or ""
    if not label:
        return jsonify({"ok": False, "error": "label이 필요합니다"}), 400

    rows = list(csv.DictReader(io.StringIO(csv_text)))
    if not rows:
        return jsonify({"ok": False, "error": "CSV 내용이 비어있거나 형식이 아닙니다"}), 400

    results_dir = app.config["RESULTS_DIR"]
    out_path = os.path.join(results_dir, f"{label}.csv")
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    generate_dashboard(results_dir)
    return jsonify({"ok": True, "path": out_path, "rows": len(rows)})


@app.route("/api/set-meta", methods=["POST"])
def api_set_meta():
    data = request.get_json(force=True, silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"ok": False, "error": "label이 필요합니다"}), 400
    spec = (data.get("spec") or "").strip()
    price_krw = data.get("price_krw")
    try:
        price_krw = float(price_krw) if price_krw is not None else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "price_krw는 숫자여야 합니다"}), 400

    results_dir = app.config["RESULTS_DIR"]
    save_device_meta(results_dir, label, spec=spec, price_krw=price_krw)
    generate_dashboard(results_dir)
    return jsonify({"ok": True})


@app.route("/api/make-report", methods=["POST"])
def api_make_report():
    results_dir = app.config["RESULTS_DIR"]
    path = generate_pdf_report(results_dir)
    return jsonify({"ok": True, "path": path})


def main():
    parser = argparse.ArgumentParser(description="브라우저의 '테스트 시작' 버튼으로 벤치마크를 실행하는 로컬 서버")
    parser.add_argument("--results-dir", default="./llm_benchmark_results")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()

    results_dir = os.path.expanduser(args.results_dir)
    os.makedirs(results_dir, exist_ok=True)
    app.config["RESULTS_DIR"] = results_dir
    generate_dashboard(results_dir)

    print(f"http://localhost:{args.port} 에서 대시보드를 열어라 (Ctrl+C로 종료)")
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
