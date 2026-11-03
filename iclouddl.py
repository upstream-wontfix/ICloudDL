#!/usr/bin/env python3
"""ICloudDL - Download photos and videos from an iCloud share for those without an Apple device.

Unofficial, AI-assisted, provided as-is. Only downloads shared albums your account
already has access to, using session cookies you export yourself.
"""
import argparse
import base64
import concurrent.futures as cf
import datetime as dt
import errno
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

try:
    import requests
except ImportError:  # handled with a friendly message in main()
    requests = None

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
CT_TEXT = {"Content-Type": "text/plain;charset=UTF-8"}
PAGE = 100           # assets fetched per request (URLs are short-lived, so we download each page promptly)
NET_TRIES = 5        # retries for any single network call
INDEX_NAME = ".iclouddl-index.json"

# resolution fields in the shared-stream records, best first
RES_PREF = ["resOriginalRes", "resVidMedRes", "resJPEGMedRes", "resVidSmallRes"]

_print_lock = threading.Lock()
VERBOSE = False
QUIET = False
_warned = set()  # one-time warnings, keyed by tag


class ICloudDLError(Exception):
    """A clean, expected failure — printed as a message, not a traceback."""


class _AuthError(ICloudDLError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"auth failed: HTTP {code}")


class _FatalIO(ICloudDLError):
    """Unrecoverable local I/O problem (e.g. disk full) — don't retry."""


def warn_once(tag, *a):
    if tag not in _warned:
        _warned.add(tag)
        warn(*a)


# ----------------------------------------------------------------------------- logging
def _c(code, s):
    return s if not sys.stdout.isatty() else f"\033[{code}m{s}\033[0m"

def info(*a):
    if not QUIET:
        with _print_lock:
            print(*a, flush=True)

def good(*a):
    if not QUIET:
        with _print_lock:
            print(_c("32", "✓"), *a, flush=True)

def warn(*a):
    with _print_lock:
        print(_c("33", "⚠"), *a, file=sys.stderr, flush=True)

def err(*a):
    with _print_lock:
        print(_c("31", "✗"), *a, file=sys.stderr, flush=True)

def dbg(*a):
    if VERBOSE and not QUIET:
        with _print_lock:
            print(_c("90", "  ·"), *a, flush=True)


# ----------------------------------------------------------------------------- helpers
def retry(fn, desc, tries=NET_TRIES):
    """Run fn() with exponential backoff; raise only after exhausting retries.

    Fatal errors (stale cookies, disk full) are re-raised immediately — retrying
    them just wastes time and buries the real message.
    """
    last = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except ICloudDLError:
            raise  # not transient; surface it now
        except Exception as e:  # noqa: BLE001 - the network layer raises many types
            last = e
            if attempt < tries:
                wait = min(30, 2 ** attempt)
                dbg(f"{desc}: attempt {attempt}/{tries} failed ({type(e).__name__}: {e}); "
                    f"retry in {wait}s")
                time.sleep(wait)
            else:
                dbg(f"{desc}: giving up after {tries} attempts ({type(e).__name__}: {e})")
    raise last


def sanitize(name, fallback="album"):
    name = (name or "").strip()
    name = re.sub(r"[/\\\x00-\x1f]", "_", name)
    name = re.sub(r'[:*?"<>|]', "_", name).rstrip(". ")  # also unsafe on Windows/exFAT drives
    # keep within the common 255-byte filename limit (emoji are multi-byte)
    while len(name.encode("utf-8", "ignore")) > 200 and len(name) > 1:
        name = name[:-1]
    return name or fallback


def b64txt(enc):
    try:
        return base64.b64decode(enc).decode("utf-8", "replace")
    except Exception:
        return None


def fval(fields, key):
    f = fields.get(key)
    return f.get("value") if isinstance(f, dict) else None


def ts_iso(ms):
    if ms is None:
        return None
    try:
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()
    except Exception:
        return None


