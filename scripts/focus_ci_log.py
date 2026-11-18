"""Focus a `gh run view --log` dump for the evidence folder (Submission Guide: "trim repeated log lines").

    gh run view <id> --log | python scripts/focus_ci_log.py --run-url <url> > evidence/ci/<name>.txt

Lines are kept verbatim. Dropped: runner setup/teardown steps, the package-install list, Docker
build layers and
the per-package rows of the Trivy table (its summary lines are kept). The full log stays at --run-url.
"""

from __future__ import annotations

import argparse
import re
import sys

DROP_STEPS = re.compile(
    r"^(Set up job|Initialize containers|Stop containers|Complete job|Post |Run actions/checkout"
    r"|Run astral-sh/setup-uv|Run azure/setup-helm|Run helm/kind-action)"
)
NOISE = {
    "Install pinned dev dependencies": re.compile(
        r"^\s*[+-] |Downloading|Downloaded|Prepared|Installed|Resolved"
    ),
    "Build churn-api:<sha>": re.compile(r"#\d+ (?!naming|exporting to image|DONE)"),
    "Vulnerability scan (report only)": re.compile(
        r"^(?!.*(Total:|Report Summary|churn-api|Detected OS|Number of))"
    ),
}


def keep(line: str) -> bool:
    parts = line.split("\t", 2)
    if len(parts) < 3:
        return True
    _, step, text = parts
    if DROP_STEPS.match(step):
        return False
    text = re.sub(r"^\S+Z ", "", text)  # timestamp
    noise = NOISE.get(step)
    return not (noise and noise.search(text))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    lines = sys.stdin.read().splitlines()
    kept = [line for line in lines if keep(line)]
    header = (
        f"# Focused extract of `gh run view --log` for {args.run_url}: {len(kept)} of {len(lines)} lines "
        "kept verbatim (setup steps, install lists, build layers and Trivy table rows dropped)."
    )
    sys.stdout.write(header + "\n" + "\n".join(kept) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
