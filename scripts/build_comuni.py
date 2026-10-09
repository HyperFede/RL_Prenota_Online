"""Regenerate rlprenota/geo/comuni_lombardia.csv from OpenStreetMap (Overpass API).

Data © OpenStreetMap contributors, available under the Open Database License (ODbL).
Usage: python scripts/build_comuni.py
"""
import csv
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
QUERY = """
[out:json][timeout:120];
area["ISO3166-2"="IT-25"]->.lombardia;
relation["boundary"="administrative"]["admin_level"="8"]["ref:ISTAT"](area.lombardia);
out center tags;
"""
# First three digits of the ISTAT code -> province label used by the booking portal
PROVINCES = {
    "016": "BERGAMO", "017": "BRESCIA", "013": "COMO", "019": "CREMONA", "097": "LECCO", "098": "LODI",
    "020": "MANTOVA", "015": "MILANO PROVINCIA", "108": "MONZA E DELLA BRIANZA", "018": "PAVIA",
    "014": "SONDRIO", "012": "VARESE",
}
MILANO_CITTA_ISTAT = "015146"
OUT = os.path.join(os.path.dirname(__file__), "..", "rlprenota", "geo", "comuni_lombardia.csv")


def main():
    data = urllib.parse.urlencode({"data": QUERY}).encode()
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    elements = None
    for url in OVERPASS_MIRRORS:
        request = urllib.request.Request(url, data=data, headers={"User-Agent": "rl-prenota-online/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=180, context=context) as response:
                elements = json.load(response)["elements"]
            break
        except (OSError, ValueError, KeyError) as e:
            print(f"{url}: {e}", file=sys.stderr)
    if elements is None:
        sys.exit("No Overpass server answered, try again later")

    rows = {}
    for element in elements:
        tags, center = element.get("tags", {}), element.get("center")
        istat = tags.get("ref:ISTAT", "").strip()
        if not center or len(istat) != 6 or istat[:3] not in PROVINCES:
            continue
        province = "MILANO CITTA'" if istat == MILANO_CITTA_ISTAT else PROVINCES[istat[:3]]
        rows[istat] = (istat, tags.get("name", "").strip(), province, round(center["lat"], 5), round(center["lon"], 5))

    if len(rows) < 1400:
        sys.exit(f"Only {len(rows)} comuni received, refusing to overwrite the table")
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["istat", "nome", "provincia", "lat", "lon"])
        writer.writerows(sorted(rows.values()))
    print(f"Wrote {len(rows)} comuni to {os.path.normpath(OUT)}")


if __name__ == "__main__":
    main()