def scalar_fields(fields):
    """Server-side record fields, minus the ASSETID blobs (which hold ephemeral URLs)."""
    out = {}
    for k, v in fields.items():
        if not isinstance(v, dict):
            continue
        t, val = v.get("type"), v.get("value")
        if t == "ASSETID":
            continue
        if t == "BYTES":
            out[k] = b64txt(val)
        elif t == "TIMESTAMP":
            out[k] = {"epoch_ms": val, "iso": ts_iso(val)}
        else:
            out[k] = val
    return out


# ----------------------------------------------------------------------------- session / API
def load_session(cookie_path):
    if not cookie_path:
        raise ICloudDLError(
            "no cookie file given. Pass -cf/--cookies PATH, or set $ICLOUDDL_COOKIES, "
            "pointing at cookies exported from www.icloud.com while signed in.")
    if not os.path.exists(cookie_path):
        raise ICloudDLError(f"cookie file not found: {cookie_path}")
    try:
        with open(cookie_path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as e:
        raise ICloudDLError(
            f"cookie file is not valid JSON ({e}). Export it with a 'cookies.json' browser "
            "extension (not the Netscape cookies.txt format).")
    except OSError as e:
        raise ICloudDLError(f"could not read cookie file: {e}")

    # Accept both a bare list and the common {\"cookies\": [...]} wrapper.
    if isinstance(raw, dict):
        raw = raw.get("cookies") or raw.get("Cookies") or []
    if not isinstance(raw, list) or not raw:
        raise ICloudDLError("cookie file contains no cookies (expected a JSON array of cookie "
                            "objects with 'name' and 'value').")

    s = requests.Session()
    loaded = 0
    for c in raw:
        if not isinstance(c, dict):
            continue
        nm, val = c.get("name"), c.get("value")
        if not nm or val is None:
            continue
        dom = c.get("domain") or ".icloud.com"
        try:
            s.cookies.set(nm, val, domain=dom, path=c.get("path", "/"))
            loaded += 1
        except Exception as e:  # noqa: BLE001 - one bad cookie shouldn't sink the rest
            dbg(f"skipped cookie {nm!r}: {e}")
    if loaded == 0:
        raise ICloudDLError("no usable cookies found in the file.")
    if not any(n.startswith(("X-APPLE-WEBAUTH", "X-APPLE-DS-WEB")) for n in s.cookies.keys()):
        warn("cookie file has no X-APPLE-WEBAUTH* tokens — this may not be an iCloud session "
             "export; authentication will probably fail.")
    dbg(f"loaded {loaded} cookies from {cookie_path}")
    s.headers.update({"Origin": "https://www.icloud.com", "Referer": "https://www.icloud.com/",
                      "User-Agent": UA})
    return s


def authenticate(cookie_path):
    s = load_session(cookie_path)

    def _val():
        try:
            r = s.post("https://setup.icloud.com/setup/ws/1/validate",
                       headers={"Content-Type": "application/json"}, data="{}", timeout=30)
        except requests.exceptions.RequestException as e:
            # Let retry() handle transient network blips; it re-raises after N tries.
            raise ConnectionError(f"could not reach setup.icloud.com: {e}") from e
        # Auth failures are not worth retrying: the cookies are stale, full stop.
        if r.status_code in (401, 403, 421):
            raise _AuthError(r.status_code)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError as e:
            raise ICloudDLError(f"validate returned non-JSON (HTTP {r.status_code}): {e}")

    try:
        data = retry(_val, "validate")
    except _AuthError:
        raise  # main() prints the re-export-cookies guidance
    except ConnectionError as e:
        raise ICloudDLError(f"{e}\nCheck your internet connection and try again.")

    ds = (data or {}).get("dsInfo") or {}
    if not isinstance(data, dict) or data.get("success") is False or not ds:
        raise _AuthError(data.get("error") if isinstance(data, dict) else "unknown")
    dsid = ds.get("dsid")
    ss = (data.get("webservices", {}).get("sharedstreams") or {}).get("url")
    if not dsid or not ss:
        raise ICloudDLError(
            "signed in, but this account has no shared-streams service "
            "(is iCloud Photos / Shared Albums enabled for it?).")
    good(f"authenticated as {ds.get('appleId')}  (dsid {dsid})")
    return s, str(dsid), ss


def _post_json(s, url, body):
    """POST text/plain JSON, following a single X-Apple-MMe-Host partition hint, with retries."""
    def _do():
        r = s.post(url, headers=CT_TEXT, data=json.dumps(body), timeout=180)
        if r.status_code == 330 and r.headers.get("x-apple-mme-host"):
            host = r.headers["x-apple-mme-host"]
            u2 = re.sub(r"https://[^/]+", f"https://{host}", url)
            dbg(f"following partition hint -> {host}")
            r = s.post(u2, headers=CT_TEXT, data=json.dumps(body), timeout=180)
        if r.status_code in (401, 403, 421):
            raise _AuthError(r.status_code)  # session expired mid-run
        r.raise_for_status()
        return r
    r = retry(_do, f"POST {url.rsplit('/', 1)[-1]}")
    if not r.text.strip():
        return {}
    try:
        return r.json()
    except ValueError as e:
        raise ICloudDLError(f"unexpected non-JSON response from {url.rsplit('/', 1)[-1]}: {e}")


def list_albums(s, dsid, ss):
    data = _post_json(s, f"{ss}/{dsid}/sharedstreams/webgetalbumslist", {})
    albums = []
    for a in data.get("albums", []):
        albums.append({
            "name": (a.get("attributes") or {}).get("name"),
            "guid": a.get("albumguid"),
            "location": a.get("albumlocation"),
            "ownerdsid": a.get("ownerdsid"),
            "sharingtype": a.get("sharingtype"),
            "attributes": a.get("attributes") or {},
        })
    return albums


def album_count(s, album):
    """Asset count for display only — never fatal; returns None if the summary call fails."""
    try:
        data = _post_json(s, album["location"] + "albumsummary", {"albumguid": album["guid"]})
        return len(data.get("assets", []))
    except _AuthError:
        raise
    except Exception as e:  # noqa: BLE001
        dbg(f"albumsummary failed for {album.get('name')!r}: {e}")
        return None


def fetch_page(s, album, offset, page=PAGE):
    # NB: the API's "limit" is the END rank (exclusive), not a count.
    return _post_json(s, album["location"] + "webgetassets",
                      {"albumguid": album["guid"], "offset": str(offset),
                       "limit": str(offset + page)}).get("records", [])


# ----------------------------------------------------------------------------- download / convert
def pick_resource(fields):
    for key in RES_PREF:
        f = fields.get(key)
        if isinstance(f, dict) and isinstance(f.get("value"), dict) and f["value"].get("downloadURL"):
            stem = key[3:-3]
            return key, f["value"]["downloadURL"], fval(fields, f"res{stem}FileType")
    return None, None, None


def sniff_type(path):
    """Return (correct_ext, needs_transcode) from magic bytes."""
    try:
        head = open(path, "rb").read(32)
    except Exception:
        return None, False
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg", False
    if head[:4] == b"\x89PNG":
        return ".png", False
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif", False
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"heif", b"mif1", b"hevc", b"hevx", b"msf1"):
            return ".heic", True
        if brand == b"qt  ":
            return ".mov", False
        return ".mp4", False
    return None, False


