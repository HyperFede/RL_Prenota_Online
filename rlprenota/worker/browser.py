"""Headless Chromium for the workers: one long-lived browser per worker, cleaned between runs.

Images, fonts and third-party scripts the search never needs are blocked (faster pages, less RAM).
The browser is recycled every RECYCLE_RUNS runs, when it uses more than MAX_RSS_MB, or after any crash.
"""
import logging
import os
import threading

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.service import Service

log = logging.getLogger(__name__)

RECYCLE_RUNS = 50
MAX_RSS_MB = 450
BLOCKED_URLS = ["*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico", "*.woff", "*.woff2", "*.ttf",
                "*maps.googleapis.com*", "*maps.gstatic.com*", "*rilevazioni.js*", "*google-analytics*", "*googletagmanager*"]


def chromium_options(headless=True):
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    for arg in ("--window-size=1400,1000", "--lang=it-IT", "--disable-gpu", "--disable-extensions",
                "--disable-background-networking", "--disable-sync", "--no-first-run", "--mute-audio",
                "--disable-features=Translate,MediaRouter,OptimizationHints", "--blink-settings=imagesEnabled=false"):
        options.add_argument(arg)
    if os.environ.get("RLP_CHROMIUM_NO_SANDBOX") == "1":
        # Only if the seccomp profile can't be used: the container is the remaining isolation
        options.add_argument("--no-sandbox")
    binary = os.environ.get("RLP_CHROMIUM_BINARY")
    if binary:
        options.binary_location = binary
    options.page_load_strategy = "eager"
    return options


def make_chromium(headless=True):
    driver_path = os.environ.get("RLP_CHROMEDRIVER")
    service = Service(executable_path=driver_path) if driver_path else Service()
    driver = webdriver.Chrome(options=chromium_options(headless), service=service)
    driver.set_page_load_timeout(60)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": BLOCKED_URLS})
    except WebDriverException as e:
        log.warning(f"Blocco delle risorse non attivo: {e}")
    return driver


def process_tree_rss_mb(pid):
    """Resident memory of a process and its children (Linux /proc only; None elsewhere)."""
    if not os.path.isdir("/proc"):
        return None
    total, stack, seen = 0, [pid], set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            with open(f"/proc/{current}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1])
            for task in os.listdir(f"/proc/{current}/task"):
                with open(f"/proc/{current}/task/{task}/children") as f:
                    stack.extend(int(c) for c in f.read().split())
        except (OSError, ValueError):
            continue
    return total / 1024


class BrowserManager:
    def __init__(self, factory=make_chromium):
        self.factory = factory
        self.driver = None
        self.runs = 0
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if self.driver is not None and self._needs_recycle():
                self._quit()
            if self.driver is None:
                self.driver = self.factory()
                self.runs = 0
            self.runs += 1
            return self.driver

    def _needs_recycle(self):
        if self.runs >= RECYCLE_RUNS:
            return True
        try:
            pid = self.driver.service.process.pid
        except AttributeError:
            return False
        rss = process_tree_rss_mb(pid)
        return rss is not None and rss > MAX_RSS_MB

    def reset(self):
        """Forget everything about the last user before the next run."""
        with self._lock:
            if self.driver is None:
                return
            try:
                self.driver.delete_all_cookies()
                self.driver.execute_cdp_cmd("Storage.clearDataForOrigin",
                                            {"origin": "https://prenotasalute.regione.lombardia.it", "storageTypes": "all"})
                self.driver.get("about:blank")
            except WebDriverException:
                self._quit()

    def kill(self):
        """Watchdog: stop a stuck browser (the run then fails with a transient error)."""
        with self._lock:
            self._quit()

    def _quit(self):
        driver, self.driver = self.driver, None
        if driver is None:
            return
        # Best effort on a browser that may already be broken: quit, then make sure the process is gone
        try:
            driver.quit()
        except Exception:  # nosec B110
            pass
        try:
            driver.service.process.kill()
        except Exception:  # nosec B110
            pass
