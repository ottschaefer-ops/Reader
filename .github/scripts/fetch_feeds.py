#!/usr/bin/env python3
"""Fetch the feeds in feeds.txt into a folder the app reads from the data branch.

usage: fetch_feeds.py feeds.txt prev_dir out_dir
Writes out_dir/index.json and out_dir/f/<id>.xml (UTF-8). A feed that fails
keeps its last good copy from prev_dir.

Some sites hand out old copies of their feed. Each feed is asked for twice,
once with a changing query string, and the copy with the newest article wins;
a copy older than the last one kept is dropped. A line in feeds.txt can also
name a web page (page=https://...): headlines on it that the feed lacks are
added to the feed.
"""
import email.utils, hashlib, html, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"


def key(u):
    u = u.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
        "Cache-Control": "no-cache", "Pragma": "no-cache",
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


def dated_links(body):
    """{article link: time in ms} for the items of a feed."""
    out = {}
    try:
        root = ET.fromstring(body)
    except Exception:
        return out
    name = lambda e: e.tag.rsplit("}", 1)[-1].lower()
    for e in root.iter():
        if name(e) not in ("item", "entry"):
            continue
        link, ts = "", None
        for c in e:
            n = name(c)
            if n == "link" and not link:
                link = (c.get("href") or c.text or "").strip()
            elif n in ("pubdate", "date", "published", "updated") and ts is None:
                ts = parse_ts(c.text)
        if ts:
            out[link or "#%d" % len(out)] = ts
    return out


def newest(body):
    return max(dated_links(body).values(), default=0)


def fetch_one(url):
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


def fetch(url):
    """The feed as the site gives it, and again past the site's caches; the
    copy with the newest article wins."""
    got, err = [], None
    for u in (url, url + ("&" if "?" in url else "?") + "_r=%d" % (time.time() // 60)):
        try:
            final, body, title = fetch_one(u)
            got.append((newest(body), len(got), url if u != url and final == u else final, body, title))
        except Exception as e:
            err = err or e
    if not got:
        raise err
    best = max(got, key=lambda g: (g[0], -g[1]))
    return best[2], best[3], best[4]


def text_of(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def page_items(page_url, page, now):
    """Headlines on a news page: a heading holding a link, with a date
    (DD/MM/YYYY or YYYY-MM-DD) between it and the heading before or after."""
    host = key(page_url).split("/")[0]
    out, seen = [], set()
    heads = list(re.finditer(r"<h([1-4])\b[^>]*>(.*?)</h\1>", page, re.S | re.I))
    for i, m in enumerate(heads):
        # The date sits between this heading and the ones around it.
        lo = max(heads[i - 1].end() if i else 0, m.start() - 1500)
        hi = min(heads[i + 1].start() if i + 1 < len(heads) else len(page), m.end() + 600)
        a = re.search(r"<a\b[^>]*\bhref=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", m.group(2), re.S | re.I)
        if not a:
            continue
        link = urllib.parse.urljoin(page_url, html.unescape(a.group(1)))
        title = text_of(a.group(2))
        if not title or key(link).split("/")[0] != host or link in seen:
            continue
        pat = r"\b(\d{2})/(\d{2})/(\d{4})\b|\b(\d{4})-(\d{2})-(\d{2})\b"
        before = list(re.finditer(pat, page[lo:m.start()]))
        d = before[-1] if before else re.search(pat, page[m.end():hi])
        if not d:
            continue
        g = d.groups()
        try:
            day = datetime(int(g[2] or g[3]), int(g[1] or g[4]), int(g[0] or g[5]), 12, tzinfo=timezone.utc)
        except ValueError:
            continue
        p = re.search(r"<p\b[^>]*>(.*?)</p>", page[m.end():hi + 900 if hi == m.end() + 600 else hi], re.S | re.I)
        seen.add(link)
        out.append({"link": link, "title": title, "text": text_of(p.group(1)) if p else "",
                    "ts": min(int(day.timestamp() * 1000), now)})
    return out


def add_page(body, page_url, prev_body, now):
    """Add the page's headlines the feed lacks. One added before keeps its time."""
    if not re.search(rb"<rss\b", body[:2000]):
        return body, 0
    final, raw = get(page_url)
    have = {key(l) for l in dated_links(body)}
    before = {key(l): t for l, t in dated_links(prev_body).items()} if prev_body else {}
    esc = lambda t: html.escape(t, quote=False)
    add = []
    for it in page_items(final, raw.decode("utf-8", "replace"), now):
        if key(it["link"]) in have:
            continue
        ts = before.get(key(it["link"]), it["ts"])
        add.append((ts, "<item><title>%s</title><link>%s</link><description>%s</description><pubDate>%s</pubDate><guid>%s</guid></item>\n" % (
            esc(it["title"]), esc(it["link"]), esc(it["text"]), email.utils.formatdate(ts / 1000, usegmt=True), esc(it["link"]))))
    if not add:
        return body, 0
    block = "".join(x for _, x in sorted(add, reverse=True)).encode("utf-8")
    m = re.search(rb"<item[\s>]", body) or re.search(rb"</channel>", body)
    if not m:
        return body, 0
    merged = body[:m.start()] + block + body[m.start():]
    ET.fromstring(merged)
    return merged, len(add)


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
        line = line.split("#", 1)[0]
        line, _, label = line.partition("|")
        names, label = line.split(), label.strip()
        page = next((n[5:] for n in names if n.startswith("page=")), "")
        names = [n for n in names if not n.startswith("page=")]
        if not names:
            continue
        url = names[0]
        fid = hashlib.sha1(key(url).encode()).hexdigest()[:12]
        path = "f/%s.xml" % fid
        entry = {"url": url, "file": path}
        try:
            final, body, title = fetch(url)
            kept, last, note = os.path.join(prev, path), None, ""
            if os.path.exists(kept):
                with open(kept, "rb") as fh:
                    last = fh.read()
            if page:
                try:
                    body, n = add_page(body, page, last, now)
                    note = " +%d from the page" % n
                except Exception as e:
                    note = " (page failed: %s)" % str(e)[:80]
            if last and newest(last) > newest(body):
                body, note = last, note + " (site gave an older copy; kept the last one)"
            with open(os.path.join(out, path), "wb") as fh:
                fh.write(body)
            entry.update(final=final, title=html.unescape(title), fetchedAt=now, ok=True)
            print("OK  ", url, len(body), time.strftime("newest %d %b %H:%M", time.gmtime(newest(body) / 1000)) + note)
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
        order.append({"url": url, "title": label or entry.get("title", "")})
        for n in names + [entry.get("final") or url]:
            feeds[key(n)] = entry
    with open(os.path.join(out, "index.json"), "w") as fh:
        json.dump({"updated": now, "list": order, "feeds": feeds}, fh, indent=1)


if __name__ == "__main__":
    main()
