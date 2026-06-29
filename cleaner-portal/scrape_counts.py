"""Print the live dashboard counts as a JSON line (read-only). Run as a SUBPROCESS by
the Cleaner Portal GUI — Playwright's sync API must run in a process's main thread, so
the GUI can't call scrape() in a worker thread (it hangs). The load progress prints to
stdout and the final line is 'COUNTS {json}'."""
import json

import status_app

if __name__ == "__main__":
    result = status_app.scrape()
    print("COUNTS " + json.dumps(result), flush=True)