def transcode_heic(src, dst):
    """Convert a real HEIC to JPEG, preserving EXIF. Returns True on success."""
    try:
        import pillow_heif  # noqa
        from PIL import Image
    except ImportError:
        warn_once("heic-deps",
                  "found a real HEIC file but pillow-heif is not installed; keeping it as .heic. "
                  "Install extras to transcode:  pip install pillow pillow-heif")
        return False
    try:
        pillow_heif.register_heif_opener()
        im = Image.open(src)
        exif = im.info.get("exif")
        im.convert("RGB").save(dst, "JPEG", quality=95, exif=exif if exif else b"")
        return True
    except Exception as e:  # noqa: BLE001
        warn(f"HEIC transcode failed for {os.path.basename(src)} ({e}); keeping original")
        return False


def download_url(s, url, dest):
    tmp = dest + ".part"

    def _do():
        try:
            with s.get(url, stream=True, timeout=300) as resp:
                if resp.status_code in (401, 403, 410):
                    # A signed asset URL went stale (rare: only on very slow/huge runs).
                    raise ICloudDLError(
                        f"asset URL expired (HTTP {resp.status_code}); re-run to resume.")
                resp.raise_for_status()
                expected = resp.headers.get("Content-Length")
                written = 0
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(1 << 16):
                        if chunk:
                            fh.write(chunk)
                            written += len(chunk)
                if expected is not None and written != int(expected):
                    raise IOError(f"short read: got {written} of {expected} bytes")
            os.replace(tmp, dest)
        except OSError as e:
            if getattr(e, "errno", None) in (errno.ENOSPC, errno.EDQUOT, errno.EROFS):
                raise _FatalIO(f"cannot write {dest}: {os.strerror(e.errno)} — "
                               "free up disk space (or pick another --output) and re-run.")
            raise
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    retry(_do, f"download {os.path.basename(dest)}")


