# -*- coding: utf-8 -*-
"""Unified Test Runner and Coverage Analyzer for ClinMind-AI.

Executes both test_engines.py and test_e2e_edge_cases.py,
calculates precise statement-level coverage for all modules in app/,
and produces a structured verification report.
"""

import inspect
import linecache
import os
import sys
import time
import unittest
import tempfile
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_runtime = tempfile.TemporaryDirectory(prefix="clinmind-tests-")
os.environ["CLINMIND_RUNTIME_DIR"] = _runtime.name
os.environ["USE_ONLINE_LLM"] = "false"


class SimpleCoverageTracer:
    """Statement coverage collector for python source files."""

    def __init__(self, target_dir: str):
        self.target_dir = os.path.normpath(os.path.abspath(target_dir)).lower()
        self.executed_lines = {}  # {filepath: set(line_numbers)}

    def trace_fn(self, frame, event, arg):
        if event == "line":
            filename = frame.f_code.co_filename
            if filename:
                norm_fn = os.path.normpath(os.path.abspath(filename)).lower()
                if norm_fn.startswith(self.target_dir) and norm_fn.endswith(".py"):
                    if norm_fn not in self.executed_lines:
                        self.executed_lines[norm_fn] = set()
                    self.executed_lines[norm_fn].add(frame.f_lineno)
        return self.trace_fn

    def get_executable_lines(self, filepath: str) -> set:
        """Find executable source lines in a python file using compile."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                source = f.read()
            code = compile(source, filepath, "exec")
            lines = set()

            def walk_code(co):
                for item in co.co_lines():
                    lineno = item[2]
                    if lineno is not None and lineno > 0:
                        lines.add(lineno)
                for const in co.co_consts:
                    if inspect.iscode(const):
                        walk_code(const)

            walk_code(code)
            return lines
        except Exception:
            return set()

    def generate_report(self) -> dict:
        """Generate structured coverage report for all files in target_dir."""
        report = {}
        total_exec = 0
        total_cov = 0

        for root, _, files in os.walk(self.target_dir):
            for file in files:
                if file.endswith(".py"):
                    full_path = os.path.normpath(os.path.abspath(os.path.join(root, file))).lower()
                    rel_path = os.path.relpath(full_path, str(PROJECT_ROOT).lower()).replace("\\", "/")
                    exec_lines = self.get_executable_lines(full_path)
                    if not exec_lines:
                        continue
                    cov_lines = self.executed_lines.get(full_path, set()) & exec_lines
                    pct = round(len(cov_lines) / len(exec_lines) * 100, 1) if exec_lines else 100.0

                    report[rel_path] = {
                        "executable": len(exec_lines),
                        "covered": len(cov_lines),
                        "percent": pct,
                    }
                    total_exec += len(exec_lines)
                    total_cov += len(cov_lines)

        overall_pct = round(total_cov / total_exec * 100, 1) if total_exec else 100.0
        return {
            "modules": report,
            "total_executable": total_exec,
            "total_covered": total_cov,
            "overall_percent": overall_pct,
        }


class Tee:
    def __init__(self, *files):
        self.files = files
    def write(self, obj):
        for f in self.files:
            f.write(obj)
            f.flush()
    def flush(self):
        for f in self.files:
            f.flush()


def run_all():
    log_file_path = os.path.join(PROJECT_ROOT, "tests", "test_run.log")
    log_file = open(log_file_path, "w", encoding="utf-8")
    original_stdout = sys.stdout
    sys.stdout = Tee(original_stdout, log_file)

    print("=" * 70)
    print("  CLINMIND-AI QUALITY ASSURANCE - FULL TEST & COVERAGE SUITE")
    print("=" * 70)

    tracer = SimpleCoverageTracer(os.path.join(PROJECT_ROOT, "app"))
    sys.settrace(tracer.trace_fn)

    start_time = time.time()
    results = {}
    te_tests = []
    passed_te = 0
    load_failed = False

    # 1. Run test_engines.py
    print("\n[PHASE 1] Executing tests/test_engines.py (Core Engines & Golden Paths)...")
    try:
        import tests.test_engines as te
        te_tests = [
            ("test_physiology_initial_and_natural_progression", te.test_physiology_initial_and_natural_progression),
            ("test_physiology_targeted_anti_hypertensive", te.test_physiology_targeted_anti_hypertensive),
            ("test_physiology_lethal_medication_rupture_countdown", te.test_physiology_lethal_medication_rupture_countdown),
            ("test_guardrail_physical_exam_routing", te.test_guardrail_physical_exam_routing),
            ("test_guardrail_lab_and_imaging_routing", te.test_guardrail_lab_and_imaging_routing),
            ("test_agent_patient_trust_evolution", te.test_agent_patient_trust_evolution),
            ("test_agent_nurse_order_and_critical_alarms", te.test_agent_nurse_order_and_critical_alarms),
            ("test_ged_evaluator_expert_pathway", te.test_ged_evaluator_expert_pathway),
            ("test_ged_evaluator_fatal_contraindication", te.test_ged_evaluator_fatal_contraindication),
            ("test_api_full_flow", te.test_api_full_flow),
        ]
        passed_te = 0
        for name, fn in te_tests:
            try:
                fn()
                results[f"engines::{name}"] = "PASS"
                passed_te += 1
                print(f"  + {name:<55} [PASS]")
            except Exception as e:
                results[f"engines::{name}"] = f"FAIL: {e}"
                print(f"  x {name:<55} [FAIL: {e}]")
    except Exception as e:
        print(f"Error loading test_engines: {e}")
        load_failed = True

    # 2. Run test_e2e_edge_cases.py
    print("\n[PHASE 2] Executing tests/test_e2e_edge_cases.py (E2E, Fuses & Cohorts)...")
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_e2e_edge_cases")
    runner = unittest.TextTestRunner(verbosity=1)
    e2e_res = runner.run(suite)

    # 3. Run test_static_assets.py
    print("\n[PHASE 3] Executing tests/test_static_assets.py (Frontend & Consistency)...")
    static_suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_static_assets")
    static_runner = unittest.TextTestRunner(verbosity=1)
    static_res = static_runner.run(static_suite)

    print("\n[PHASE 4] Executing tests/test_session_backend.py (Session & Persistence)...")
    session_suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_session_backend")
    session_res = unittest.TextTestRunner(verbosity=1).run(session_suite)

    print("\n[PHASE 5] Model configuration & v2 startup regressions...")
    v2_suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_v2_regressions")
    v2_res = unittest.TextTestRunner(verbosity=1).run(v2_suite)

    print("\n[PHASE 6] Executing tests/test_emotion_tick.py (Emotion Engine & Settlement)...")
    emotion_suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_emotion_tick")
    emotion_res = unittest.TextTestRunner(verbosity=1).run(emotion_suite)

    print("\n[PHASE 7] Executing tests/test_api_emotion.py (API Emotion Channels)...")
    api_emotion_suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_api_emotion")
    api_emotion_res = unittest.TextTestRunner(verbosity=1).run(api_emotion_suite)

    sys.settrace(None)
    elapsed = time.time() - start_time

    # Coverage summary
    cov_data = tracer.generate_report()

    print("\n" + "=" * 70)
    print("  AUTOMATION TEST RESULTS SUMMARY")
    print("=" * 70)
    suites = [e2e_res, static_res, session_res, v2_res, emotion_res, api_emotion_res]
    total_run = len(te_tests) + sum(r.testsRun for r in suites)
    total_passed = passed_te + sum(r.testsRun - len(r.failures) - len(r.errors) for r in suites)
    total_failed = len(te_tests) - passed_te + int(load_failed) + sum(len(r.failures) + len(r.errors) for r in suites)

    print(f"Total Tests Executed : {total_run}")
    print(f"Total Passed         : {total_passed} (100.0%)" if total_failed == 0 else f"Total Passed         : {total_passed}")
    print(f"Total Failed         : {total_failed}")
    print(f"Total Execution Time : {elapsed:.3f} s")

    print("\n" + "=" * 70)
    print(f"  STATEMENT COVERAGE REPORT (Overall: {cov_data['overall_percent']}%)")
    print("=" * 70)
    print(f"{'Module Path':<45} | {'Exec':<6} | {'Cov':<6} | {'Coverage':<8}")
    print("-" * 70)
    for mod, stat in sorted(cov_data["modules"].items()):
        print(f"{mod:<45} | {stat['executable']:<6} | {stat['covered']:<6} | {stat['percent']:>6.1f} %")
    print("-" * 70)
    print(f"{'TOTAL / APP AVERAGE':<45} | {cov_data['total_executable']:<6} | {cov_data['total_covered']:<6} | {cov_data['overall_percent']:>6.1f} %")
    print("=" * 70)

    sys.stdout = original_stdout
    log_file.close()
    return total_failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
