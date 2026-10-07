"""One-off diagnostic: what does the real Claude Code editor show right after a bracketed paste?
Reuses the acceptance runner's disposable setup/teardown (owned surface, trust dialog handling)."""
import json, sys, time, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_acceptance as la
from cmux_bridge.core import classify_screen

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
a = types.SimpleNamespace(workspace=sys.argv[2], scratch=sys.argv[3], out=str(out), model="sonnet", state_dir=str(out / "state"))
r = la.Runner(a)
r.preflight(); r.setup()
surf = r.surf
def rows(n=40):
    res = r.cm("read-screen", "--surface", surf, "--lines", str(n))
    return res.stdout.split("\n") if res.ok else [f"<{res.error_kind}>"]
def dump(tag, n=40):
    rs = rows(n)
    (out / f"{tag}.txt").write_text("\n".join(rs))
    (out / f"{tag}.repr.txt").write_text("\n".join(repr(l) for l in rs))
    print(tag, classify_screen(rs))
    for l in [x for x in rs if x.strip()][-6:]: print("   ", repr(l)[:150])
dump("0-idle")
text = "line one: 'single' \"double\" `backtick` $HOME ${X}\nline two: ✓ 日本語 🚀 \"$(id)\" && ; | > <\nline three: tab\there and trailing spaces   \n<<<END>>>"
r.cm("send", "--surface", surf, "--", "\x1b[200~" + text + "\x1b[201~")
for t in (0.5, 1.5, 3.0):
    time.sleep(t); dump(f"1-after-paste-{t}s")
r.cm("send-key", "--surface", surf, "ctrl+c"); time.sleep(1.0); dump("2-after-ctrlc")
r.cm("send", "--surface", surf, "--", "single line staged text"); time.sleep(1.0); dump("3-single-line")
r.cm("send-key", "--surface", surf, "ctrl+c"); time.sleep(1.0); dump("4-after-ctrlc-2")
r.teardown()
print("owned", json.load(open(out / "owned-surfaces.json")) if (out / "owned-surfaces.json").exists() else None)