# ----------------------------------------------------------------------------- EXIF / sidecar
def run_exiftool(paths):
    """Read EXIF for many files, in chunks so we never blow the arg limit or stall forever."""
    if not paths:
        return {}
    base = ["exiftool", "-json", "-q", "-api", "largefilesupport=1",
            "-c", "%+.8f", "-d", "%Y-%m-%dT%H:%M:%S"]
    out = {}
    CHUNK = 200
    for i in range(0, len(paths), CHUNK):
        batch = paths[i:i + CHUNK]
        try:
            p = subprocess.run(base + batch, capture_output=True, text=True, timeout=1800)
        except FileNotFoundError:
            warn_once("no-exiftool",
                      "exiftool not found on PATH; sidecars will carry iCloud fields only "
                      "(no embedded EXIF/GPS). Install:  apt install libimage-exiftool-perl  "
                      "or  brew install exiftool")
            return out
        except subprocess.TimeoutExpired:
            warn(f"exiftool timed out on {len(batch)} files; those sidecars lack embedded EXIF")
            continue
        except Exception as e:  # noqa: BLE001
            warn(f"exiftool failed: {e}")
            continue
        if not p.stdout.strip():
            if p.stderr.strip():
                dbg(f"exiftool: {p.stderr.strip()[:200]}")
            continue
        try:
            for e in json.loads(p.stdout):
                src = e.pop("SourceFile", None)
                if src:
                    out[os.path.abspath(src)] = e
        except ValueError as e:
            warn(f"could not parse exiftool output: {e}")
    return out


def _num(v):
    try:
        return float(str(v).split()[0])
    except Exception:
        return None


def build_sidecar(album, meta, exif):
    gps = None
    lat, lon = exif.get("GPSLatitude"), exif.get("GPSLongitude")
    if lat is not None and lon is not None:
        gps = {"lat": _num(lat), "lon": _num(lon), "alt": _num(exif.get("GPSAltitude"))}
    return {
        "source": "icloud-shared-album",
        "album": album["name"],
        "filename": meta["savedAs"],
        "original_filename": meta["originalFilename"],
        "capture_time": exif.get("DateTimeOriginal") or exif.get("CreateDate")
                        or (meta["master_fields"].get("originalCreationDate") or {}).get("iso"),
        "gps": gps,
        "camera": {"make": exif.get("Make"), "model": exif.get("Model"),
                   "lens": exif.get("LensModel")},
        "exposure": {"f_number": exif.get("FNumber"), "exposure_time": exif.get("ExposureTime"),
                     "iso": exif.get("ISO"), "focal_length_mm": _num(exif.get("FocalLength"))},
        "image": {"width": exif.get("ImageWidth"), "height": exif.get("ImageHeight"),
                  "content_type": exif.get("MIMEType")},
        "icloud": {
            "asset_guid": meta.get("assetguid"),
            "master_record": meta.get("masterRecordName"),
            "owner_dsid": album.get("ownerdsid"),
            "sharing_type": album.get("sharingtype"),
            "contributed_by": meta["asset_fields"].get("contributedBy"),
            "added_date": (meta["asset_fields"].get("addedDate") or {}).get("iso"),
            "batch_create_date": (meta["asset_fields"].get("batchCreateDate") or {}).get("iso"),
            "duration_ms": meta["asset_fields"].get("duration"),
        },
        "icloud_fields": {"master": meta["master_fields"], "asset": meta["asset_fields"]},
        "exif_raw": exif,
        "notes": ["iCloud serves shared-album media as re-encoded (~1536px) files, "
                  "not full-resolution originals."],
    }


