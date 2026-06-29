"""
Cleaner Portal — GUI front end for the Tesla dashboard cleanup.

Shows the live counts (like Portal Status) AND runs the cleanup with a streaming
progress log, a dry-run toggle, a Stop button, and a stall watchdog — so a long
apply never looks "stuck". The actual work is the calibrated tesla_cleanup.py, run
as a subprocess so its output streams into the window live.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("AUTH_MODE", "cdp")
os.environ.setdefault("CDP_PROFILE_DIR", os.path.join(HERE, ".auth"))

PY = os.path.join(HERE, ".venv", "bin", "python")
CLEANUP = os.path.join(HERE, "tesla_cleanup.py")
PREFLIGHT = os.path.join(HERE, "preflight_login.py")
SCRAPE = os.path.join(HERE, "scrape_counts.py")
STALL_SECONDS = 100          # warn if no output for this long during a run

BG = "#1e1e1e"; CARD = "#2a2a2a"; FG = "#f0f0f0"; MUTE = "#9aa0a6"
OK = "#cdeac0"; WARN = "#ffd166"; BAD = "#ff8fa3"; INFO = "#8ecae6"


def _child_env():
    e = dict(os.environ)
    e["PYTHONUNBUFFERED"] = "1"               # stream child stdout line-by-line
    return e


class CleanerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue" = queue.Queue()
        self.proc = None
        self.running = False
        self.busy = False               # a counts-refresh subprocess is in flight
        self.last_activity = 0.0

        root.title("Cleaner Portal")
        root.configure(bg=BG)
        root.geometry("520x560")
        root.minsize(480, 520)

        wrap = tk.Frame(root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=16, pady=14)

        tk.Label(wrap, text="Tesla Dispatch — cleanup", fg=FG, bg=BG,
                 font=("Helvetica", 15, "bold")).pack(anchor="w")
        tk.Label(wrap, text="bumps Pickup Today/Late to tomorrow · assigns drivers",
                 fg=MUTE, bg=BG, font=("Helvetica", 11)).pack(anchor="w", pady=(0, 10))

        # counts row
        cards = tk.Frame(wrap, bg=BG); cards.pack(fill="x")
        self.cards = {}
        for key, label, color in [("pickup", "Pickup", WARN), ("eta", "ETA", INFO),
                                   ("drv", "Driver Needed", BAD), ("loaded", "Loaded", OK)]:
            self.cards[key] = self._card(cards, label, color)

        # controls
        ctl = tk.Frame(wrap, bg=BG); ctl.pack(fill="x", pady=(12, 6))
        self.dry = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Dry run (preview, no changes)",
                        variable=self.dry).pack(side="left")
        self.refresh_btn = ttk.Button(ctl, text="Refresh counts", command=self.refresh)
        self.refresh_btn.pack(side="right")

        run_row = tk.Frame(wrap, bg=BG); run_row.pack(fill="x", pady=(0, 8))
        self.run_btn = ttk.Button(run_row, text="Run cleanup", command=self.run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(run_row, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        # live log
        tk.Label(wrap, text="Progress", fg=MUTE, bg=BG,
                 font=("Helvetica", 11)).pack(anchor="w")
        logf = tk.Frame(wrap, bg=CARD); logf.pack(fill="both", expand=True, pady=(2, 6))
        self.log = tk.Text(logf, bg="#141414", fg="#d6d6d6", insertbackground=FG,
                           font=("Menlo", 10), height=10, wrap="word", bd=0,
                           state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.log.pack(side="left", fill="both", expand=True)

        self.status = tk.Label(wrap, text="Idle.", fg=MUTE, bg=BG,
                               font=("Helvetica", 11), anchor="w", justify="left")
        self.status.pack(fill="x")

        root.lift(); root.attributes("-topmost", True)
        root.after(600, lambda: root.attributes("-topmost", False))
        self.root.after(150, self._poll)
        self.refresh()                          # show counts on open

    # ---- ui helpers ----
    def _card(self, parent, label, color):
        f = tk.Frame(parent, bg=CARD); f.pack(side="left", expand=True, fill="x", padx=3)
        tk.Label(f, text=label, fg=MUTE, bg=CARD, font=("Helvetica", 10)).pack(pady=(8, 0))
        v = tk.Label(f, text="…", fg=color, bg=CARD, font=("Helvetica", 19, "bold"))
        v.pack(pady=(0, 8)); return v

    def _logln(self, text):
        line = text.rstrip()
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        try:                                        # also persist for later inspection
            with open(os.path.join(HERE, "output", "gui.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:                           # noqa: BLE001
            pass

    def _set_status(self, text, color=MUTE):
        self.status.config(text=text, fg=color)

    # ---- counts refresh (read-only, via subprocess) ----
    # Playwright's sync API must run in a process's MAIN thread, so we can't scrape in a
    # GUI worker thread (it hangs). Run scrape_counts.py as a subprocess instead and read
    # its output; the worker thread here only reads a pipe, which is thread-safe.
    def refresh(self):
        if self.running or self.busy:
            return
        self.busy = True
        self.refresh_btn.config(state="disabled"); self.run_btn.config(state="disabled")
        for v in self.cards.values():
            v.config(text="…")
        self._set_status("Loading counts… (can take a minute on a big board)")
        threading.Thread(target=self._count_worker, daemon=True).start()

    def _count_worker(self):
        try:
            proc = subprocess.Popen([PY, SCRAPE], cwd=HERE, env=_child_env(),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
            for line in proc.stdout:
                self.q.put(("cline", line))
            self.q.put(("cdone", proc.wait()))
        except Exception as e:                  # noqa: BLE001
            self.q.put(("run_err", str(e)))

    # ---- run cleanup ----
    def run(self):
        if self.running or self.busy:
            return
        self.run_btn.config(state="disabled"); self.refresh_btn.config(state="disabled")
        self._set_status("Checking your Tesla session…", INFO)
        threading.Thread(target=self._preflight_worker, daemon=True).start()

    def _preflight_worker(self):
        try:
            code = subprocess.run([PY, PREFLIGHT], cwd=HERE, env=_child_env()).returncode
        except Exception as e:                  # noqa: BLE001
            self.q.put(("run_err", str(e))); return
        self.q.put(("preflight", code))

    def _start_apply(self):
        dry = self.dry.get()
        if not dry:
            pk = self.cards["pickup"].cget("text")
            n = pk if pk.isdigit() else "the"
            if not messagebox.askyesno("Run cleanup",
                    f"Bump {n} Pickup Today/Late shipment(s) to tomorrow on your LIVE "
                    "board (and assign drivers)?\n\nThis submits real changes."):
                self.run_btn.config(state="normal"); self.refresh_btn.config(state="normal")
                self._set_status("Cancelled.", MUTE); return
        self.running = True
        self.stop_btn.config(state="normal")
        self.last_activity = time.time()
        mode = "DRY RUN" if dry else "APPLY"
        self._logln(f"=== {mode} started ===")
        self._set_status(f"Running ({mode})… live progress below.", INFO)
        args = [PY, CLEANUP] + ([] if dry else ["--apply"])
        threading.Thread(target=self._apply_worker, args=(args,), daemon=True).start()

    def _apply_worker(self, args):
        try:
            self.proc = subprocess.Popen(args, cwd=HERE, env=_child_env(),
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1)
            for line in self.proc.stdout:
                self.q.put(("log", line))
            self.q.put(("exit", self.proc.wait()))
        except Exception as e:                  # noqa: BLE001
            self.q.put(("run_err", str(e)))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self._logln("=== stopping… ===")
            try:
                self.proc.terminate()
            except Exception:                   # noqa: BLE001
                pass

    # ---- event pump ----
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        # stall watchdog
        if self.running and (time.time() - self.last_activity) > STALL_SECONDS:
            secs = int(time.time() - self.last_activity)
            self._set_status(f"⚠ No progress for {secs}s — it may be stalled. "
                             "You can Stop and re-run (done work is saved).", WARN)
        self.root.after(200, self._poll)

    def _show_counts(self, d):
        self.cards["pickup"].config(text=str(d["pickup"]))
        self.cards["eta"].config(text=str(d["eta"]))
        self.cards["drv"].config(text=str(d["drv"]))
        self.cards["loaded"].config(text=str(d["loaded"]))
        extra = (f"  ·  board has {d['total']} (acts on {d['loaded']} loaded/pass)"
                 if d["total"] > d["loaded"] else "")
        self._set_status(f"Ready. {d['pickup']} pickups would move to "
                         f"{d['pickup_date']}.{extra}", MUTE)
        self._logln(f"counts: pickup={d['pickup']} eta={d['eta']} drv={d['drv']} "
                    f"loaded={d['loaded']} total={d['total']}")

    def _handle(self, kind, payload):
        if kind == "cline":                         # a line from the counts subprocess
            line = payload.rstrip()
            if line.startswith("COUNTS "):
                try:
                    self._got_counts = json.loads(line[len("COUNTS "):])
                except Exception:                   # noqa: BLE001
                    pass
            elif line:
                self._logln(line)
                if "logged out" in line.lower():
                    self._set_status("Signed out of Tesla — sign in (Cleaner Portal / "
                                     "login-once), then Refresh.", BAD)
                elif line.startswith("Pass ") or "page size" in line or "loaded" in line:
                    self._set_status(line, INFO)
        elif kind == "cdone":
            self.busy = False
            self.refresh_btn.config(state="normal")
            if not self.running:
                self.run_btn.config(state="normal")
            if getattr(self, "_got_counts", None):
                self._show_counts(self._got_counts)
                self._got_counts = None
            elif payload != 0:
                self._set_status("Couldn't load counts — see the progress log.", BAD)
        elif kind == "preflight":
            if payload == 10:
                self._set_status("Signed out of Tesla — a sign-in window was opened. "
                                 "Sign in there, then click Run again.", BAD)
                self.run_btn.config(state="normal"); self.refresh_btn.config(state="normal")
            elif payload == 0:
                self._start_apply()
            else:
                self._set_status(f"Couldn't reach Tesla (error {payload}). "
                                 "Check your connection and try again.", BAD)
                self.run_btn.config(state="normal"); self.refresh_btn.config(state="normal")
        elif kind == "log":
            self.last_activity = time.time()
            line = payload.rstrip()
            if line:
                self._logln(line)
            low = line.lower()
            if "pickup: bumped" in low or line.startswith("Pass "):
                self._set_status(line, INFO)
            elif "logged out" in low:
                self._set_status("Tesla signed you out mid-run. Sign in and Run again.", BAD)
        elif kind == "exit":
            self.running = False
            self.proc = None
            self.stop_btn.config(state="disabled")
            self.run_btn.config(state="normal"); self.refresh_btn.config(state="normal")
            if payload == 0:
                self._logln("=== finished ===")
                self._set_status("Done. Refresh to see the updated board.", OK)
                self.refresh()
            else:
                self._logln(f"=== ended (exit {payload}) ===")
                self._set_status(f"Stopped/failed (exit {payload}). Done work is saved; "
                                 "re-run to continue.", WARN)
        elif kind == "run_err":
            self.running = False; self.proc = None
            self.stop_btn.config(state="disabled")
            self.run_btn.config(state="normal"); self.refresh_btn.config(state="normal")
            self._set_status(f"Error: {str(payload)[:90]}", BAD)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:                           # noqa: BLE001
        pass
    CleanerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
