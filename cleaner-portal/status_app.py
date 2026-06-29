"""
Portal Status (Test) — a small, READ-ONLY GUI for the Tesla Dispatch Dashboard.

It attaches to the same dedicated Chrome over CDP, loads the board, and shows the
counts the cleanup cares about — Pickup Date Today/Late, ETA Today, Driver Needed,
and the total — WITHOUT changing anything. Safe to click anytime; nothing is submitted.

Run directly:  .venv/bin/python status_app.py   (or via "Portal Status.app")
"""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

# Self-contained env: attach over CDP to this folder's dedicated profile.
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("AUTH_MODE", "cdp")
os.environ.setdefault("CDP_PROFILE_DIR", os.path.join(HERE, ".auth"))


# --------------------------------------------------------------------------
# the scrape (runs in a worker thread; changes nothing)
# --------------------------------------------------------------------------
def scrape() -> dict:
    import config
    config.WINDOW_MODE = "ghost"          # off-screen
    config.HEADLESS = False
    from auth import browser_context
    import tesla_cleanup as tc

    with browser_context() as ctx:
        page = ctx.new_page()
        tc.load_dashboard(page)           # raises RuntimeError if signed out
        eta, pickup = tc.count_badges(page)
        drv = tc.count_driver_needed(page)
        loaded = page.locator(".grid-entry").count()
        total = tc._result_total(page) or loaded
        _, pickup_date = tc.compute_dates()
    return {
        "pickup": pickup, "eta": eta, "drv": drv,
        "loaded": loaded, "total": total, "pickup_date": pickup_date,
    }


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class StatusApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue" = queue.Queue()
        root.title("Portal Status (Test)")
        root.configure(bg="#1e1e1e")
        root.geometry("360x420")
        root.resizable(False, False)

        wrap = tk.Frame(root, bg="#1e1e1e")
        wrap.pack(fill="both", expand=True, padx=18, pady=16)

        tk.Label(wrap, text="Tesla Dispatch — live counts",
                 fg="#f0f0f0", bg="#1e1e1e",
                 font=("Helvetica", 15, "bold")).pack(anchor="w")
        tk.Label(wrap, text="read-only · nothing is submitted",
                 fg="#9aa0a6", bg="#1e1e1e",
                 font=("Helvetica", 11)).pack(anchor="w", pady=(0, 12))

        self.cards = {}
        for key, label, color in [
            ("pickup", "Pickup Date Today / Late", "#ffd166"),
            ("eta", "ETA Today", "#8ecae6"),
            ("drv", "Driver Needed", "#ff8fa3"),
            ("loaded", "Shipments loaded", "#cdeac0"),
        ]:
            self.cards[key] = self._card(wrap, label, color)

        self.bump = tk.Label(wrap, text="", fg="#9aa0a6", bg="#1e1e1e",
                             font=("Helvetica", 11), justify="left", wraplength=320)
        self.bump.pack(anchor="w", pady=(6, 0))

        self.status = tk.Label(wrap, text="Loading… (can take ~30–60s)",
                               fg="#9aa0a6", bg="#1e1e1e", font=("Helvetica", 11))
        self.status.pack(anchor="w", pady=(10, 8))

        btns = tk.Frame(wrap, bg="#1e1e1e")
        btns.pack(fill="x", side="bottom")
        self.refresh_btn = ttk.Button(btns, text="Refresh", command=self.refresh)
        self.refresh_btn.pack(side="left")
        ttk.Button(btns, text="Close", command=root.destroy).pack(side="right")

        root.lift()
        root.attributes("-topmost", True)
        root.after(600, lambda: root.attributes("-topmost", False))
        self.refresh()

    def _card(self, parent, label, color):
        f = tk.Frame(parent, bg="#2a2a2a")
        f.pack(fill="x", pady=4)
        tk.Label(f, text=label, fg="#c7c7c7", bg="#2a2a2a",
                 font=("Helvetica", 12)).pack(side="left", padx=12, pady=10)
        val = tk.Label(f, text="…", fg=color, bg="#2a2a2a",
                       font=("Helvetica", 20, "bold"))
        val.pack(side="right", padx=14)
        return val

    def refresh(self):
        self.refresh_btn.config(state="disabled")
        self.status.config(text="Loading… (can take ~30–60s)", fg="#9aa0a6")
        for v in self.cards.values():
            v.config(text="…")
        self.bump.config(text="")
        threading.Thread(target=self._work, daemon=True).start()
        self.root.after(150, self._poll)

    def _work(self):
        try:
            self.q.put(("ok", scrape()))
        except RuntimeError as e:
            kind = "login" if "logged out" in str(e).lower() else "error"
            self.q.put((kind, str(e)))
        except Exception as e:                       # noqa: BLE001
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def _poll(self):
        try:
            kind, payload = self.q.get_nowait()
        except queue.Empty:
            self.root.after(150, self._poll)
            return
        self.refresh_btn.config(state="normal")
        if kind == "ok":
            d = payload
            self.cards["pickup"].config(text=str(d["pickup"]))
            self.cards["eta"].config(text=str(d["eta"]))
            self.cards["drv"].config(text=str(d["drv"]))
            self.cards["loaded"].config(text=str(d["loaded"]))
            note = f"Pickups would move to {d['pickup_date']} (Other)."
            if d["total"] > d["loaded"]:
                note += (f"\nBoard has {d['total']} total; counts cover the "
                         f"{d['loaded']} loaded (cleanup handles the rest in passes).")
            self.bump.config(text=note)
            self.status.config(text="Updated " + time.strftime("%-I:%M:%S %p"),
                               fg="#9aa0a6")
        elif kind == "login":
            self.status.config(
                text="Signed out of Tesla — use Cleaner Portal / login-once to sign in.",
                fg="#ff8fa3")
        else:
            self.status.config(text=str(payload)[:90], fg="#ff8fa3")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:                                # noqa: BLE001
        pass
    StatusApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
