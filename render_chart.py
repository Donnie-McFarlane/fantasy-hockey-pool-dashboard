"""Render either dashboard from the shared template and its selected dataset."""

import argparse
import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
POOL_URL = "https://www.pickuphockey.com/hp/hp_viewstats.aspx"


def relabel(value, names):
    if isinstance(value, dict):
        return {names.get(key, key): relabel(item, names) for key, item in value.items()}
    if isinstance(value, list):
        return [relabel(item, names) for item in value]
    return names.get(value, value) if isinstance(value, str) else value


def live_names(stats):
    # The seed order is fixed by the scoring updater. These IDs also stay fixed
    # if display aliases are changed in a future snapshot.
    ids = [f"pool-{i:02}" for i in range(1, len(stats["contestants"]) + 1)]
    try:
        names = json.loads(os.environ["CONTESTANT_NAMES"])
        if set(names) != set(ids):
            raise ValueError
        labels = [names[key] for key in ids]
        if any(not isinstance(name, str) or not name.strip() for name in labels):
            raise ValueError
        reserved = {"date", "Median Pace"}
        if len(set(labels)) != len(labels) or reserved.intersection(labels):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SystemExit("CONTESTANT_NAMES must map all pool IDs to unique display names.") from None
    return {c["name"]: name for c, name in zip(stats["contestants"], labels, strict=True)}


def render(data, output, live):
    stats = json.loads(data.read_text(encoding="utf-8"))
    last = stats["history"][-1]
    assert stats["benchmarkPointsPerDay"] == 4.5
    assert len(stats["contestants"]) == 11 and len(stats["history"]) == stats["days"]
    assert not stats["discrepancies"] and last["date"] == stats["end"]
    assert all(c["actualPoints"] == last[c["name"]] == sum(p["nhlPoints"] for p in c["players"])
               for c in stats["contestants"])
    assert {r["owner"] for r in stats["playerBoard"]["rows"]} == {c["name"] for c in stats["contestants"]}
    stats["demo"] = not live
    if live:
        stats = relabel(stats, live_names(stats))

    template = (ROOT / "chart_template.html").read_text(encoding="utf-8")
    library = (ROOT / "echarts.min.js").read_text(encoding="utf-8")
    data_json = json.dumps(stats, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    link = f'href="{POOL_URL}" target="_blank" rel="noopener noreferrer"'
    replacements = {
        "__ECHARTS_LIB__": library.replace("</script", "<\\/script"),
        "__DATA_JSON__": data_json,
        "__POOL_SOURCE_LINK__": f'<a class="source-site-link" {link}>Official pool standings ↗</a>' if live else "",
        "__POOL_FOOTER_LINK__": f' Official pool link: <a {link}>PickupHockey.com</a>' if live else "",
    }
    for token, value in replacements.items():
        if template.count(token) != 1:
            raise SystemExit(f"Missing or duplicate template marker {token}")
        template = template.replace(token, value, 1)
    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(template, encoding="utf-8")
    shutil.copytree(ROOT / "assets", output / "assets", dirs_exist_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--data", type=Path, default=ROOT / "data/demo.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(args.data, args.output, args.live)
