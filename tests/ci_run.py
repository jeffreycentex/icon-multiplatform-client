"""CI test runner: each test module in its own process (a crash in one, e.g. Qt
or PortAudio on a headless runner, doesn't hide the others), with faulthandler
on, and every failure reported as a GitHub Actions annotation carrying the
tail of its output (annotations are readable through the API, raw logs may not
be). Exit code 1 if any module failed.

    python tests/ci_run.py
"""

import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TAIL = 60


def esc(text):
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def main():
    modules = sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(HERE, "test_*.py")))
    failed = []
    summary = []
    for m in modules:
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, "-X", "faulthandler", "-m", "unittest", "tests." + m, "-v"],
                               cwd=ROOT, capture_output=True, text=True, timeout=600)
            out, rc = (r.stdout or "") + (r.stderr or ""), r.returncode
        except subprocess.TimeoutExpired as e:
            out, rc = ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) \
                + "\nTIMEOUT after 600 s", -1
        dt = time.time() - t0
        print("=" * 70)
        print(out)
        last = [ln for ln in out.splitlines() if ln.startswith(("Ran ", "OK", "FAILED"))]
        status = "ok" if rc == 0 else "FAILED (exit %d)" % rc
        summary.append("%-16s %-18s %5.1fs  %s" % (m, status, dt, " | ".join(last[-2:])))
        if rc != 0:
            failed.append(m)
            interesting = [ln for ln in out.splitlines()
                           if ln.startswith(("FAIL:", "ERROR:", "Fatal Python", "Current thread", "Thread 0x",
                                             "  File", "AssertionError", "Traceback"))
                           or "Error" in ln or "Segmentation" in ln]
            tail = out.splitlines()[-TAIL:]
            body = "\n".join(interesting[:40]) + "\n----- last lines -----\n" + "\n".join(tail)
            if len(failed) <= 9:            # GitHub keeps 10 error annotations per step
                print("::error title=%s %s::%s" % (m, status, esc(body[-6000:])))
    text = "\n".join(summary)
    print("\n" + text)
    print("::notice title=test summary (%s)::%s" % (sys.platform, esc(text)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
