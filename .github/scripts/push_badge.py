#!/usr/bin/env python3
"""Update Reader's icon count on devices that turned on updates while closed.

usage: push_badge.py out_dir
Runs after fetch_feeds.py. Reads the sync gist (GIST_TOKEN): the push
subscriptions, and "seen", which is the unread count and the newest article of
each feed on the device that last had Reader open. Articles newer than that are
new; when there are more new ones than at the last push, each device gets the
count (unread then + new since) and a notification, sent by webpush.mjs.
The push key pair is made by Reader and kept in the gist (reader-push-key.json).
Nothing is sent without GIST_TOKEN, a repo secret.
"""
import email.utils, json, os, subprocess, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from fetch_feeds import fetch, key  # noqa: E402

SYNC_FILE, KEY_FILE, STATE_FILE = "reader-sync.json", "reader-push-key.json", "reader-push-state.json"
SUBJECT = "https://ottschaefer-ops.github.io/Reader/"


def gh(path, token, body=None, method=None):
    req = urllib.request.Request("https://api.github.com" + path, data=json.dumps(body).encode() if body else None,
        method=method or ("PATCH" if body else "GET"), headers={"Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json", "Content-Type": "application/json", "User-Agent": "reader-push"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def file_text(f, token):
    if not f:
        return ""
    if not f.get("truncated"):
        return f.get("content") or ""
    req = urllib.request.Request(f["raw_url"], headers={"Authorization": "Bearer " + token, "User-Agent": "reader-push"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def find_gist(token):
    for page in range(1, 11):
        lst = gh("/gists?per_page=100&page=%d" % page, token)
        for g in lst:
            if SYNC_FILE in (g.get("files") or {}):
                return g["id"]
        if len(lst) < 100:
            break
    return None


def parse_ts(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def link_key(link):
    # The app's linkKey: host without www, then the path, no trailing slash, lower case.
    try:
        u = urllib.parse.urlsplit(link)
        if not u.netloc:
            return ""
        return (u.netloc.lower().removeprefix("www.") + (u.path or "/")).rstrip("/").lower()
    except Exception:
        return ""


def items_of(body):
    root = ET.fromstring(body)
    name = lambda e: e.tag.rsplit("}", 1)[-1].lower()
    def kid(e, *names):
        for n in names:
            for c in e:
                if name(c) == n and (c.text or "").strip():
                    return c.text.strip()
        return ""
    out = []
    for e in root.iter():
        n = name(e)
        if n == "entry":
            link = ""
            for c in e:
                if name(c) == "link" and (c.get("rel") or "alternate") == "alternate":
                    link = c.get("href") or ""
                    break
            ts = parse_ts(kid(e, "published") or kid(e, "updated"))
        elif n == "item":
            link = kid(e, "link")
            guid = kid(e, "guid")
            if not link and guid.startswith("http"):
                link = guid
            ts = parse_ts(kid(e, "pubdate", "date", "published", "updated"))
        else:
            continue
        title = kid(e, "title")
        if ts and (link or title):
            out.append({"k": link_key(link) or title.lower(), "ts": ts, "title": title})
    return out


def js_hash(s):
    # The app's hash(): FNV-1a over the text, in base 36.
    h = 2166136261
    for ch in s:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    d, o = "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while h:
        h, r = divmod(h, 36)
        o = d[r] + o
    return o or "0"


def sync_check(out, token, files, data):
    """Temporary. Counts only (no article or account details): is the gist getting read marks?"""
    try:
        now = int(time.time() * 1000)
        marks = data.get("marks") or {}
        link = {k: v for k, v in marks.items() if k.startswith("l:")}
        ago = lambda t: round((now - abs(t)) / 60000) if t else None
        gists = sum(1 for page in range(1, 4) for g in gh("/gists?per_page=100&page=%d" % page, token) if SYNC_FILE in (g.get("files") or {}))
        cur = {}
        for name in os.listdir(os.path.join(out, "f")):
            for it in items_of(open(os.path.join(out, "f", name), "rb").read()):
                if "/" in it["k"]:
                    cur["l:" + js_hash(it["k"])] = it["ts"]
        def tally(days):
            ks = [k for k, t in cur.items() if now - t < days * 864e5]
            return {"articles": len(ks), "readByLink": sum(1 for k in ks if marks.get(k, 0) > 0), "unreadByLink": sum(1 for k in ks if marks.get(k, 0) < 0)}
        # Do the id-based marks match the ids the server's feed address gives?
        from fetch_feeds import dated_links
        byid = {}
        with open(os.path.join(out, "index.json")) as fh:
            entries = {e["file"]: e for e in json.load(fh)["feeds"].values()}
        for fname, e in entries.items():
            try:
                links = dated_links(open(os.path.join(out, fname), "rb").read())
            except Exception:
                continue
            base = e["url"]
            forms = {"asListed": base, "final": e.get("final") or base, "noSlash": base.rstrip("/"), "slash": base.rstrip("/") + "/",
                     "noWww": base.replace("://www.", "://"), "http": base.replace("https://", "http://")}
            for url, ts in links.items():
                if now - ts > 3 * 864e5 or not url.startswith("http"):
                    continue
                u = urllib.parse.urlsplit(url)
                canon = u.scheme + "://" + u.netloc.lower() + (u.path or "/")
                lk = "l:" + js_hash(link_key(url))
                hit = next((n for n, f in forms.items() if marks.get(js_hash(f + "|" + canon), 0) > 0), None)
                kind = ("link+" if marks.get(lk, 0) > 0 else "nolink+") + (hit or "noId")
                day = "day1" if now - ts < 864e5 else "day2to3"
                byid.setdefault(day, {}).setdefault(kind, 0)
                byid[day][kind] += 1
        seen = data.get("seen") or {}
        info = {"checkedAt": now, "byId": byid, "gists": gists, "bytes": (files.get(SYNC_FILE) or {}).get("size"),
                "marks": len(marks), "linkMarks": len(link), "unreadMarks": sum(1 for v in marks.values() if v < 0),
                "newestMarkMinAgo": ago(max((abs(v) for v in marks.values()), default=0)),
                "newestLinkMarkMinAgo": ago(max((abs(v) for v in link.values()), default=0)),
                "last1Day": tally(1), "last3Days": tally(3), "last20Days": tally(20),
                "lastOpenUnread": seen.get("n"), "lastOpenMinAgo": ago(seen.get("t")),
                "feeds": sum(1 for f in (data.get("feeds") or {}).values() if isinstance(f, dict) and f.get("ts", 0) > 0),
                "stars": sum(1 for v in (data.get("stars") or {}).values() if isinstance(v, dict) and v.get("t", 0) > 0),
                "devicesWithPush": len(data.get("subs") or {})}
        with open(os.path.join(out, "sync-check.json"), "w") as fh:
            json.dump(info, fh)
    except Exception as e:
        print("sync check failed:", str(e)[:120])


def main():
    out = sys.argv[1]
    token = os.environ.get("GIST_TOKEN", "").strip()
    if not token:
        print("push: no GIST_TOKEN secret, skipping")
        return
    gid = find_gist(token)
    if not gid:
        print("push: no sync gist yet")
        return
    files = gh("/gists/" + gid, token).get("files") or {}
    data = json.loads(file_text(files.get(SYNC_FILE), token) or "{}")
    try:
        state = json.loads(file_text(files.get(STATE_FILE), token) or "{}")
    except Exception:
        state = {}
    try:
        vapid = json.loads(file_text(files.get(KEY_FILE), token) or "{}").get("d", "")
    except Exception:
        vapid = ""
    sync_check(out, token, files, data)
    subs = [v["s"] for v in (data.get("subs") or {}).values()
            if isinstance(v, dict) and v.get("ts", 0) > 0 and isinstance(v.get("s"), dict) and v["s"].get("endpoint")]
    seen = data.get("seen") or {}
    at = seen.get("at") or {}
    if not subs or not at or not vapid:
        print("push: %d devices, nothing seen yet" % len(subs))
        return

    try:
        with open(os.path.join(out, "index.json")) as fh:
            index = json.load(fh).get("feeds", {})
    except Exception:
        index = {}
    known, fresh, now = set(), {}, int(time.time() * 1000)
    for k, f in (data.get("feeds") or {}).items():
        if not isinstance(f, dict) or f.get("ts", 0) <= 0 or not f.get("u") or k not in at:
            continue
        try:
            e = index.get(key(f["u"]))
            if e and e.get("file") and os.path.exists(os.path.join(out, e["file"])):
                body = open(os.path.join(out, e["file"]), "rb").read()
            else:
                body = fetch(f["u"])[1]
            got = items_of(body)
        except Exception as err:
            print("push: skipped a feed:", str(err)[:100])
            continue
        for it in got:
            if it["ts"] <= at[k]:
                known.add(it["k"])
            elif it["ts"] < now + 864e5:
                fresh.setdefault(it["k"], it)
    new = sorted((it for k, it in fresh.items() if k not in known), key=lambda i: -i["ts"])
    base = state.get("n", 0) if state.get("t") == seen.get("t") else 0
    print("push: %d new since last open, %d at last push, %d devices" % (len(new), base, len(subs)))
    if len(new) <= base:
        return

    count = int(seen.get("n") or 0) + len(new)
    title = "1 new article" if len(new) == 1 else "%d new articles" % len(new)
    payload = json.dumps({"badge": count, "title": title, "body": new[0]["title"][:180]})
    job = json.dumps({"subs": subs, "payload": payload, "subject": SUBJECT})
    subprocess.run(["node", os.path.join(os.path.dirname(__file__), "webpush.mjs")], input=job.encode(), check=False,
                   env={**os.environ, "VAPID_PRIVATE_KEY": vapid})
    gh("/gists/" + gid, token, {"files": {STATE_FILE: {"content": json.dumps({"t": seen.get("t"), "n": len(new)})}}})


if __name__ == "__main__":
    main()
