"""
results/ 폴더 안의 모든 CSV(Mac, Windows 등 여러 장비 결과)를 모아
report.pdf 최종 보고서를 생성한다.

benchmark_app.py 실행이나 import_result.py 붙여넣기는 CSV와 대시보드만
갱신하고 PDF는 만들지 않는다 — 모든 장비 결과를 다 모은 뒤 이 스크립트를
마지막에 한 번 실행해서 최종 보고서를 만드는 방식.

사용법:
  python make_report.py --results-dir ./results
"""

import argparse

from benchmark_app import generate_pdf_report


def main():
    parser = argparse.ArgumentParser(description="results/ 폴더의 모든 CSV로 report.pdf 최종 보고서 생성")
    parser.add_argument("--results-dir", default="./llm_benchmark_results")
    args = parser.parse_args()

    report_path = generate_pdf_report(args.results_dir)
    print(f"보고서 생성: {report_path}")


if __name__ == "__main__":
    main()
