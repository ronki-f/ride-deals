"""Daily price collector: direct TLV flights to airports near riding destinations.

Reads destinations.json, asks the Travelpayouts (Aviasales) cached-price API for
direct round trips, keeps only trips that fall inside a destination's open
window, rates each price against history, and writes data/deals.json.
"""
import csv, datetime as dt, json, os, statistics, sys, time
import urllib.error, urllib.parse, urllib.request

ORIGIN = "TLV"
API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
SITE = "https://www.aviasales.com"
TOKEN = os.environ.get("TP_TOKEN", "").strip()
TIERS = [t.strip() for t in os.environ.get("TIERS", "Start batch").split(",")]
MONTHS_AHEAD = int(os.environ.get("MONTHS_AHEAD", "12"))
TRIP_MIN = int(os.environ.get("TRIP_MIN", "3"))
TRIP_MAX = int(os.environ.get("TRIP_MAX", "14"))
PER_AIRPORT = int(os.environ.get("PER_AIRPORT", "25"))
MIN_HISTORY_DAYS = 7
HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "data", "history.csv")
OUT = os.path.join(HERE, "data", "deals.json")


class ApiError(Exception):
    pass


def fetch_offers(dest, departure_at=None, return_at=None):
    """One API call. Returns a list of offer dicts (may be empty)."""
    p = {"origin": ORIGIN, "destination": dest, "direct": "true",
         "currency": "usd", "limit": "50", "sorting": "price", "unique": "false"}
    if departure_at:
        p["departure_at"] = departure_at
        p["one_way"] = "false"
        p["return_at"] = return_at or departure_at
    url = API + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers={"X-Access-Token": TOKEN})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.load(r)
            if not body.get("success", True):
                raise ApiError(str(body)[:300])
            return body.get("data", [])
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ApiError(f"HTTP {e.code}: token rejected or no access")
            if e.code == 429 or e.code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            raise ApiError(f"HTTP {e.code} for {dest} {departure_at}")
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 * (attempt + 1))
    return []


def month_list(n):
    d = dt.date.today().replace(day=1)
    out = []
    for _ in range(n):
        out.append(d.strftime("%Y-%m"))
        d = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return out


def next_month(m):
    y, mo = map(int, m.split("-"))
    return f"{y + (mo == 12)}-{1 if mo == 12 else mo + 1:02d}"


def is_open(dest, d1, d2):
    """True if both trip dates fall inside the destination's open window."""
    if dest.get("open_date") and dest.get("close_date"):
        return dest["open_date"] <= d1 and d2 <= dest["close_date"]
    a, b = dest["open_month"], dest["close_month"]
    return all(a <= int(d[5:7]) <= b for d in (d1, d2))


def load_history():
    rows = []
    if os.path.exists(HIST):
        with open(HIST, newline="") as f:
            rows = list(csv.DictReader(f))
    return rows


def main():
    if not TOKEN:
        sys.exit("TP_TOKEN is not set (add it as a GitHub secret).")
    cfg = json.load(open(os.path.join(HERE, "destinations.json"), encoding="utf-8"))
    names = cfg["airports"]
    dests = [d for d in cfg["destinations"] if d["tier"] in TIERS]
    by_airport = {}
    for d in dests:
        for a in d["airports"]:
            by_airport.setdefault(a, []).append(d)
    today = dt.date.today().isoformat()
    months = month_list(MONTHS_AHEAD)

    raw = {}          # airport -> {offer key -> offer}
    no_direct = []
    for ap in sorted(by_airport):
        probe = fetch_offers(ap)
        time.sleep(0.3)
        if not probe:
            no_direct.append(ap)
            print(f"{ap}: no direct fares found")
            continue
        found = {}
        for m in months:
            for ret in (m, next_month(m)):
                for o in fetch_offers(ap, m, ret):
                    if o.get("transfers", 0) != 0 or o.get("return_transfers", 0) != 0:
                        continue
                    if not o.get("return_at"):
                        continue
                    d1, d2 = o["departure_at"][:10], o["return_at"][:10]
                    days = (dt.date.fromisoformat(d2) - dt.date.fromisoformat(d1)).days
                    if not TRIP_MIN <= days <= TRIP_MAX:
                        continue
                    found[(d1, d2, o.get("airline"), o.get("flight_number"))] = o
                time.sleep(0.25)
        raw[ap] = found
        print(f"{ap}: {len(found)} direct round trips")

    # history of daily cheapest price per airport + departure month
    hist = [r for r in load_history() if r["date"] != today]
    hist_by = {}
    for r in hist:
        hist_by.setdefault((r["airport"], r["depart_month"]), []).append(float(r["min_price"]))
    history_days = len({r["date"] for r in hist})

    new_rows, offers = [], []
    for ap, found in raw.items():
        per_month = {}
        for (d1, d2, _, _), o in found.items():
            per_month.setdefault(d1[:7], []).append(float(o["price"]))
        for m, prices in per_month.items():
            new_rows.append({"date": today, "airport": ap, "depart_month": m,
                             "min_price": min(prices), "n": len(prices)})
        rated = []
        for (d1, d2, _, _), o in found.items():
            m = d1[:7]
            open_d = [d for d in by_airport[ap] if is_open(d, d1, d2)]
            if not open_d:
                continue
            price = float(o["price"])
            h = hist_by.get((ap, m), [])
            if len(h) >= MIN_HISTORY_DAYS:
                base, basis = statistics.median(h), "history"
            elif len(per_month[m]) >= 4:
                base, basis = statistics.median(per_month[m]), "today"
            else:
                base, basis = None, "none"
            disc = round(1 - price / base, 3) if base else None
            link = o.get("link", "")
            rated.append({
                "airport": ap, "airport_name": names.get(ap, ap),
                "depart": d1, "return": d2, "price": price,
                "airline": o.get("airline"), "flight": o.get("flight_number"),
                "baseline": round(base) if base else None, "basis": basis,
                "discount": disc,
                "link": (SITE + link) if link.startswith("/") else link,
                "destinations": [{"name": d["name"], "country": d["country"],
                                  "type": d["type"], "transfer": d["transfer"],
                                  "notes": d["notes"]} for d in open_d],
            })
        rated.sort(key=lambda x: (-(x["discount"] or -1), x["price"]))
        offers.extend(rated[:PER_AIRPORT])

    offers.sort(key=lambda x: (-(x["discount"] or -1), x["price"]))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"generated": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
               "currency": "USD", "history_days": history_days + 1,
               "min_history_days": MIN_HISTORY_DAYS,
               "no_direct_airports": no_direct, "offers": offers},
              open(OUT, "w"), indent=1, ensure_ascii=False)
    with open(HIST, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "airport", "depart_month", "min_price", "n"])
        w.writeheader()
        w.writerows(hist + new_rows)
    print(f"Wrote {len(offers)} offers; history days: {history_days + 1}")


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        sys.exit(f"API error: {e}")
