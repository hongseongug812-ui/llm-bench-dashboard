"""
다른 장비(Windows 등)에서 benchmark_app.py를 실행해 나온 결과 CSV의 내용을
파일 전송 없이 그대로 복사해서 붙여넣으면, 이 폴더의 비교 대시보드에
바로 반영해주는 도구. (report.pdf는 여기서 자동 생성되지 않음 — 모든 장비
결과를 다 모은 뒤 make_report.py를 따로 실행해서 최종 보고서를 만든다)

사용법:
  1. 다른 장비에서 benchmark_app.py 실행 → results/{label}.csv 생성됨
  2. 그 CSV 파일을 텍스트 에디터로 열어 전체 복사
  3. 이 장비에서:
       python import_result.py --label windows_gemma4_12b --results-dir ./results
     실행 후 복사한 CSV 내용을 붙여넣고 끝나면
       Mac/Linux: Ctrl+D
       Windows:   Ctrl+Z 후 Enter
  4. 모든 장비 결과를 다 모았으면:
       python make_report.py --results-dir ./results
"""

import argparse
import csv
import io
import os
import sys
import webbrowser

from benchmark_app import generate_dashboard


def main():
    parser = argparse.ArgumentParser(description="다른 장비에서 실행한 benchmark_app.py 결과 CSV를 붙여넣어 등록")
    parser.add_argument("--label", required=True, help="예: windows_gemma4_12b (CSV 파일명 & 범례로 사용)")
    parser.add_argument("--results-dir", default="./llm_benchmark_results")
    parser.add_argument("--no-browser", action="store_true", help="등록 후 브라우저 자동 오픈 끄기")
    args = parser.parse_args()

    print(f"'{args.label}' 결과 CSV 내용을 붙여넣으세요.")
    print("(끝나면 Mac/Linux는 Ctrl+D, Windows는 Ctrl+Z 후 Enter)")
    pasted = sys.stdin.read()

    rows = list(csv.DictReader(io.StringIO(pasted)))
    if not rows:
        print("붙여넣은 내용이 비어있거나 CSV 형식이 아닙니다.", file=sys.stderr)
        sys.exit(1)

    results_dir = os.path.expanduser(args.results_dir)
    os.makedirs(results_dir, exist_ok=True)
    out_path = os.path.join(results_dir, f"{args.label}.csv")
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"등록됨: {out_path} ({len(rows)}개 행)")

    dashboard_path = generate_dashboard(results_dir)
    print(f"대시보드 갱신: {dashboard_path}")
    print(f"모든 장비 결과를 다 모았으면 'python make_report.py --results-dir {args.results_dir}'로 최종 보고서를 생성해라.")

    if not args.no_browser:
        webbrowser.open(f"file://{os.path.abspath(dashboard_path)}")


if __name__ == "__main__":
    main()
