#!/usr/bin/env python3
"""Fetch the feeds in feeds.txt into a folder the app reads from the data branch.

usage: fetch_feeds.py feeds.txt prev_dir out_dir
Writes out_dir/index.json and out_dir/f/<id>.xml (UTF-8). A feed that fails
keeps its last good copy from prev_dir.
"""
import hashlib, html, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"


def key(u):
    u = u.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, text/html;q=0.8, */*;q=0.5"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.geturl(), r.read(8_000_000)


def alternate(base, body):
    page = body.decode("utf-8", "replace")
    for m in re.finditer(r"<link\b[^>]*>", page, re.I):
        tag = m.group(0)
        if re.search(r"rel=[\"']?[^\"'>]*alternate", tag, re.I) and re.search(r"application/(rss|atom)\+xml", tag, re.I):
            h = re.search(r"href=[\"']([^\"']+)", tag, re.I)
            if h:
                return urllib.parse.urljoin(base, html.unescape(h.group(1)))
    return None


def as_utf8(body):
    m = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([A-Za-z0-9._-]+)[\"']", body)
    enc = m.group(1).decode().lower() if m else "utf-8"
    if enc in ("utf-8", "utf8"):
        return body.lstrip(b"\xef\xbb\xbf")
    text = body.decode(enc, "replace")
    text = re.sub(r"(<\?xml[^>]*encoding=[\"'])[A-Za-z0-9._-]+", r"\1UTF-8", text, count=1)
    return text.encode("utf-8")


def fetch(url):
    final, body = get(url)
    head = body[:1000].lstrip().lower()
    if head.startswith(b"<!doctype html") or b"<html" in head[:300]:
        alt = alternate(final, body)
        if not alt:
            raise ValueError("no feed on that page")
        final, body = get(alt)
    body = as_utf8(body)
    root = ET.fromstring(body)
    kind = root.tag.rsplit("}", 1)[-1].lower()
    if kind not in ("rss", "feed", "rdf"):
        raise ValueError("not a feed")
    title = ""
    for c in root.iter():
        if c.tag.rsplit("}", 1)[-1].lower() == "title":
            title = (c.text or "").strip()
            break
    return final, body, title


def main():
    src, prev, out = sys.argv[1:4]
    os.makedirs(os.path.join(out, "f"), exist_ok=True)
    try:
        with open(os.path.join(prev, "index.json")) as fh:
            old = json.load(fh).get("feeds", {})
    except Exception:
        old = {}
    feeds, order, now = {}, [], int(time.time() * 1000)
    for line in open(src, encoding="utf-8"):
        names = line.split("#", 1)[0].split()
        if not names:
            continue
        url = names[0]
        fid = hashlib.sha1(key(url).encode()).hexdigest()[:12]
        path = "f/%s.xml" % fid
        entry = {"url": url, "file": path}
        try:
            final, body, title = fetch(url)
            with open(os.path.join(out, path), "wb") as fh:
                fh.write(body)
            entry.update(final=final, title=html.unescape(title), fetchedAt=now, ok=True)
            print("OK  ", url, len(body))
        except Exception as e:
            was = old.get(key(url)) or {}
            kept = os.path.join(prev, path)
            if os.path.exists(kept):
                with open(kept, "rb") as a, open(os.path.join(out, path), "wb") as b:
                    b.write(a.read())
                entry.update(final=was.get("final", url), title=was.get("title", ""), fetchedAt=was.get("fetchedAt", 0))
            else:
                entry["file"] = ""
            entry.update(ok=False, error=str(e)[:200])
            print("FAIL", url, e)
        order.append({"url": url, "title": entry.get("title", "")})
        for n in names + [entry.get("final") or url]:
            feeds[key(n)] = entry
    with open(os.path.join(out, "index.json"), "w") as fh:
        json.dump({"updated": now, "list": order, "feeds": feeds}, fh, indent=1)


if __name__ == "__main__":
    main()