# ----------------------------------------------------------------------------- album driver
def load_index(folder):
    path = os.path.join(folder, INDEX_NAME)
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            return {}
    return {}


def save_index(folder, index):
    path = os.path.join(folder, INDEX_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(index, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def deterministic_name(original, assetguid, taken):
    """Stable filename: original stem, disambiguated by asset GUID (never by order)."""
    base = sanitize(os.path.basename(original or ""), assetguid)
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    cand = base
    owner = taken.get(cand.lower())
    if owner and owner != assetguid:
        short = assetguid.split("-")[0][:8]
        cand = f"{stem}_{short}{('.' + ext) if ext else ''}"
    taken[cand.lower()] = assetguid
    return cand


def download_album(s, album, args):
    name = album["name"] or album["guid"]
    folder = os.path.join(args.output, sanitize(album["name"], album["guid"]))
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        raise ICloudDLError(f"cannot create output folder {folder!r}: {e}")
    index = {} if args.force else load_index(folder)
    taken = {v["savedAs"].lower(): k for k, v in index.items()
             if isinstance(v, dict) and v.get("savedAs")}

    total = album_count(s, album)
    info(f"\n=== {name}  ({'?' if total is None else total} items)  ->  {folder}")

    new_items = []      # (dest_path, meta) downloaded this run (need sidecar/exif)
    index_lock = threading.Lock()
    done = skipped = failed = 0

    offset = 0
    while True:
        records = retry(lambda: fetch_page(s, album, offset), f"page@{offset}")
        masters = [r for r in records if r.get("recordType") == "CPLMaster"]
        assets_by_master = {}
        for r in records:
            if r.get("recordType") == "CPLAsset":
                mref = (r.get("fields", {}).get("masterRef", {}) or {}).get("value", {})
                mrn = mref.get("recordName") if isinstance(mref, dict) else None
                if mrn:
                    assets_by_master[mrn] = r
        if not masters:
            break

        jobs = []
        for m in masters:
            mf = m.get("fields", {})
            asset = assets_by_master.get(m["recordName"])
            af = asset.get("fields", {}) if asset else {}
            assetguid = asset["recordName"] if asset else m["recordName"]
            key, url, ftype = pick_resource(mf)
            is_video = (ftype or "").startswith(("public.mpeg", "com.apple.quicktime")) \
                or (key or "").startswith(("resVid",))
            if args.novideo and is_video:
                continue
            filename = b64txt(fval(mf, "filenameEnc")) or b64txt(fval(af, "filenameEnc")) \
                or (assetguid + ".bin")

            # idempotency: already have this asset on disk?
            prev = index.get(assetguid)
            if prev and not args.force and os.path.exists(os.path.join(folder, prev["savedAs"])):
                skipped += 1
                continue

            with index_lock:
                saved = prev["savedAs"] if (prev and not args.force) \
                    else deterministic_name(filename, assetguid, taken)
            meta = {
                "assetguid": assetguid, "masterRecordName": m["recordName"],
                "originalFilename": filename, "savedAs": saved,
                "resourceUsed": key, "fileType": ftype,
                "master_fields": scalar_fields(mf), "asset_fields": scalar_fields(af),
            }
            if url is None:
                failed += 1
                warn(f"no download URL for {filename}")
                continue
            jobs.append((meta, url))

        def work(job):
            meta, url = job
            dest = os.path.join(folder, meta["savedAs"])
            if args.dry_run:
                return ("dry", meta, dest)
            try:
                download_url(s, url, dest)
                if not args.no_convert:
                    ext, needs = sniff_type(dest)
                    if needs:  # genuine HEIC -> transcode to JPEG
                        newdest = os.path.splitext(dest)[0] + ".jpg"
                        if transcode_heic(dest, newdest):
                            if newdest != dest:
                                os.remove(dest)
                            dest = newdest
                    elif ext:  # already a normal format: just correct the extension (lossless)
                        if os.path.splitext(dest)[1].lower() != ext:
                            newdest = os.path.splitext(dest)[0] + ext
                            if os.path.abspath(newdest) != os.path.abspath(dest):
                                os.replace(dest, newdest)
                            dest = newdest
                    meta["savedAs"] = os.path.basename(dest)
                return ("ok", meta, dest)
            except (_FatalIO, _AuthError):
                raise  # abort the whole run — retrying every remaining asset is pointless
            except Exception as e:  # noqa: BLE001 - never let one asset kill the run
                return ("fail", meta, str(e))

        with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            for status, meta, extra in pool.map(work, jobs):
                if status == "ok":
                    done += 1
                    new_items.append((extra, meta))
                    with index_lock:
                        index[meta["assetguid"]] = {"savedAs": meta["savedAs"]}
                    if done % 25 == 0:
                        info(f"  downloaded {done} (skipped {skipped})...")
                elif status == "dry":
                    done += 1
                elif status == "fail":
                    failed += 1
                    err(f"  failed: {meta['originalFilename']}: {extra}")

        if not args.dry_run:
            save_index(folder, index)
        offset += len(masters)
        if len(masters) < PAGE:
            break

    if args.dry_run:
        info(f"  [dry-run] would download {done}, skip {skipped}")
        return

    # sidecars (idempotent: regenerated deterministically for items fetched this run)
    if not args.no_sidecar and new_items:
        info(f"  writing sidecars + EXIF for {len(new_items)} files...")
        exif = run_exiftool([p for p, _ in new_items])
        for dest, meta in new_items:
            side = build_sidecar(album, meta, exif.get(os.path.abspath(dest), {}))
            with open(dest + ".json", "w") as fh:
                json.dump(side, fh, indent=2, ensure_ascii=False)

    if args.manifest:
        with open(os.path.join(folder, "album.json"), "w") as fh:
            json.dump({"album": album, "count": total, "index": index}, fh,
                      indent=2, ensure_ascii=False)

    media = len([f for f in os.listdir(folder)
                 if not f.endswith(".json") and f != INDEX_NAME])
    good(f"{name}: {done} new, {skipped} already present, {failed} failed  "
         f"({media} files on disk)")
    if failed:
        warn(f"{failed} asset(s) in {name!r} did not download — re-run the same command to "
             "retry just those (finished files are skipped).")


# ----------------------------------------------------------------------------- resolve / CLI
def resolve_albums(selectors, albums):
    chosen, seen = [], set()
    for sel in selectors:
        s = sel.strip()
        match = None
        if s.isdigit() and int(s) < len(albums):
            match = albums[int(s)]
        if match is None:
            for a in albums:
                if s.lower() in ((a.get("guid") or "").lower(), (a.get("name") or "").lower()):
                    match = a
                    break
        if match is None:
            warn(f"no album matches {sel!r}; skipping "
                 f"(use -l to see available names/indexes)")
            continue
        if match["guid"] not in seen:
            seen.add(match["guid"])
            chosen.append(match)
    return chosen


__version__ = "1.0.0"

EXAMPLES = """\
examples:
  iclouddl -cf cookies.json -l                      list shared albums you can access
  iclouddl -cf cookies.json -a "Lake Tahoe" -a 1    download specific albums (name / index / GUID)
  iclouddl -cf cookies.json -A -o ~/Pictures/shared download every album
  iclouddl -cf cookies.json -A --dry-run            preview without downloading
  iclouddl -cf cookies.json -A -v                   verbose: show retries + full tracebacks

Re-runs are idempotent: already-downloaded assets are skipped. Add -v (or --debug) when
reporting a bug so the output shows exactly where it failed.
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog="iclouddl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Download photos and videos from an iCloud share for those without an Apple device.",
        epilog=EXAMPLES)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # actions (mutually exclusive)
    g = p.add_mutually_exclusive_group()
    g.add_argument("-l", "--list", action="store_true", help="List shared albums you can access")
    g.add_argument("-a", "--album", action="append", metavar="ALBUM",
                   help="Download one album by index, name, or GUID (repeatable)")
    g.add_argument("-A", "--all", action="store_true", help="Download every shared album")
    # inputs / options
    p.add_argument("-cf", "--cookies", default=os.environ.get("ICLOUDDL_COOKIES"),
                   help="Exported www.icloud.com.cookies.json (or $ICLOUDDL_COOKIES)")
    p.add_argument("-o", "--output", default="icloud-shared", help="Output root directory")
    p.add_argument("-nv", "--novideo", action="store_true", help="Skip videos")
    p.add_argument("-f", "--force", action="store_true", help="Re-download even if already present")
    p.add_argument("-j", "--jobs", type=int, default=4, help="Parallel downloads (default 4)")
    p.add_argument("--no-convert", action="store_true",
                   help="Keep bytes exactly as served (default: fix extension / transcode true HEIC)")
    p.add_argument("--no-sidecar", action="store_true", help="Do not write JSON sidecars")
    p.add_argument("--manifest", action="store_true", help="Also write album.json (raw iCloud records)")
    p.add_argument("--dry-run", action="store_true", help="Show what would download; fetch nothing")
    p.add_argument("-q", "--quiet", action="store_true", help="Only print warnings and errors")
    p.add_argument("-v", "--verbose", "--debug", dest="verbose", action="store_true",
                   help="Verbose: show retries and full tracebacks (use when reporting a bug)")
    return p


def _run(args):
    """The actual work; raises ICloudDLError / other exceptions for main() to format."""
    if requests is None:
        raise ICloudDLError("the 'requests' package is required but not installed. "
                            "Install dependencies:  pip install -r requirements.txt")
    if args.jobs < 1:
        warn(f"--jobs {args.jobs} is invalid; using 1")
        args.jobs = 1

    s, dsid, ss = authenticate(args.cookies)
    albums = retry(lambda: list_albums(s, dsid, ss), "list albums")

    if args.list:
        if not albums:
            info("no shared albums found on this account")
        for i, a in enumerate(albums):
            n = album_count(s, a)
            info(f"[{i}] {a['name']}  ({'?' if n is None else n} items)  {a['guid']}")
        return 0

    targets = albums if args.all else resolve_albums(args.album, albums)
    if not targets:
        raise ICloudDLError("no matching albums to download (run with -l to list them)")
    for a in targets:
        download_album(s, a, args)
    good(f"\nall done: {len(targets)} album(s) under {os.path.abspath(args.output)}")
    return 0


def main(argv=None):
    global VERBOSE, QUIET
    args = build_parser().parse_args(argv)
    VERBOSE, QUIET = args.verbose, args.quiet

    if not (args.list or args.all or args.album):
        build_parser().print_help()
        return 0

    try:
        return _run(args)
    except KeyboardInterrupt:
        err("\ninterrupted. Re-run the same command to resume where it left off.")
        return 130
    except _AuthError as e:
        err(f"iCloud rejected the session (HTTP {e.code}). Your cookies are expired or were "
            "invalidated by signing out.")
        err("Fix: sign in at www.icloud.com, re-export the cookies, overwrite the file, re-run.")
        return 3
    except ICloudDLError as e:
        err(str(e))
        return 1
    except Exception as e:  # noqa: BLE001 - last line of defense; keep it legible
        err(f"unexpected error: {type(e).__name__}: {e}")
        if VERBOSE:
            traceback.print_exc()
        else:
            err("re-run with -v (or --debug) to see the full traceback.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
