"""
로컬 LLM 벤치마크 + 대시보드 올인원 앱

동작 순서:
  1. OpenAI 호환 엔드포인트(llama.cpp server / vLLM / Ollama)에 TTFT·동시성 부하 테스트 실행
  2. 결과를 --results-dir 폴더에 {label}.csv 로 저장
  3. 같은 폴더 안의 모든 CSV(과거 실행분 포함)를 다시 읽어 dashboard.html 자동 생성/갱신
  4. 기본 브라우저로 대시보드 자동 오픈

핵심: --results-dir 를 Dropbox/iCloud Drive/공유폴더/USB 등으로 지정하면
Mac에서 돌린 결과와 Windows에서 돌린 결과가 같은 폴더에 쌓이고,
아무 쪽에서나 dashboard.html을 열면 두 장비 비교가 바로 보인다.

사용 예:
  # Mac에서
  python benchmark_app.py --url http://localhost:8080/v1/chat/completions \
      --model gemma-4-E4B --label mac_e4b --results-dir ~/Dropbox/llm_bench

  # Windows에서 (같은 공유 폴더 경로)
  python benchmark_app.py --url http://localhost:8080/v1/chat/completions \
      --model gemma-4-E4B --label windows_e4b --results-dir ~/Dropbox/llm_bench
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import json
import os
import platform
import shutil
import statistics
import time
import webbrowser

import httpx
import psutil
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

DEFAULT_PROMPT = "너는 고객 상담 챗봇이다. 배송 지연 문의에 대해 3문장으로 답변해줘."

# 내장 CID 폰트(HYSMyeongJo-Medium)는 라틴 문자를 전각으로 그려 "TTFT"가 "T T F T"처럼 벌어지는 문제가 있어
# 폭이 정상인 오픈소스 한글 폰트(NanumGothic, SIL OFL)로 교체함 — assets/fonts/에 번들
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
PDF_FONT = "NanumGothic"
PDF_FONT_BOLD = "NanumGothic-Bold"
pdfmetrics.registerFont(TTFont(PDF_FONT, os.path.join(_FONT_DIR, "NanumGothic-Regular.ttf")))
pdfmetrics.registerFont(TTFont(PDF_FONT_BOLD, os.path.join(_FONT_DIR, "NanumGothic-Bold.ttf")))
pdfmetrics.registerFontFamily(PDF_FONT, normal=PDF_FONT, bold=PDF_FONT_BOLD)


async def single_request(client: httpx.AsyncClient, url: str, model: str, prompt: str) -> dict:
    """단일 요청의 TTFT / 총 소요시간 / 생성 토큰 수를 측정. 실패 시 error 필드에 사유 기록"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 256,
    }
    t_start = time.perf_counter()
    ttft = None
    token_count = 0
    error = None

    try:
        async with client.stream("POST", url, json=payload, timeout=120) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                text = delta.get("content") or delta.get("reasoning")  # thinking 모델은 reasoning 필드로 먼저 스트리밍됨
                if text:
                    if ttft is None:
                        ttft = time.perf_counter() - t_start  # 핵심 로직: 첫 토큰 도착 시각
                    token_count += 1
    except httpx.TimeoutException:
        error = "timeout"
    except httpx.HTTPStatusError as e:
        error = f"http_{e.response.status_code}"
    except httpx.HTTPError:
        error = "connection_error"

    total_time = time.perf_counter() - t_start
    gen_time = total_time - (ttft or 0)
    tok_per_sec = token_count / gen_time if gen_time > 0 else 0

    return {
        "ttft_s": round(ttft or 0, 3),
        "total_s": round(total_time, 3),
        "tokens": token_count,
        "tok_per_sec": round(tok_per_sec, 2),
        "error": error,
    }


async def sample_ram(samples: list, stop_event: asyncio.Event, interval: float = 0.5):
    """부하 중 시스템 RAM 사용량(GB)을 주기적으로 샘플링 (장비 사양 판단용)"""
    while not stop_event.is_set():
        samples.append(psutil.virtual_memory().used / (1024 ** 3))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def _detect_power_method() -> str | None:
    """
    전력 측정 방식 자동 감지.
    - Windows/Linux + Nvidia GPU: nvidia-smi (별도 권한 불필요)
    - macOS: powermetrics (root 권한 필수 — sudo로 실행해야 값이 잡힘, 아니면 측정 생략)
    """
    if shutil.which("nvidia-smi"):
        return "nvidia-smi"
    if platform.system() == "Darwin" and hasattr(os, "geteuid") and os.geteuid() == 0:
        return "powermetrics"
    return None


POWER_METHOD = _detect_power_method()


