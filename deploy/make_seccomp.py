"""Generate deploy/chrome-seccomp.json: Docker's default seccomp profile plus what Chromium's sandbox needs.

Chromium isolates each page in a sandbox built on user/PID namespaces. Docker's default profile only
allows creating namespaces with CAP_SYS_ADMIN, which our containers drop; instead of disabling the
sandbox (--no-sandbox), we allow exactly the namespace syscalls it uses.

Usage: python deploy/make_seccomp.py
"""
import json
import os
import ssl
import sys
import urllib.request

# Docker's default profile (moby/profiles); the generated file is committed, so changes show up in review
SOURCE = "https://raw.githubusercontent.com/moby/profiles/main/seccomp/default.json"
CHROME_SYSCALLS = ["clone", "clone3", "unshare", "setns", "chroot"]
OUT = os.path.join(os.path.dirname(__file__), "chrome-seccomp.json")


def main():
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    # SOURCE is a fixed https URL
    with urllib.request.urlopen(SOURCE, timeout=60, context=context) as response:  # nosec B310
        profile = json.load(response)
    if profile.get("defaultAction") != "SCMP_ACT_ERRNO" or not profile.get("syscalls"):
        sys.exit("Unexpected profile format")
    profile["syscalls"].append({"names": CHROME_SYSCALLS, "action": "SCMP_ACT_ALLOW",
                                "comment": "Chromium namespace sandbox (rl-prenota)"})
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=1)
    print(f"Wrote {OUT} ({len(profile['syscalls'])} rules)")


if __name__ == "__main__":
    main()