async def _read_power_once_w() -> float | None:
    """GPU/SoC 전력 순간치(W)를 1회 측정. 실패하면 None."""
    try:
        if POWER_METHOD == "nvidia-smi":
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            return float(out.decode().strip().splitlines()[0])
        if POWER_METHOD == "powermetrics":
            proc = await asyncio.create_subprocess_exec(
                "powermetrics", "-n", "1", "-i", "200", "--samplers", "cpu_power,gpu_power",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            for line in out.decode(errors="ignore").splitlines():
                if "Combined Power" in line:
                    return float(line.split(":")[1].strip().split()[0]) / 1000.0  # mW → W
    except Exception:
        return None
    return None


async def sample_power(samples: list, stop_event: asyncio.Event, interval: float = 1.0):
    """부하 중 전력 소모량(W)을 주기적으로 샘플링. 측정 방식 미지원 환경에서는 조용히 건너뜀"""
    if POWER_METHOD is None:
        return
    while not stop_event.is_set():
        watt = await _read_power_once_w()
        if watt is not None:
            samples.append(watt)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def run_concurrency_level(url: str, model: str, prompt: str, concurrency: int, num_requests: int) -> tuple:
    """동시 요청 N개를 반복 실행 (실전 챗봇 트래픽 시뮬레이션), RAM·전력 사용량도 함께 샘플링"""
    results = []
    ram_samples = []
    power_samples = []
    stop_event = asyncio.Event()
    ram_sampler = asyncio.create_task(sample_ram(ram_samples, stop_event))
    power_sampler = asyncio.create_task(sample_power(power_samples, stop_event))
    wall_start = time.perf_counter()
    async with httpx.AsyncClient() as client:
        for _ in range(max(1, num_requests // concurrency)):
            batch = await asyncio.gather(
                *[single_request(client, url, model, prompt) for _ in range(concurrency)]
            )
            results.extend(batch)
    wall_time = time.perf_counter() - wall_start
    stop_event.set()
    await ram_sampler
    await power_sampler
    return results, ram_samples, power_samples, wall_time


def summarize(results: list, ram_samples: list, power_samples: list, wall_time: float) -> dict:
    """
    avg_tok_per_sec: 요청 1건이 체감하는 평균 생성 속도(사용자 관점)
    aggregate_tok_per_sec: 이 동시성 구간 전체 실행 시간(wall_time) 동안 실제로 뽑아낸 총 토큰 수 기준 처리량(서버 관점).
    순차 배치를 단순히 합산하면 동시성=1에서도 값이 배치 수만큼 부풀려지므로 wall_time 기준으로 계산함.
    power_avg_w/power_peak_w: 측정 불가 환경(권한 없음/지원 안 함)에서는 "-"로 표기됨.
    """
    ok = [r for r in results if not r.get("error")]
    error_rate = round(100 * (len(results) - len(ok)) / len(results), 1) if results else 0.0
    ram_avg = round(statistics.mean(ram_samples), 2) if ram_samples else 0.0
    ram_peak = round(max(ram_samples), 2) if ram_samples else 0.0
    power_avg = round(statistics.mean(power_samples), 1) if power_samples else "-"
    power_peak = round(max(power_samples), 1) if power_samples else "-"

    if not ok:
        return {
            "p50_ttft": 0.0, "p95_ttft": 0.0, "ttft_stddev": 0.0,
            "avg_tok_per_sec": 0.0, "aggregate_tok_per_sec": 0.0, "tok_stddev": 0.0,
            "avg_total_s": 0.0, "error_rate": error_rate,
            "ram_avg_gb": ram_avg, "ram_peak_gb": ram_peak,
            "power_avg_w": power_avg, "power_peak_w": power_peak,
        }

    ttfts = [r["ttft_s"] for r in ok]
    toks = [r["tok_per_sec"] for r in ok]
    totals = [r["total_s"] for r in ok]
    total_tokens = sum(r["tokens"] for r in ok)
    return {
        "p50_ttft": round(statistics.median(ttfts), 3),
        "p95_ttft": round(sorted(ttfts)[int(len(ttfts) * 0.95) - 1], 3) if len(ttfts) > 1 else ttfts[0],
        "ttft_stddev": round(statistics.pstdev(ttfts), 3) if len(ttfts) > 1 else 0.0,
        "avg_tok_per_sec": round(statistics.mean(toks), 2),
        "aggregate_tok_per_sec": round(total_tokens / wall_time, 2) if wall_time > 0 else 0.0,
        "tok_stddev": round(statistics.pstdev(toks), 2) if len(toks) > 1 else 0.0,
        "avg_total_s": round(statistics.mean(totals), 3),
        "error_rate": error_rate,
        "ram_avg_gb": ram_avg,
        "ram_peak_gb": ram_peak,
        "power_avg_w": power_avg,
        "power_peak_w": power_peak,
    }


async def warmup(url: str, model: str, prompt: str) -> None:
    """측정 시작 전 워밍업 1회 실행 — 모델 최초 로딩 지연이 첫 요청의 TTFT/안정성 통계를 오염시키는 것을 방지"""
    print("[웜업] 모델 최초 로딩 대기 중...")
    async with httpx.AsyncClient() as client:
        await single_request(client, url, model, prompt)


async def run_benchmark(url: str, model: str, prompt: str, concurrency_levels: list, num_requests: int) -> list:
    await warmup(url, model, prompt)
    all_rows = []
    for c in concurrency_levels:
        print(f"[동시성={c}] 테스트 중...")
        results, ram_samples, power_samples, wall_time = await run_concurrency_level(
            url, model, prompt, c, num_requests)
        summary = summarize(results, ram_samples, power_samples, wall_time)
        summary["concurrency"] = c
        print(f"  TTFT p50={summary['p50_ttft']}s(σ{summary['ttft_stddev']}) | "
              f"평균 tok/s={summary['avg_tok_per_sec']}(σ{summary['tok_stddev']}) | "
              f"전체처리량={summary['aggregate_tok_per_sec']} tok/s | "
              f"평균응답={summary['avg_total_s']}s | 에러율={summary['error_rate']}% | "
              f"RAM평균={summary['ram_avg_gb']}GB(peak {summary['ram_peak_gb']}GB) | "
              f"전력평균={summary['power_avg_w']}W(peak {summary['power_peak_w']}W)")
        all_rows.append(summary)
    return all_rows


def save_csv(results_dir: str, label: str, rows: list) -> str:
    path = os.path.join(results_dir, f"{label}.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def load_all_results(results_dir: str) -> list:
    """결과 폴더 안의 모든 CSV(다른 장비가 넣은 것 포함)를 읽어 대시보드용 데이터로 변환"""
    datasets = []
    for path in sorted(glob.glob(os.path.join(results_dir, "*.csv"))):
        label = os.path.splitext(os.path.basename(path))[0]
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            for k, v in r.items():
                try:
                    r[k] = float(v)
                except (TypeError, ValueError):
                    pass
        datasets.append({"label": label, "rows": rows})
    return datasets


DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>로컬 LLM 벤치마크 대시보드</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>⚡</text></svg>">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  :root {
    --page: #0d0d0d; --surface: #1a1a19; --surface-2: #232320; --border: rgba(255,255,255,0.10);
    --ink: #ffffff; --ink-2: #c3c2b7; --ink-muted: #898781; --grid: #2c2c2a; --baseline: #383835;
    --series-1: #3987e5; --series-2: #199e70; --series-3: #c98500; --series-4: #008300;
    --series-5: #9085e9; --series-6: #e66767; --series-7: #d55181; --series-8: #d95926;
    --good: #0ca30c; --warning: #fab219; --critical: #d03b3b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px; background: var(--page); color: var(--ink);
    font-family: system-ui, -apple-system, "Pretendard", "Segoe UI", "Malgun Gothic", sans-serif;
  }
  .wrap { max-width: 1180px; margin: 0 auto; }
  header { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-bottom: 4px; }
  h1 { font-size: 21px; margin: 0; letter-spacing: -0.01em; }
  .sub { color: var(--ink-2); font-size: 13px; }
  .stamp { color: var(--ink-muted); font-size: 12px; margin-bottom: 22px; }
  .report-link {
    color: var(--series-1); text-decoration: none; font-size: 13px; border: 1px solid var(--border);
    padding: 5px 12px; border-radius: 999px; transition: background 0.15s;
  }
  .report-link:hover { background: var(--surface-2); }

  .panel {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px; margin-bottom: 18px;
    transition: border-color 0.15s;
  }
  .panel-title { font-size: 13px; color: var(--ink-2); font-weight: 600; margin-bottom: 14px; }

  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; margin-bottom: 18px; }
  .tile {
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 16px 18px; transition: transform 0.15s, border-color 0.15s, box-shadow 0.15s;
  }
  .tile:hover {
    transform: translateY(-2px); border-color: rgba(255,255,255,0.22);
    box-shadow: 0 8px 20px rgba(0,0,0,0.35);
  }
  .tile-label { font-size: 12px; color: var(--ink-muted); margin-bottom: 10px; display: flex; align-items: center; justify-content: space-between; }
  .tile-stats { display: flex; gap: 18px; margin-bottom: 12px; }
  .tile-stat-value { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.1; }
  .tile-stat-unit { font-size: 11px; color: var(--ink-muted); margin-top: 3px; }
  .badge {
    display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600;
    padding: 3px 9px; border-radius: 999px;
  }
  .badge::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .badge-good { color: var(--good); background: rgba(12,163,12,0.14); }
  .copy-btn {
    font-size: 10.5px; font-weight: 600; color: var(--ink-2); background: var(--surface-2);
    border: 1px solid var(--border); border-radius: 999px; padding: 3px 9px; cursor: pointer;
    font-family: inherit; transition: background 0.15s, color 0.15s;
  }
  .copy-btn:hover { background: rgba(255,255,255,0.14); color: var(--ink); }
  .copy-btn.copied { color: var(--good); border-color: var(--good); }
  .badge-critical { color: var(--critical); background: rgba(208,59,59,0.14); }

  .criteria { display: flex; flex-wrap: wrap; gap: 6px; padding-top: 12px; border-top: 1px solid var(--border); }
  .chip {
    display: inline-flex; align-items: center; gap: 4px; font-size: 10.5px; font-weight: 600;
    padding: 3px 8px; border-radius: 999px; color: var(--ink-2); background: var(--surface-2);
  }
  .chip-pass { color: var(--good); }
  .chip-fail { color: var(--critical); }

  .compare-headline { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
  .compare-headline .crown { font-size: 20px; }
  .compare-headline b { font-size: 16px; }
  .compare-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  .compare-table th, .compare-table td { padding: 9px 10px; text-align: center; border-bottom: 1px solid var(--grid); }
  .compare-table th:first-child, .compare-table td:first-child { text-align: left; color: var(--ink-2); }
  .compare-table th { color: var(--ink-muted); font-weight: 600; font-size: 11px; text-transform: uppercase; }
  .win-cell { font-weight: 700; color: var(--good); }
  .win-cell .medal { margin-right: 4px; }

  .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media (max-width: 900px) { .charts { grid-template-columns: 1fr; } .tiles { grid-template-columns: 1fr; } }
  .chart-box {
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .chart-box:hover { border-color: rgba(255,255,255,0.18); box-shadow: 0 8px 20px rgba(0,0,0,0.3); }
  .chart-title { font-size: 13px; color: var(--ink-2); margin-bottom: 10px; }

  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; min-width: 960px; font-variant-numeric: tabular-nums; }
  th, td { padding: 9px 10px; text-align: right; border-bottom: 1px solid var(--grid); }
  th:first-child, td:first-child { text-align: left; font-variant-numeric: normal; }
  th { color: var(--ink-muted); font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.02em; }
  tbody tr:hover { background: var(--surface-2); }
  .best { color: var(--good); font-weight: 700; }
  .worst { color: var(--critical); font-weight: 700; }
  .empty { color: var(--ink-muted); font-size: 13px; padding: 40px 0; text-align: center; }

  .running {
    display: none; align-items: center; background: rgba(250,178,25,0.10); border: 1px solid rgba(250,178,25,0.35);
    color: var(--warning); padding: 14px 18px; border-radius: 12px; margin-bottom: 18px; font-size: 14px;
  }
  .spin {
    display: inline-block; width: 11px; height: 11px; margin-right: 10px; flex: none;
    border: 2px solid var(--warning); border-top-color: transparent; border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>⚡ 로컬 LLM 벤치마크 대시보드</h1>
  <a class="report-link" href="report.pdf" target="_blank">📄 PDF 보고서 열기</a>
</header>
<div class="sub">결과 폴더(__RESULTS_DIR__)에 있는 모든 실행 결과를 자동으로 모아서 비교</div>
<div class="stamp">마지막 생성: __GENERATED_AT__</div>

<div id="runningBanner" class="running"></div>

<div class="panel">
  <div class="panel-title">🚀 새 테스트 시작</div>
  <div class="sub" style="margin-bottom:10px;">
    <code>python server.py</code>로 이 페이지를 띄운 경우에만 동작한다 (파일을 직접 열었다면 동작하지 않는다).
    실행할 때마다 타임스탬프가 붙은 새 라벨로 저장되므로, 여러 번 눌러도 이전 기록을 덮어쓰지 않고 계속 쌓인다.
  </div>
  <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:8px; margin-bottom:10px;">
    <input id="runUrl" type="text" placeholder="엔드포인트 URL" value="http://localhost:11434/v1/chat/completions"
           style="background:var(--surface-2); border:1px solid var(--border); color:var(--ink); padding:8px 10px; border-radius:8px; font-family:inherit; font-size:12.5px;">
    <input id="runModel" type="text" placeholder="모델명" value="gemma4:12b"
           style="background:var(--surface-2); border:1px solid var(--border); color:var(--ink); padding:8px 10px; border-radius:8px; font-family:inherit; font-size:12.5px;">
    <input id="runLabel" type="text" placeholder="라벨(선택, 비우면 모델명+시간)"
           style="background:var(--surface-2); border:1px solid var(--border); color:var(--ink); padding:8px 10px; border-radius:8px; font-family:inherit; font-size:12.5px;">
    <input id="runConcurrency" type="text" placeholder="동시성" value="1,5,10"
           style="background:var(--surface-2); border:1px solid var(--border); color:var(--ink); padding:8px 10px; border-radius:8px; font-family:inherit; font-size:12.5px;">
    <input id="runNumRequests" type="text" placeholder="동시성별 요청 수" value="15"
           style="background:var(--surface-2); border:1px solid var(--border); color:var(--ink); padding:8px 10px; border-radius:8px; font-family:inherit; font-size:12.5px;">
  </div>
  <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
    <button class="copy-btn" id="startRunBtn">▶ 테스트 시작</button>
    <span id="runStatus" class="sub"></span>
  </div>
  <div id="runLog" style="display:none; margin-top:10px; font-family:ui-monospace,monospace; font-size:11.5px; color:var(--ink-2); white-space:pre-wrap; max-height:160px; overflow-y:auto; background:var(--surface-2); padding:8px; border-radius:8px;"></div>
</div>

<div class="panel">
  <div class="panel-title">📥 결과 요약 — 다른 장비 결과 붙여넣기</div>
  <div class="sub" style="margin-bottom:10px;">
    다른 장비(Windows 등)에서 benchmark_app.py를 실행해 나온 CSV 내용을 그대로 붙여넣으면, 이 화면(차트·비교)에 바로 반영된다.
    파일 전송은 필요 없다. 단, 이건 <b>이 브라우저 화면에서만 보이는 미리보기</b>이며 새로고침하면 사라진다 —
    나중에도 남기려면 "CSV 다운로드"로 저장한 뒤 <code>results/</code> 폴더에 넣어라.
  </div>
  <div style="display:flex; gap:10px; flex-wrap:wrap; margin-bottom:10px;">
    <input id="pasteLabel" type="text" placeholder="라벨 (예: windows_gemma4_12b)"
           style="flex:1; min-width:220px; background:var(--surface-2); border:1px solid var(--border); color:var(--ink); padding:8px 10px; border-radius:8px; font-family:inherit; font-size:13px;">
  </div>
  <textarea id="pasteCsv" rows="4" placeholder="CSV 내용을 여기에 붙여넣으세요 (헤더 포함)"
            style="width:100%; background:var(--surface-2); border:1px solid var(--border); color:var(--ink); padding:10px; border-radius:8px; font-family:ui-monospace,monospace; font-size:12px; resize:vertical;"></textarea>
  <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; align-items:center;">
    <button class="copy-btn" id="addResultBtn">➕ 비교에 추가 (미리보기)</button>
    <button class="copy-btn" id="downloadCsvBtn">💾 CSV 다운로드</button>
    <span id="pasteStatus" class="sub"></span>
  </div>
</div>

<div class="panel">
  <div class="panel-title">📄 최종 보고서 만들기</div>
  <div class="sub" style="margin-bottom:10px;">
    이 페이지는 정적 파일이라 브라우저에서 바로 PDF를 만들 수는 없다. 아래 명령을 복사해서 터미널에 붙여넣으면
    <code>results/</code> 폴더 안 모든 CSV(Mac·Windows 등)를 모아 <code>report.pdf</code>를 새로 생성한다.
    다른 장비 결과까지 <code>results/</code>에 다 모은 뒤 마지막에 한 번 실행하면 된다.
  </div>
  <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
    <button class="copy-btn" id="makeReportBtn">📄 보고서 생성 명령 복사</button>
    <span id="reportStatus" class="sub"></span>
  </div>
</div>

<div id="content">
  <div id="comparePanel" class="panel" style="display:none;"></div>

  <div id="tiles" class="tiles"></div>

  <div class="charts" style="margin-bottom:18px;">
    <div class="chart-box">
      <div class="chart-title">TTFT p50 vs 동시성 (낮을수록 좋음)</div>
      <canvas id="ttftChart" height="220"></canvas>
    </div>
    <div class="chart-box">
      <div class="chart-title">전체 처리량 (aggregate tok/s) vs 동시성 (높을수록 좋음)</div>
      <canvas id="tokChart" height="220"></canvas>
    </div>
    <div class="chart-box">
      <div class="chart-title">안정성 (TTFT 표준편차) vs 동시성 (낮을수록 좋음)</div>
      <canvas id="stddevChart" height="220"></canvas>
    </div>
    <div class="chart-box">
      <div class="chart-title">에러율(%) vs 동시성 (낮을수록 좋음)</div>
      <canvas id="errorChart" height="220"></canvas>
    </div>
    <div class="chart-box">
      <div class="chart-title">RAM 평균 사용량(GB) vs 동시성</div>
      <canvas id="ramChart" height="220"></canvas>
    </div>
    <div class="chart-box">
      <div class="chart-title">평균 응답 완료 시간(s) vs 동시성 (낮을수록 좋음)</div>
      <canvas id="totalTimeChart" height="220"></canvas>
    </div>
    <div class="chart-box">
      <div class="chart-title">전력 소모량 평균(W) vs 동시성 (측정 미지원 환경은 빈 값)</div>
      <canvas id="powerChart" height="220"></canvas>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">상세 결과</div>
    <div class="table-wrap">
      <table id="resultTable"><thead></thead><tbody></tbody></table>
    </div>
  </div>
</div>
<div id="emptyState" class="empty" style="display:none;">결과 CSV가 없음 — 먼저 benchmark_app.py를 실행해라</div>
</div>

<script>
// dataviz skill 참조 팔레트(dark) — 카테고리 색은 고정 순서로만 배정
const COLORS = ['#3987e5', '#199e70', '#c98500', '#008300', '#9085e9', '#e66767', '#d55181', '#d95926'];
const INK_MUTED = '#898781';
const GRID = '#2c2c2a';
let datasets = __EMBEDDED_DATA__;
const RUNNING_LABEL = __RUNNING_LABEL__;
const RESULTS_DIR_ABS = __RESULTS_DIR_JSON__;

// README 판단 기준(judge_adoption)과 동일한 로직의 JS 미러
function judgeAdoption(rows) {
  const mid = rows.filter(r => r.concurrency >= 5 && r.concurrency <= 10);
  const ttftOk = rows.length > 0 && rows.every(r => (r.p50_ttft ?? 999) <= 1.0);
  const tokOk = mid.length > 0 && mid.every(r => (r.avg_tok_per_sec ?? 0) >= 20);
  const errorOk = rows.length > 0 && rows.every(r => (r.error_rate ?? 100) <= 5.0);
  return { ok: ttftOk && tokOk && errorOk, ttftOk, tokOk, errorOk };
}

function render() {
  const banner = document.getElementById('runningBanner');
  if (RUNNING_LABEL) {
    banner.style.display = 'flex';
    banner.innerHTML = `<span class="spin"></span>"${RUNNING_LABEL}" 벤치마크 실행 중입니다. 완료되면 이 페이지를 새로고침하세요.`;
    document.getElementById('content').style.display = 'none';
    document.getElementById('emptyState').style.display = 'none';
    return;
  }
  banner.style.display = 'none';
  document.getElementById('emptyState').style.display = datasets.length ? 'none' : 'block';
  document.getElementById('content').style.display = datasets.length ? 'block' : 'none';
  if (!datasets.length) return;
  renderComparison();
  renderTiles();
  renderChart('ttftChart', 'p50_ttft', 'TTFT (s)');
  renderChart('tokChart', 'aggregate_tok_per_sec', 'tok/s');
  renderChart('stddevChart', 'ttft_stddev', 'TTFT σ (s)');
  renderChart('errorChart', 'error_rate', '에러율 (%)');
  renderChart('ramChart', 'ram_avg_gb', 'RAM (GB)');
  renderChart('totalTimeChart', 'avg_total_s', '평균 응답시간 (s)');
  renderChart('powerChart', 'power_avg_w', '전력 (W)');
  renderTable();
}

// 장비/설정이 2개 이상일 때 지표별 승자를 가려주는 헤드투헤드 비교
const COMPARE_METRICS = [
  { field: 'p50_ttft', label: 'TTFT p50', lowerBetter: true, unit: 's' },
  { field: 'aggregate_tok_per_sec', label: '전체 처리량', lowerBetter: false, unit: 'tok/s' },
  { field: 'ttft_stddev', label: '안정성 (TTFT σ)', lowerBetter: true, unit: 's' },
  { field: 'error_rate', label: '에러율', lowerBetter: true, unit: '%' },
  { field: 'power_avg_w', label: '전력 소모(평균)', lowerBetter: true, unit: 'W' },
];

function renderComparison() {
  const panel = document.getElementById('comparePanel');
  if (datasets.length < 2) { panel.style.display = 'none'; return; }
  panel.style.display = 'block';

  const allConcurrency = [...new Set(datasets.flatMap(d => d.rows.map(r => r.concurrency)))].sort((a, b) => a - b);
  const wins = {}; datasets.forEach(d => wins[d.label] = 0);
  const bodyRows = [];

  COMPARE_METRICS.forEach(m => {
    allConcurrency.forEach(c => {
      const vals = datasets.map(d => {
        const row = d.rows.find(r => r.concurrency === c);
        return row ? row[m.field] : null;
      });
      if (vals.some(v => typeof v !== 'number')) return;  // "-"(측정 불가) 포함된 지표는 비교 생략
      let winnerIdx = 0;
      for (let i = 1; i < vals.length; i++) {
        if (m.lowerBetter ? vals[i] < vals[winnerIdx] : vals[i] > vals[winnerIdx]) winnerIdx = i;
      }
      wins[datasets[winnerIdx].label]++;
      bodyRows.push({ metric: `${m.label} (동시${c})`, vals, unit: m.unit, winnerIdx });
    });
  });

  const ranked = Object.entries(wins).sort((a, b) => b[1] - a[1]);
  const [topLabel, topWins] = ranked[0];
  const tie = ranked.length > 1 && ranked[0][1] === ranked[1][1];

  const headCols = datasets.map(d => `<th>${d.label}</th>`).join('');
  const rows = bodyRows.map(r => {
    const cells = r.vals.map((v, i) =>
      `<td class="${i === r.winnerIdx ? 'win-cell' : ''}">${i === r.winnerIdx ? '<span class="medal">🏆</span>' : ''}${v}${r.unit}</td>`
    ).join('');
    return `<tr><td>${r.metric}</td>${cells}</tr>`;
  }).join('');

  panel.innerHTML = `
    <div class="panel-title">⚔️ 장비 비교</div>
    <div class="compare-headline">
      <span class="crown">${tie ? '🤝' : '🏆'}</span>
      <b>${tie ? '동률입니다' : `${topLabel} 우세`}</b>
      <span class="sub">(${ranked.map(([l, w]) => `${l} ${w}승`).join(' · ')})</span>
    </div>
    <div class="table-wrap">
      <table class="compare-table">
        <thead><tr><th>지표 (동시성별)</th>${headCols}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderTiles() {
  document.getElementById('tiles').innerHTML = datasets.map((d, i) => {
    const rows = d.rows;
    const bestTok = Math.max(...rows.map(r => r.aggregate_tok_per_sec || 0));
    const bestTtft = Math.min(...rows.map(r => r.p50_ttft ?? Infinity));
    const v = judgeAdoption(rows);
    const color = COLORS[i % COLORS.length];
    const chip = (pass, text) => `<span class="chip ${pass ? 'chip-pass' : 'chip-fail'}">${pass ? '✓' : '✗'} ${text}</span>`;
    return `
      <div class="tile">
        <div class="tile-label">
          <span style="color:${color};font-weight:600;">● ${d.label}</span>
          <span class="badge ${v.ok ? 'badge-good' : 'badge-critical'}">${v.ok ? '채택 가능' : '채택 보류'}</span>
        </div>
        <div class="tile-stats">
          <div>
            <div class="tile-stat-value">${bestTok.toFixed(1)}</div>
            <div class="tile-stat-unit">최대 처리량 tok/s</div>
          </div>
          <div>
            <div class="tile-stat-value">${bestTtft === Infinity ? '-' : bestTtft.toFixed(2)}</div>
            <div class="tile-stat-unit">최소 TTFT (s)</div>
          </div>
        </div>
        <div class="criteria">
          ${chip(v.ttftOk, 'TTFT≤1s')}
          ${chip(v.tokOk, '동시5~10 ≥20tok/s')}
          ${chip(v.errorOk, '에러율≤5%')}
        </div>
        <div style="margin-top:12px;">
          <button class="copy-btn tile-copy-btn" data-label="${d.label}">📋 CSV 복사 (다른 장비 결과 붙여넣기용)</button>
        </div>
      </div>`;
  }).join('');

  // .copy-btn은 여러 버튼(테스트 시작·보고서 명령 복사 등)이 스타일 공유용으로 같이 쓰므로,
  // 이 라벨별 CSV 복사 핸들러는 .tile-copy-btn으로 범위를 좁혀서 붙인다 (안 그러면 다른 버튼까지 걸림)
  document.querySelectorAll('.tile-copy-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      copyText(labelCsv(btn.dataset.label));
      const original = btn.textContent;
      btn.textContent = '✅ 복사됨 — import_result.py에 붙여넣으세요';
      btn.classList.add('copied');
      setTimeout(() => { btn.textContent = original; btn.classList.remove('copied'); }, 1800);
    });
  });
}

function labelCsv(label) {
  const ds = datasets.find(d => d.label === label);
  if (!ds || !ds.rows.length) return '';
  const cols = Object.keys(ds.rows[0]);
  const lines = [cols.join(',')];
  ds.rows.forEach(r => lines.push(cols.map(c => r[c]).join(',')));
  return lines.join('\\n');
}

function copyText(text) {
  // file:// 페이지에서는 navigator.clipboard가 막히는 경우가 많아 execCommand로 폴백
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand('copy');
  } catch (e) {
    if (navigator.clipboard) navigator.clipboard.writeText(text);
  }
  document.body.removeChild(ta);
}

function renderChart(canvasId, field, axisLabel) {
  const allConcurrency = [...new Set(datasets.flatMap(d => d.rows.map(r => r.concurrency)))].sort((a, b) => a - b);
  const chartDatasets = datasets.map((d, i) => ({
    label: d.label,
    data: allConcurrency.map(c => {
      const row = d.rows.find(r => r.concurrency === c);
      const v = row ? row[field] : null;
      return (typeof v === 'number') ? v : null;  // "-"(측정 불가) 등 비수치 값은 공백 처리
    }),
    borderColor: COLORS[i % COLORS.length],
    backgroundColor: COLORS[i % COLORS.length],
    borderWidth: 2,
    pointRadius: 4,
    tension: 0.25,
    spanGaps: true,
  }));
  new Chart(document.getElementById(canvasId).getContext('2d'), {
    type: 'line',
    data: { labels: allConcurrency.map(c => `동시 ${c}`), datasets: chartDatasets },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: datasets.length > 1, labels: { color: '#c3c2b7', usePointStyle: true, pointStyle: 'circle' } },
        tooltip: {
          backgroundColor: '#232320', borderColor: 'rgba(255,255,255,0.10)', borderWidth: 1,
          titleColor: '#ffffff', bodyColor: '#c3c2b7', padding: 10, cornerRadius: 8,
          displayColors: true, boxPadding: 4,
        },
      },
      scales: {
        x: { ticks: { color: INK_MUTED }, grid: { color: GRID } },
        y: { ticks: { color: INK_MUTED }, grid: { color: GRID }, title: { display: true, text: axisLabel, color: INK_MUTED } },
      },
    },
  });
}

function renderTable() {
  const cols = ['label', 'concurrency', 'p50_ttft', 'p95_ttft', 'ttft_stddev', 'avg_tok_per_sec', 'tok_stddev',
                'aggregate_tok_per_sec', 'avg_total_s', 'error_rate', 'ram_avg_gb', 'ram_peak_gb',
                'power_avg_w', 'power_peak_w'];
  const headers = ['결과', '동시성', 'TTFT p50(s)', 'TTFT p95(s)', 'TTFT σ', '평균 tok/s', 'tok/s σ',
                    '전체 tok/s', '평균응답(s)', '에러율(%)', 'RAM평균(GB)', 'RAM피크(GB)',
                    '전력평균(W)', '전력피크(W)'];
  document.querySelector('#resultTable thead').innerHTML = '<tr>' + headers.map(h => `<th>${h}</th>`).join('') + '</tr>';

  const allRows = [];
  datasets.forEach(d => d.rows.forEach(r => allRows.push({ label: d.label, ...r })));
  const bestTok = Math.max(...allRows.map(r => r.aggregate_tok_per_sec || 0));
  const worstTtft = Math.max(...allRows.map(r => r.p50_ttft || 0));

  document.querySelector('#resultTable tbody').innerHTML = allRows.map(r => {
    return '<tr>' + cols.map(c => {
      let cls = c === 'aggregate_tok_per_sec' && r[c] === bestTok ? 'best'
              : c === 'p50_ttft' && r[c] === worstTtft ? 'worst'
              : c === 'error_rate' && r[c] > 0 ? 'worst' : '';
      return `<td class="${cls}">${r[c] ?? '-'}</td>`;
    }).join('') + '</tr>';
  }).join('');
}

// 콤마 구분 CSV(따옴표 없는 단순 숫자표) 텍스트를 파싱 — benchmark_app.py가 저장하는 형식과 동일
function parseCsv(text) {
  const lines = text.trim().split(/\\r?\\n/).filter(l => l.trim().length);
  if (lines.length < 2) return null;
  const headers = lines[0].split(',').map(h => h.trim());
  const rows = lines.slice(1).map(line => {
    const parts = line.split(',');
    const row = {};
    headers.forEach((h, i) => {
      const raw = (parts[i] ?? '').trim();
      const n = parseFloat(raw);
      row[h] = (raw !== '' && raw !== '-' && !isNaN(n)) ? n : (raw || '-');
    });
    return row;
  });
  return rows;
}

document.getElementById('addResultBtn').addEventListener('click', () => {
  const label = document.getElementById('pasteLabel').value.trim();
  const status = document.getElementById('pasteStatus');
  if (!label) { status.textContent = '⚠️ 라벨(장비 이름)을 먼저 입력하세요'; return; }
  const rows = parseCsv(document.getElementById('pasteCsv').value);
  if (!rows) { status.textContent = '⚠️ CSV 형식이 아닙니다 (헤더 + 최소 1행 필요)'; return; }
  datasets = datasets.filter(d => d.label !== label);
  datasets.push({ label, rows });
  render();
  status.textContent = `✅ "${label}" 비교에 추가됨 — 이 브라우저 화면에서만 반영(새로고침하면 사라짐). 남기려면 CSV 다운로드 후 results/ 폴더에 저장.`;
});

document.getElementById('downloadCsvBtn').addEventListener('click', () => {
  const label = document.getElementById('pasteLabel').value.trim() || 'pasted_result';
  const text = document.getElementById('pasteCsv').value;
  const status = document.getElementById('pasteStatus');
  if (!text.trim()) { status.textContent = '⚠️ 먼저 CSV 내용을 붙여넣으세요'; return; }
  const blob = new Blob([text], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${label}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  status.textContent = `💾 "${label}.csv" 다운로드됨 — results/ 폴더로 옮겨넣어라.`;
});

document.getElementById('makeReportBtn').addEventListener('click', () => {
  const cmd = `python make_report.py --results-dir "${RESULTS_DIR_ABS}"`;
  copyText(cmd);
  const status = document.getElementById('reportStatus');
  status.textContent = `✅ 복사됨: ${cmd}`;
});

// "새 테스트 시작" — python server.py로 띄웠을 때만 /api/run, /api/status가 존재함
let runPollTimer = null;

document.getElementById('startRunBtn').addEventListener('click', async () => {
  const startBtn = document.getElementById('startRunBtn');
  const status = document.getElementById('runStatus');
  const body = {
    url: document.getElementById('runUrl').value.trim(),
    model: document.getElementById('runModel').value.trim(),
    label: document.getElementById('runLabel').value.trim(),
    concurrency: document.getElementById('runConcurrency').value.trim(),
    num_requests: document.getElementById('runNumRequests').value.trim(),
  };
  status.textContent = '⏳ 요청 중...';
  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      status.textContent = `⚠️ ${data.error || '시작 실패'}`;
      return;
    }
    status.textContent = `⏳ "${data.label}" 실행 중...`;
    startBtn.disabled = true;
    pollRunStatus();
  } catch (e) {
    status.textContent = '⚠️ 서버에 연결할 수 없습니다 — python server.py로 이 페이지를 띄웠는지 확인하세요 (파일을 직접 열면 동작하지 않음)';
  }
});

function pollRunStatus() {
  clearInterval(runPollTimer);
  const startBtn = document.getElementById('startRunBtn');
  const status = document.getElementById('runStatus');
  const logBox = document.getElementById('runLog');
  runPollTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/status');
      const data = await res.json();
      logBox.style.display = 'block';
      logBox.textContent = (data.log || []).join('\\n');
      logBox.scrollTop = logBox.scrollHeight;
      if (!data.running) {
        clearInterval(runPollTimer);
        startBtn.disabled = false;
        status.textContent = data.error ? `⚠️ 에러: ${data.error}` : '✅ 완료 — 잠시 후 새로고침됩니다';
        if (!data.error) setTimeout(() => location.reload(), 1500);
      }
    } catch (e) { /* 일시적 네트워크 오류는 무시하고 다음 폴링에서 재시도 */ }
  }, 2000);
}

render();
</script>
</body>
</html>
"""


COMPARE_METRICS = [
    ("p50_ttft", "TTFT p50", True, "s"),
    ("aggregate_tok_per_sec", "전체 처리량", False, "tok/s"),
    ("ttft_stddev", "안정성 (TTFT SD)", True, "s"),
    ("error_rate", "에러율", True, "%"),
    ("power_avg_w", "전력 소모(평균)", True, "W"),
]


def compare_devices(datasets: list) -> dict:
    """label(장비/설정)이 2개 이상일 때, 동시성이 겹치는 지점마다 지표별 승자를 집계해 헤드투헤드 비교 결과를 만듦"""
    all_concurrency = sorted({r.get("concurrency") for ds in datasets for r in ds["rows"]})
    wins = {ds["label"]: 0 for ds in datasets}
    rows = []
    for field, label, lower_better, unit in COMPARE_METRICS:
        for c in all_concurrency:
            vals = []
            complete = True
            for ds in datasets:
                row = next((r for r in ds["rows"] if r.get("concurrency") == c), None)
                if row is None or not isinstance(row.get(field), (int, float)):
                    complete = False  # "-"(측정 불가) 등 비수치 값이 있으면 이 지표는 비교에서 제외
                    break
                vals.append(row[field])
            if not complete:
                continue
            winner_idx = (min if lower_better else max)(range(len(vals)), key=lambda i: vals[i])
            wins[datasets[winner_idx]["label"]] += 1
            rows.append({"metric": f"{label} (동시{int(c)})", "vals": vals, "unit": unit, "winner_idx": winner_idx})
    return {"wins": wins, "rows": rows}


def generate_pdf_report(results_dir: str) -> str:
    """결과 폴더의 모든 CSV를 모아 label별 표 + 채택 판정을 담은 report.pdf 생성"""
    datasets = load_all_results(results_dir)
    out_path = os.path.join(results_dir, "report.pdf")

    styles = {
        "title": ParagraphStyle("title", fontName=PDF_FONT_BOLD, fontSize=20, leading=24, spaceAfter=4,
                                 textColor=colors.HexColor("#12141a")),
        "meta": ParagraphStyle("meta", fontName=PDF_FONT, fontSize=9, textColor=colors.grey, spaceAfter=14),
        "h2": ParagraphStyle("h2", fontName=PDF_FONT_BOLD, fontSize=13, leading=16, spaceBefore=18, spaceAfter=8,
                              textColor=colors.HexColor("#12141a")),
        "body": ParagraphStyle("body", fontName=PDF_FONT, fontSize=10, leading=15),
        "verdict_ok": ParagraphStyle("verdict_ok", fontName=PDF_FONT_BOLD, fontSize=11, leading=16,
                                      textColor=colors.HexColor("#1a7f37"), spaceBefore=8),
        "verdict_ng": ParagraphStyle("verdict_ng", fontName=PDF_FONT_BOLD, fontSize=11, leading=16,
                                      textColor=colors.HexColor("#c0392b"), spaceBefore=8),
    }

    story = [
        Paragraph("로컬 LLM 벤치마크 결과 보고서", styles["title"]),
        Paragraph(f"생성 일시: {time.strftime('%Y-%m-%d %H:%M:%S')} | 결과 폴더: {os.path.abspath(results_dir)}",
                  styles["meta"]),
    ]

    story.append(Paragraph("참고: Mac(Apple Silicon) vs Windows(Nvidia GPU) 아키텍처 개요", styles["h2"]))
    story.append(Paragraph(
        "아래는 일반적으로 알려진 하드웨어 아키텍처 특성이며, 이 보고서가 실측한 수치가 아니다. "
        "실제 성능·전력 수치는 뒤의 측정 결과를 따른다.",
        styles["meta"]))
    arch_header_style = ParagraphStyle("archHeader", fontName=PDF_FONT_BOLD, fontSize=9.5,
                                        textColor=colors.white, alignment=1, leading=12)
    arch_body_style = ParagraphStyle("archBody", fontName=PDF_FONT, fontSize=9, leading=13)
    arch_data = [
        [Paragraph("", arch_header_style), Paragraph("Mac (Apple Silicon, 통합 메모리)", arch_header_style),
         Paragraph("Windows (Nvidia GPU, CUDA)", arch_header_style)],
        [Paragraph("장점", arch_header_style),
         Paragraph("CPU·GPU가 메모리를 공유(Unified Memory)해 VRAM 제약이 없음 — "
                   "70B급 이상 대형 모델도 상대적으로 저렴하게 로컬 구동 가능", arch_body_style),
         Paragraph("CUDA + TensorRT-LLM 등 성숙한 생태계로 순수 연산 처리량이 높고, "
                   "추론 최적화 도구 지원이 폭넓음", arch_body_style)],
        [Paragraph("단점", arch_header_style),
         Paragraph("순수 행렬연산 처리량은 CUDA 전용 가속 대비 낮은 경향이 있고, "
                   "추론 최적화 생태계가 상대적으로 좁음", arch_body_style),
         Paragraph("모델 크기가 GPU의 물리적 VRAM 용량에 묶여, 큰 모델을 돌리려면 "
                   "고VRAM 고가 GPU(또는 다중 GPU)가 필요", arch_body_style)],
    ]
    arch_table = Table(arch_data, colWidths=[20 * mm, 124 * mm, 124 * mm])
    arch_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2a2e3a")),
        ("BACKGROUND", (0, 1), (0, -1), colors.HexColor("#2a2e3a")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (1, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
    ]))
    story.append(arch_table)
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(
        "참고 — 파인튜닝(학습): 모델을 직접 파인튜닝할 계획이 있다면 CUDA 생태계(Windows/Linux + Nvidia)가 "
        "도구·라이브러리 지원 면에서 유리하다. 이 보고서의 측정 범위는 추론(inference) 성능이며, "
        "파인튜닝 성능은 별도 검증이 필요하다.",
        styles["body"]))
    story.append(Spacer(1, 8 * mm))

    if not datasets:
        story.append(Paragraph("결과 CSV가 없습니다. 먼저 벤치마크를 실행하세요.", styles["body"]))
    else:
        if len(datasets) >= 2:
            cmp = compare_devices(datasets)
            ranked = sorted(cmp["wins"].items(), key=lambda kv: kv[1], reverse=True)
            tie = len(ranked) > 1 and ranked[0][1] == ranked[1][1]
            headline = "동률입니다" if tie else f"{ranked[0][0]} 우세"
            story.append(Paragraph("장비 비교", styles["h2"]))
            story.append(Paragraph(
                f"{headline} ({' · '.join(f'{l} {w}승' for l, w in ranked)})",
                styles["verdict_ok"]))
            header_style = ParagraphStyle("cmpHeader", fontName=PDF_FONT_BOLD, fontSize=8.5,
                                           textColor=colors.white, alignment=1, leading=10)
            cmp_header = [Paragraph("지표 (동시성별)", header_style)] + \
                [Paragraph(ds["label"], header_style) for ds in datasets]
            cmp_data = [cmp_header] + [
                [r["metric"]] + [f"{v}{r['unit']}" for v in r["vals"]] for r in cmp["rows"]
            ]
            col_w = min(38 * mm, (269 * mm - 50 * mm) / max(len(datasets), 1))
            cmp_table = Table(cmp_data, colWidths=[50 * mm] + [col_w] * len(datasets))
            cmp_style = [
                ("FONTNAME", (0, 0), (-1, -1), PDF_FONT),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("FONTNAME", (0, 0), (-1, 0), PDF_FONT_BOLD),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2a2e3a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
            ]
            for row_i, r in enumerate(cmp["rows"], start=1):
                col = r["winner_idx"] + 1
                cmp_style.append(("TEXTCOLOR", (col, row_i), (col, row_i), colors.HexColor("#1a7f37")))
                cmp_style.append(("FONTNAME", (col, row_i), (col, row_i), PDF_FONT))
            cmp_table.setStyle(TableStyle(cmp_style))
            story.append(cmp_table)
            story.append(Spacer(1, 10 * mm))

        header = ["동시성", "TTFT p50(s)", "TTFT p95(s)", "TTFT SD", "평균 tok/s", "tok/s SD",
                   "전체 tok/s", "평균응답(s)", "에러율(%)", "RAM평균(GB)", "RAM피크(GB)",
                   "전력평균(W)", "전력피크(W)"]
        col_widths = [14 * mm, 18 * mm, 18 * mm, 14 * mm, 18 * mm, 14 * mm,
                      18 * mm, 18 * mm, 16 * mm, 19 * mm, 19 * mm, 19 * mm, 19 * mm]
        for ds in datasets:
            story.append(Paragraph(f"결과: {ds['label']}", styles["h2"]))
            table_data = [header]
            for r in ds["rows"]:
                table_data.append([
                    r.get("concurrency", "-"),
                    r.get("p50_ttft", "-"),
                    r.get("p95_ttft", "-"),
                    r.get("ttft_stddev", "-"),
                    r.get("avg_tok_per_sec", "-"),
                    r.get("tok_stddev", "-"),
                    r.get("aggregate_tok_per_sec", "-"),
                    r.get("avg_total_s", "-"),
                    r.get("error_rate", "-"),
                    r.get("ram_avg_gb", "-"),
                    r.get("ram_peak_gb", "-"),
                    r.get("power_avg_w", "-"),
                    r.get("power_peak_w", "-"),
                ])
            table = Table(table_data, colWidths=col_widths)
            table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), PDF_FONT),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("FONTNAME", (0, 0), (-1, 0), PDF_FONT_BOLD),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2a2e3a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
            ]))
            story.append(table)

        story.append(Paragraph("참고: 측정 지표 설명", styles["h2"]))
        story.append(Paragraph(
            "TTFT p50/p95는 첫 토큰 응답 시간, tok/s는 초당 생성 토큰 수. "
            "TTFT SD / tok/s SD는 표준편차로 응답 일관성(안정성)을 나타내며, 값이 클수록 응답 편차가 커 사용자 체감 품질이 불안정함을 의미함.",
            styles["body"]))

    def _decorate_page(canvas, doc_):
        canvas.saveState()
        page_w, page_h = landscape(A4)
        canvas.setFillColor(colors.HexColor("#5b8cff"))
        canvas.rect(0, page_h - 4, page_w, 4, stroke=0, fill=1)
        canvas.setFont(PDF_FONT, 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(14 * mm, 10 * mm, "llm-bench-dashboard 자동 생성 보고서")
        canvas.drawRightString(page_w - 14 * mm, 10 * mm, f"{canvas.getPageNumber()}p")
        canvas.restoreState()

    doc = SimpleDocTemplate(out_path, pagesize=landscape(A4),
                             topMargin=16 * mm, bottomMargin=18 * mm,
                             leftMargin=14 * mm, rightMargin=14 * mm)
    doc.build(story, onFirstPage=_decorate_page, onLaterPages=_decorate_page)
    return out_path


def generate_dashboard(results_dir: str, running_label: str = None) -> str:
    """running_label을 넘기면 진행중 배너만 표시하고 기존 데이터는 비워서 보여줌(진행 중인 실행과 과거 결과 혼동 방지)"""
    datasets = [] if running_label else load_all_results(results_dir)
    html = (
        DASHBOARD_TEMPLATE
        .replace("__EMBEDDED_DATA__", json.dumps(datasets, ensure_ascii=False))
        .replace("__RESULTS_DIR__", os.path.abspath(results_dir))
        .replace("__GENERATED_AT__", time.strftime("%Y-%m-%d %H:%M:%S"))
        .replace("__RUNNING_LABEL__", json.dumps(running_label, ensure_ascii=False))
        .replace("__RESULTS_DIR_JSON__", json.dumps(os.path.abspath(results_dir), ensure_ascii=False))
    )
    out_path = os.path.join(results_dir, "dashboard.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="예: http://localhost:8080/v1/chat/completions")
    parser.add_argument("--model", required=True)
    parser.add_argument("--label", required=True, help="예: mac_e4b, windows_e4b (CSV 파일명 & 범례로 사용)")
    parser.add_argument("--results-dir", default="./llm_benchmark_results",
                         help="Dropbox/iCloud Drive 등 공유 폴더를 지정하면 여러 장비 결과를 한 대시보드에서 비교 가능")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--concurrency", default="1,5,10", help="콤마로 구분된 동시 요청 수 리스트")
    parser.add_argument("--num-requests", type=int, default=20, help="동시성 레벨당 총 요청 수")
    parser.add_argument("--no-browser", action="store_true", help="테스트 후 브라우저 자동 오픈 끄기")
    args = parser.parse_args()

    results_dir = os.path.expanduser(args.results_dir)
    os.makedirs(results_dir, exist_ok=True)
    concurrency_levels = [int(x) for x in args.concurrency.split(",")]

    if POWER_METHOD:
        print(f"[전력 측정] {POWER_METHOD} 사용 가능 — 전력 소모량 측정됨")
    elif platform.system() == "Darwin":
        print("[전력 측정] macOS는 powermetrics에 root 권한이 필요함 — "
              "전력 소모량을 측정하려면 'sudo python benchmark_app.py ...'로 재실행. 지금은 '-'로 표기됨")
    else:
        print("[전력 측정] nvidia-smi를 찾을 수 없어 전력 소모량은 '-'로 표기됨")

    dashboard_path = generate_dashboard(results_dir, running_label=args.label)
    if not args.no_browser:
        webbrowser.open(f"file://{os.path.abspath(dashboard_path)}")

    rows = asyncio.run(run_benchmark(args.url, args.model, args.prompt, concurrency_levels, args.num_requests))
    csv_path = save_csv(results_dir, args.label, rows)
    print(f"결과 저장: {csv_path}")

    dashboard_path = generate_dashboard(results_dir)
    print(f"대시보드 갱신: {dashboard_path}")
    print(f"모든 장비 결과를 다 모았으면 'python make_report.py --results-dir {args.results_dir}'로 최종 보고서를 생성해라.")


if __name__ == "__main__":
    main()
