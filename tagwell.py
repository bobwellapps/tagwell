#!/usr/bin/env python3
"""
Tagwell - a simple music tag editor (tags, cover art, lyrics) for
FLAC, MP3, M4A (AAC/ALAC), OGG Vorbis and Opus files.

Open one or many music files, select one or several in the list, and add,
edit, rename or remove tags. Changes apply to every selected file.

Requirements:
    pip install PySide6 mutagen

Run:
    python3 tagwell.py [music file | folder ...]
"""

import sys
import os
import re
import json
import base64
import ssl
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from mutagen import MutagenError
from mutagen.flac import FLAC, Picture
from PySide6.QtCore import Qt, QRegularExpression, QTimer, QBuffer, QIODevice
from PySide6.QtGui import (QAction, QKeySequence, QRegularExpressionValidator, QFont,
                           QColor, QImage, QPixmap)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QPushButton, QFileDialog,
    QMessageBox, QHeaderView, QLabel, QComboBox, QAbstractItemView,
    QStyledItemDelegate, QLineEdit, QCompleter, QInputDialog,
    QListWidget, QListWidgetItem, QSplitter, QDialog, QDialogButtonBox,
    QFormLayout, QSizePolicy, QFrame, QScrollArea, QCheckBox, QListView,
    QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QRadioButton, QProgressBar,
    QStyle,
)
from PySide6.QtCore import QEvent, QObject, Signal, QSize, QLockFile, QDir
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import (QIcon, QPalette, QPainter, QPainterPath, QPen,
                           QFontMetrics)
from PySide6.QtCore import QUrl, QRectF, QSettings, qVersion, QRect, QItemSelection, QItemSelectionModel
from PySide6.QtWidgets import (QStyleOptionViewItem, QToolBar, QToolButton,
                               QStackedWidget, QMenu, QRubberBand)

# Common tag names offered in the dropdowns; any custom name can be typed in.
COMMON_TAGS = [
    "TITLE", "ARTIST", "ALBUM", "ALBUMARTIST", "TRACKNUMBER", "TOTALTRACKS",
    "DISCNUMBER", "TOTALDISCS", "DATE", "GENRE", "COMPOSER", "PERFORMER",
    "COMMENT", "DESCRIPTION", "LYRICS", "ISRC", "LABEL", "COPYRIGHT",
]

# Old names replaced by TOTALTRACKS / TOTALDISCS: never offered in any tag list,
# even when a loaded file or preset still uses them.
NEVER_SUGGEST = {"TRACKTOTAL", "DISCTOTAL"}

MIXED_TEXT = "<different in each file>"

# Tags that hold several lines of text. These (and any value containing a line
# break) are edited in a text window instead of inside the table cell.
LONG_TEXT_TAGS = {"LYRICS", "UNSYNCEDLYRICS", "SYNCEDLYRICS"}
FULL_TEXT = Qt.UserRole + 1


def is_long_text(key, value):
    return key in LONG_TEXT_TAGS or "\n" in value or "\r" in value


def text_summary(value):
    lines = [l for l in value.replace("\r\n", "\n").split("\n")]
    first = next((l.strip() for l in lines if l.strip()), "")
    count = sum(1 for l in lines if l.strip())
    if count <= 1:
        return first
    return f"{first}   … ({count} lines — double-click to view)"


class TextEditDialog(QDialog):
    """View or edit a multi-line tag value, such as lyrics."""

    def __init__(self, parent, key, text, hint=""):
        super().__init__(parent)
        self.setWindowTitle(f"Edit {key}")
        self.resize(560, 620)
        lay = QVBoxLayout(self)
        if hint:
            h = QLabel(hint)
            h.setWordWrap(True)
            lay.addWidget(h)
        self.edit = QPlainTextEdit(text)
        self.edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        lay.addWidget(self.edit, 1)
        self.count = QLabel()
        lay.addWidget(self.count)
        self.edit.textChanged.connect(self._update_count)
        self._update_count()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _update_count(self):
        t = self.edit.toPlainText()
        lines = sum(1 for l in t.split("\n") if l.strip())
        synced = bool(LRC_LINE.search(t))
        self.count.setText(f"{lines} line{'s' if lines != 1 else ''}" +
                           (" · time-synced (LRC)" if synced else ""))

    def text(self):
        return self.edit.toPlainText()
META = Qt.UserRole  # row info stored on the Tag cell: ("same", key, i) or ("mixed", key)


# A time-synced lyrics line, e.g. "[01:23.45] some words"
LRC_LINE = re.compile(r"^\s*\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]", re.M)


def natural_key(path):
    """Sort key that puts 'Track 2' before 'Track 10'."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", path.lower())]


def valid_tag_name(name: str) -> bool:
    """Vorbis comment field names: printable ASCII 0x20-0x7D, no '='."""
    if not name:
        return False
    return all(0x20 <= ord(c) <= 0x7D and c != "=" for c in name)


# Only characters allowed in a tag name can be typed (ASCII 0x20-0x7D, no '=').
TAG_NAME_REGEX = QRegularExpression(r"[\x20-\x3C\x3E-\x7D]+")


# ---------------------------------------------------------------- pictures
# The standard FLAC/ID3 picture types, by number.
PICTURE_TYPES = [
    "Other", "File icon (32×32 PNG)", "Other file icon", "Front cover",
    "Back cover", "Leaflet page", "Media (e.g. CD label)", "Lead artist",
    "Artist", "Conductor", "Band", "Composer", "Lyricist", "Recording location",
    "During recording", "During performance", "Video screen capture",
    "A bright coloured fish", "Illustration", "Band logo", "Publisher logo",
]
FRONT_COVER = 3
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


def type_name(t):
    return PICTURE_TYPES[t] if 0 <= t < len(PICTURE_TYPES) else f"Type {t}"


class Pic:
    """One embedded picture (immutable, so lists of them compare by content)."""
    __slots__ = ("type", "mime", "desc", "width", "height", "depth", "data")

    def __init__(self, type, mime, desc, width, height, depth, data):
        self.type, self.mime, self.desc = type, mime, desc
        self.width, self.height, self.depth, self.data = width, height, depth, data

    def _key(self):
        return (self.type, self.mime, self.desc, self.data)

    def __eq__(self, other):
        return isinstance(other, Pic) and self._key() == other._key()

    def __hash__(self):
        return hash((self.type, self.mime, len(self.data), self.data[:256]))

    @classmethod
    def from_mutagen(cls, p):
        return cls(p.type, p.mime, p.desc, p.width, p.height, p.depth, bytes(p.data))

    @classmethod
    def from_file(cls, path, ptype):
        with open(path, "rb") as fh:
            return cls.from_bytes(fh.read(), ptype)

    @classmethod
    def from_bytes(cls, data, ptype):
        img = QImage.fromData(data)
        if img.isNull():
            raise ValueError("This file isn't an image the app can read.")
        if data[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        else:
            # Players widely support only JPEG and PNG: convert anything else to PNG.
            buf = QBuffer()
            buf.open(QIODevice.WriteOnly)
            img.save(buf, "PNG")
            data, mime = bytes(buf.data()), "image/png"
        return cls(ptype, mime, "", img.width(), img.height(), img.depth(), data)

    def to_mutagen(self):
        p = Picture()
        p.type, p.mime, p.desc = self.type, self.mime, self.desc
        p.width, p.height, p.depth, p.data = self.width, self.height, self.depth, self.data
        return p

    def with_type(self, ptype):
        return Pic(ptype, self.mime, self.desc, self.width, self.height, self.depth, self.data)

    @property
    def ext(self):
        return {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
                "image/gif": ".gif", "image/webp": ".webp",
                "image/bmp": ".bmp"}.get(self.mime.lower(), ".img")

    def summary(self):
        size = len(self.data)
        size = f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1048576:.1f} MB"
        fmt = self.ext.lstrip(".").upper().replace("JPG", "JPEG")
        dims = f"{self.width}×{self.height}" if self.width else ""
        return " · ".join(x for x in (fmt, dims, size) if x)


# ---------------------------------------------------------------- online covers
USER_AGENT = "Tagwell/1.1 (https://github.com/bobwellapps/tagwell)"
SIZES = ["Medium (500 px)", "Large (1200 px)", "Original (full size)"]
SIZE_KEYS = ["medium", "large", "largest"]


def _ssl_context():
    # Use the bundled certificate list so HTTPS works on any distro.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL = None


def http_get(url, timeout=20, limit=40 * 1024 * 1024):
    global _SSL
    if _SSL is None:
        _SSL = _ssl_context()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("download too large")
    return data


def _lucene(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def search_cover_art_archive(artist, album):
    q = "releasegroup:" + _lucene(album)
    if artist:
        q += " AND artist:" + _lucene(artist)
    url = ("https://musicbrainz.org/ws/2/release-group/?fmt=json&limit=15&query="
           + urllib.parse.quote(q))
    data = json.loads(http_get(url))
    out = []
    for rg in data.get("release-groups", []):
        base = f"https://coverartarchive.org/release-group/{rg['id']}/front"
        credit = "".join(a.get("name", "") + a.get("joinphrase", "")
                         for a in rg.get("artist-credit", []))
        extra = " ".join(x for x in (rg.get("first-release-date", "")[:4],
                                     rg.get("primary-type", "")) if x)
        out.append({
            "source": "Cover Art Archive", "title": rg.get("title", ""),
            "artist": credit, "extra": extra, "thumb": base + "-250",
            "full": {"medium": [base + "-500", base + "-1200", base],
                     "large": [base + "-1200", base, base + "-500"],
                     "largest": [base, base + "-1200", base + "-500"]},
        })
    return out


class _Bridge(QObject):
    """Carries results from worker threads back to the window."""
    found = Signal(int, object, str)      # search id, results, error
    thumb = Signal(int, int, bytes)       # search id, result index, image data
    fetched = Signal(object, str)         # full image bytes (or None), error


class CoverSearchDialog(QDialog):
    def __init__(self, parent, artist, album):
        super().__init__(parent)
        self.setWindowTitle("Find cover online")
        self.resize(820, 600)
        self.image_data = None
        self.results = []
        self.search_id = 0
        self.pending = 0
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.bridge = _Bridge()
        self.bridge.found.connect(self._on_found)
        self.bridge.thumb.connect(self._on_thumb)
        self.bridge.fetched.connect(self._on_fetched)

        lay = QVBoxLayout(self)
        form = QHBoxLayout()
        self.artist_edit = QLineEdit(artist)
        self.artist_edit.setPlaceholderText("Artist")
        self.album_edit = QLineEdit(album)
        self.album_edit.setPlaceholderText("Album")
        self.search_btn = QPushButton("Search")
        self.search_btn.setDefault(True)
        self.search_btn.clicked.connect(self.search)
        for w in (self.artist_edit, self.album_edit):
            w.returnPressed.connect(self.search)
        form.addWidget(QLabel("Artist:"))
        form.addWidget(self.artist_edit, 1)
        form.addWidget(QLabel("Album:"))
        form.addWidget(self.album_edit, 1)
        form.addWidget(self.search_btn)
        lay.addLayout(form)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("Covers from the Cover Art Archive (MusicBrainz)"))
        opts.addStretch(1)
        opts.addWidget(QLabel("Download size:"))
        self.size_combo = QComboBox()
        self.size_combo.addItems(SIZES)
        self.size_combo.setCurrentIndex(1)
        opts.addWidget(self.size_combo)
        lay.addLayout(opts)

        self.list = QListWidget()
        self.list.setViewMode(QListView.IconMode)
        self.list.setIconSize(QSize(170, 170))
        self.list.setGridSize(QSize(200, 272))
        self.list.setResizeMode(QListView.Adjust)
        self.list.setMovement(QListView.Static)
        self.list.setWordWrap(True)
        self.list.setSpacing(6)
        self.list.itemDoubleClicked.connect(lambda _: self.use_selected())
        self.list.itemSelectionChanged.connect(
            lambda: self.use_btn.setEnabled(bool(self.list.selectedItems())))
        lay.addWidget(self.list, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        bb = QDialogButtonBox()
        self.web_btn = bb.addButton("Search the Web…", QDialogButtonBox.ResetRole)
        self.web_btn.setIcon(icon("internet-web-browser", "edit-find"))
        self.web_btn.setToolTip("Look for the cover in your web browser, then drag it in")
        self.web_btn.setAutoDefault(False)
        self.web_btn.clicked.connect(self.search_web)
        self.use_btn = bb.addButton("Use this cover", QDialogButtonBox.AcceptRole)
        self.use_btn.setEnabled(False)
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self.use_selected)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        if album:
            QTimer.singleShot(0, self.search)
        else:
            self.status.setText("Type the album name (and artist), then press Search.")

    # ---- searching
    def search(self):
        artist = self.artist_edit.text().strip()
        album = self.album_edit.text().strip()
        if not album:
            self.status.setText("Type the album name to search for "
                                "(adding the artist gives better matches).")
            return
        self.search_id += 1
        sid = self.search_id
        self.results = []
        self.error = ""
        self.list.clear()
        self.pending = 1
        self.status.setText("Searching the Cover Art Archive…")
        self.pool.submit(self._run_search, sid, artist, album)

    def _run_search(self, sid, artist, album):
        try:
            self.bridge.found.emit(sid, search_cover_art_archive(artist, album), "")
        except Exception as e:
            self.bridge.found.emit(sid, [], _friendly_error(e))

    def _on_found(self, sid, results, error):
        if sid != self.search_id:
            return  # an older search
        self.pending = 0
        self.error = error
        for r in results:
            idx = len(self.results)
            self.results.append(r)
            text = r["title"] + "\n" + r["artist"] + ("\n" + r["extra"] if r["extra"] else "")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, idx)
            item.setToolTip(text)
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            item.setSizeHint(QSize(188, 262))
            item.setFlags(item.flags() & ~Qt.ItemIsSelectable & ~Qt.ItemIsEnabled)
            self.list.addItem(item)
            item.setHidden(True)  # shown once its thumbnail has loaded
            r["item"] = item
            r["thumb_state"] = "loading"
            self.pool.submit(self._load_thumb, sid, idx, r["thumb"])
        self._update_status()

    def _load_thumb(self, sid, idx, url):
        try:
            data = http_get(url, timeout=20, limit=5 * 1024 * 1024)
        except Exception:
            data = b""
        self.bridge.thumb.emit(sid, idx, data)

    def _on_thumb(self, sid, idx, data):
        if sid != self.search_id:
            return
        r = self.results[idx]
        img = QImage.fromData(data) if data else QImage()
        if img.isNull():
            r["thumb_state"] = "none"  # e.g. no cover in the archive for that release
        else:
            r["thumb_state"] = "ok"
            item = r["item"]
            item.setIcon(QIcon(QPixmap.fromImage(img).scaled(
                170, 170, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            item.setFlags(item.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            item.setHidden(False)
            if not self.list.selectedItems():
                item.setSelected(True)
                self.list.setCurrentItem(item)
        self._update_status()

    def _update_status(self):
        shown = sum(1 for r in self.results if r.get("thumb_state") == "ok")
        loading = self.pending or any(r.get("thumb_state") == "loading" for r in self.results)
        if loading:
            msg = f"Searching… {shown} cover{'s' if shown != 1 else ''} found so far"
        elif shown:
            msg = (f"{shown} cover{'s' if shown != 1 else ''} found. "
                   "Double-click one, or select it and press Use this cover.")
        elif self.error:
            msg = (f"Couldn't search the Cover Art Archive ({self.error}). "
                   "Check your internet connection and try again, or press Search the Web…")
        elif self.results:
            msg = ("Found the album, but nobody has uploaded a cover for it to the "
                   "Cover Art Archive yet. Try a different edition, or press "
                   "Search the Web… to find it in your browser.")
        else:
            msg = ("No matching albums found. Try fewer words, e.g. drop “Deluxe Edition” "
                   "or “Remastered”, check the spelling, or press Search the Web… "
                   "to find the cover in your browser.")
        self.status.setText(msg)

    def search_web(self):
        words = " ".join(w for w in (self.artist_edit.text().strip(),
                                     self.album_edit.text().strip()) if w)
        data = PictureFinderDialog.ask(self, (words + " album cover").strip(),
                                       search_now=bool(words))
        if data:
            self.image_data = data
            self.accept()

    # ---- choosing
    def use_selected(self):
        items = self.list.selectedItems()
        if not items:
            return
        r = self.results[items[0].data(Qt.UserRole)]
        urls = r["full"][SIZE_KEYS[self.size_combo.currentIndex()]]
        self.setEnabled(False)
        self.status.setText("Downloading cover…")
        self.pool.submit(self._download, urls)

    def _download(self, urls):
        err = ""
        for url in urls:  # fall back to smaller sizes if a size isn't available
            try:
                data = http_get(url, timeout=60)
                if not QImage.fromData(data).isNull():
                    self.bridge.fetched.emit(data, "")
                    return
                err = "the download wasn't an image"
            except Exception as e:
                err = _friendly_error(e)
        self.bridge.fetched.emit(None, err)

    def _on_fetched(self, data, error):
        self.setEnabled(True)
        if data is None:
            self.status.setText(f"Couldn't download that cover ({error}). Try another one.")
            return
        self.image_data = data
        self.accept()

    def done(self, result):
        self.search_id += 1  # ignore anything still arriving
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().done(result)


# ---------------------------------------------------------------- pictures from the web
# Based on the webimage kit (as used in Musicwell). Tagwell never searches image sites
# itself: it opens an image search in the person's own web browser, and they drag the
# picture they want into Tagwell, paste it, or choose a file.
import threading
from PySide6.QtGui import QDesktopServices, QGuiApplication, QShortcut

WEB_ENGINES = {
    "duckduckgo": ("DuckDuckGo", "https://duckduckgo.com/?iax=images&ia=images&q={q}"),
    "google": ("Google", "https://www.google.com/search?tbm=isch&q={q}"),
    "bing": ("Bing", "https://www.bing.com/images/search?q={q}"),
}
DEFAULT_WEB_ENGINE = "duckduckgo"
WEB_MAX_BYTES = 15 * 1024 * 1024
WEB_MAX_SIDE = 1600   # bigger pictures are made smaller before they go into the files
WEB_SMALL = 300       # smaller than this: still used, but Tagwell says it's small
# drags from Tagwell's own lists and tables are never taken as pictures
_OWN_DRAG_FORMATS = ["application/x-qabstractitemmodeldatalist"]


def tagwell_settings():
    return QSettings(os.path.join(os.path.dirname(presets_path()), "tagwell.conf"),
                     QSettings.IniFormat)


def web_engine(settings):
    e = str(settings.value("web/image_engine", DEFAULT_WEB_ENGINE) or DEFAULT_WEB_ENGINE)
    return e if e in WEB_ENGINES else DEFAULT_WEB_ENGINE


def web_search_url(words, engine=DEFAULT_WEB_ENGINE):
    url = WEB_ENGINES.get(engine, WEB_ENGINES[DEFAULT_WEB_ENGINE])[1]
    return url.format(q=urllib.parse.quote_plus(words.strip()))


def open_web_search(words, engine=DEFAULT_WEB_ENGINE):
    """Opens the person's web browser on an image search for these words."""
    return open_in_browser(web_search_url(words, engine))


def open_in_browser(url):
    if getattr(sys, "frozen", False):
        # The AppImage points the library path at its own bundled libraries; the web
        # browser must start with the system's own, so give it the original settings.
        import subprocess
        env = dict(os.environ)
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
        try:
            subprocess.Popen(["xdg-open", url], env=env, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    return QDesktopServices.openUrl(QUrl(url))


def web_accepts(mime):
    """Is there a picture (or a picture's web address) in this drag or the clipboard?"""
    if mime is None or any(mime.hasFormat(f) for f in _OWN_DRAG_FORMATS):
        return False
    if mime.hasImage():
        return True
    if mime.hasUrls() and any(u.scheme() in ("http", "https", "data") for u in mime.urls()):
        return True
    if mime.hasHtml() and "<img" in mime.html().lower():
        return True
    if mime.hasText() and re.match(r"\s*(https?://|data:image/)\S+\s*$", mime.text() or ""):
        return True
    return False


def _web_unwrap(url):
    """A search result's link -> the picture itself (Google imgurl=, Bing mediaurl=, DDG u=)."""
    try:
        parts = urllib.parse.urlsplit(url)
        qs = urllib.parse.parse_qs(parts.query)
    except ValueError:
        return url
    for k in ("imgurl", "mediaurl", "u", "img_url", "image_url"):
        v = qs.get(k)
        if v and v[0].lower().startswith(("http://", "https://")):
            if k != "u" or "duckduckgo" in (parts.hostname or ""):
                return v[0]
    return url


def _web_looks_like_image(url):
    return bool(re.search(r"\.(jpe?g|png|webp|gif|bmp|avif)(\?|#|$)", url, re.I))


def _encode_image(img, fmt="JPEG"):
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, fmt, 92 if fmt == "JPEG" else -1)
    buf.close()
    return bytes(buf.data())


def web_extract(mime):
    """Everything usable in a drop or the clipboard, taken now (a drag's data is gone
    afterwards): {"urls": [best first], "files": [paths], "data": PNG bytes or b""}."""
    if not web_accepts(mime):
        return None
    urls, files = [], []
    if mime.hasUrls():
        for u in mime.urls():
            if u.isLocalFile():
                files.append(u.toLocalFile())
            elif u.scheme() in ("http", "https", "data"):
                urls.append(u.toString())
    if mime.hasHtml():
        for src in re.findall(r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", mime.html(), re.I):
            urls.append(src.replace("&amp;", "&"))
    if mime.hasText():
        t = (mime.text() or "").strip()
        if re.match(r"(https?://|data:image/)\S+$", t):
            urls.append(t)
    full = [_web_unwrap(u) for u in urls]
    # the full-size picture behind a search result first, then pictures' own
    # addresses, then web pages, then data: pictures
    ranked = sorted(dict.fromkeys(full), key=lambda u: (
        0 if u not in urls else 1 if _web_looks_like_image(u)
        else 3 if u.startswith("data:") else 2))
    data = b""
    if mime.hasImage():
        img = mime.imageData()
        if isinstance(img, QImage) and not img.isNull():
            data = _encode_image(img, "PNG")
    return {"urls": ranked, "files": files, "data": data}


def web_tidy(data):
    """A picture Tagwell can read? Then as a JPEG (or PNG, if it has see-through parts),
    at most WEB_MAX_SIDE pixels on its longest side."""
    img = QImage.fromData(data)
    if img.isNull():
        return None
    if max(img.width(), img.height()) > WEB_MAX_SIDE:
        img = img.scaled(WEB_MAX_SIDE, WEB_MAX_SIDE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    elif data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return data  # already a JPEG or PNG of a fine size
    return _encode_image(img, "PNG" if img.hasAlphaChannel() else "JPEG")


def _web_page_picture(html, base):
    """A web page dropped instead of its picture: the picture it shares itself with."""
    for pat in (r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image|og:image:url|twitter:image)["'][^>]*content\s*=\s*["']([^"']+)""",
                r"""<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]*(?:property|name)\s*=\s*["'](?:og:image|twitter:image)["']"""):
        m = re.search(pat, html, re.I)
        if m:
            return urllib.parse.urljoin(base, m.group(1).replace("&amp;", "&"))
    return ""


def web_fetch(found):
    """(picture bytes, "") or (b"", why not). Runs in a background thread."""
    why = ""
    for f in found.get("files", []):
        try:
            with open(f, "rb") as fh:
                data = fh.read(WEB_MAX_BYTES)
        except OSError as e:
            why = f"couldn't read {os.path.basename(f)}: {e.strerror or e}"
            continue
        t = web_tidy(data)
        if t:
            return t, ""
        why = f"{os.path.basename(f)} isn't a picture Tagwell can read"
    for url in found.get("urls", [])[:6]:
        if url.startswith("data:"):
            m = re.match(r"data:image/[\w.+-]+;base64,(.*)$", url, re.S)
            if m:
                try:
                    t = web_tidy(base64.b64decode(m.group(1)))
                except (ValueError, TypeError):
                    t = None
                if t:
                    return t, ""
            continue
        try:
            data = http_get(url, timeout=15, limit=WEB_MAX_BYTES)
        except Exception as e:
            why = f"the picture couldn't be downloaded ({_friendly_error(e)})"
            continue
        t = web_tidy(data)
        if t:
            return t, ""
        head = data[:400000].decode("utf-8", "replace")
        if "<html" in head.lower() or "<meta" in head.lower():
            pic = _web_page_picture(head, url)
            if pic:
                try:
                    t = web_tidy(http_get(pic, timeout=15, limit=WEB_MAX_BYTES))
                except Exception:
                    t = None
                if t:
                    return t, ""
            why = ("that's a web page, not a picture. Open the picture itself "
                   "(right-click it, Open Image in New Tab) and drag or copy that")
        else:
            why = "that isn't a picture Tagwell can read"
    if found.get("data"):
        t = web_tidy(found["data"])
        if t:
            return t, ""
    return b"", why or "there's no picture in it"


class WebPictureTaker(QObject):
    """Fetches a dropped or pasted picture in the background; done / failed arrive on
    the main thread. done(picture bytes, note), e.g. note "it's quite small (120 × 90)"."""
    done = Signal(bytes, str)
    failed = Signal(str)
    _live = set()

    def __init__(self, found, parent=None):
        super().__init__(parent)
        WebPictureTaker._live.add(self)
        self.found = found

    def start(self):
        def work():
            try:
                data, why = web_fetch(self.found)
            except Exception as e:  # never let a drop crash the app
                data, why = b"", str(e)
            if data:
                img = QImage.fromData(data)
                note = (f"it's quite small ({img.width()} × {img.height()})"
                        if not img.isNull() and max(img.width(), img.height()) < WEB_SMALL
                        else "")
                self.done.emit(data, note)
            else:
                self.failed.emit(why)
            WebPictureTaker._live.discard(self)
        threading.Thread(target=work, name="webpicture", daemon=True).start()
        return self


class _PictureDropArea(QLabel):
    def __init__(self, finder):
        super().__init__()
        self.finder = finder
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(340, 260)
        self.setWordWrap(True)
        self.setAcceptDrops(True)
        self.setObjectName("dropArea")
        self.setStyleSheet("QLabel#dropArea { border: 2px dashed palette(mid); "
                           "border-radius: 10px; padding: 12px; }")
        self.setText("Drag a picture here from your web browser or a folder\n\n"
                     "or right-click it in the browser, Copy Image, then press Paste")

    @staticmethod
    def _ok(mime):
        return web_accepts(mime) or (mime.hasUrls() and any(
            u.isLocalFile() for u in mime.urls()))

    def dragEnterEvent(self, ev):
        if self._ok(ev.mimeData()):
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev):
        self.dragEnterEvent(ev)

    def dropEvent(self, ev):
        md = ev.mimeData()
        found = web_extract(md) or {
            "urls": [], "data": b"",
            "files": [u.toLocalFile() for u in md.urls() if u.isLocalFile()]}
        ev.setDropAction(Qt.CopyAction)
        ev.accept()
        self.finder.take(found)


class PictureFinderDialog(QDialog):
    """Pick a picture from a web search in the person's browser (dragged or pasted in),
    or from a file."""

    def __init__(self, parent=None, words="", title="Search the web for a cover",
                 search_now=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        self.settings = tagwell_settings()
        self.data = b""
        self._taker = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        intro = QLabel("Search for the picture in your web browser, then drag the one you "
                       "want into the box below (or copy it and press Paste). You can also "
                       "choose a picture file.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        row = QHBoxLayout()
        self.words = QLineEdit(words)
        self.words.setPlaceholderText("What to search for")
        self.words.returnPressed.connect(self.search)
        self.engine = QComboBox()
        for key, (name, _url) in WEB_ENGINES.items():
            self.engine.addItem(name, key)
        self.engine.setCurrentIndex(max(0, self.engine.findData(web_engine(self.settings))))
        self.engine.setToolTip("Search engine (remembered for next time)")
        self.engine.currentIndexChanged.connect(
            lambda: self.settings.setValue("web/image_engine", self.engine.currentData()))
        search = QPushButton(icon("internet-web-browser", "edit-find"), "Search the Web")
        search.setAutoDefault(False)
        search.clicked.connect(self.search)
        row.addWidget(self.words, 1)
        row.addWidget(self.engine)
        row.addWidget(search)
        lay.addLayout(row)

        self.area = _PictureDropArea(self)
        lay.addWidget(self.area, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        btns = QHBoxLayout()
        paste = QPushButton(icon("edit-paste"), "Paste")
        paste.setAutoDefault(False)
        paste.setToolTip("A picture (or a picture's address) you copied (Ctrl+V)")
        paste.clicked.connect(self.paste)
        sc = QShortcut(QKeySequence.Paste, self)
        sc.activated.connect(self._paste_key)
        choose = QPushButton(icon("document-open"), "Choose a File…")
        choose.setAutoDefault(False)
        choose.clicked.connect(self.choose)
        btns.addWidget(paste)
        btns.addWidget(choose)
        btns.addStretch(1)
        self.box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_btn = self.box.button(QDialogButtonBox.Ok)
        self.ok_btn.setText("Use this Picture")
        self.ok_btn.setEnabled(False)
        self.box.accepted.connect(self.accept)
        self.box.rejected.connect(self.reject)
        btns.addWidget(self.box)
        lay.addLayout(btns)
        self.resize(660, 580)
        if search_now and words.strip():
            QTimer.singleShot(150, self.search)  # straight to the browser

    def search(self):
        words = self.words.text().strip()
        if not words:
            self._say("Type what to search for first, for example the artist and album.")
            return
        if open_web_search(words, self.engine.currentData()):
            self._say("Your web browser should now show the search. Drag the picture you "
                      "want into the box above.")
        else:
            self._say("Couldn't open your web browser.")

    def _paste_key(self):
        if self.focusWidget() is self.words:
            self.words.paste()  # Ctrl+V in the search box pastes text there
        else:
            self.paste()

    def paste(self):
        md = QGuiApplication.clipboard().mimeData()
        found = web_extract(md)
        if not found and md is not None and md.hasUrls():
            files = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if files:
                found = {"urls": [], "files": files, "data": b""}
        if found:
            self.take(found)
        else:
            self._say("There's no picture on the clipboard. In the browser, right-click the "
                      "picture and choose Copy Image (or Copy Image Link).")

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a picture", os.path.expanduser("~"),
            "Pictures (*.jpg *.jpeg *.png *.webp *.bmp *.gif);;All files (*)")
        if path:
            self.take({"urls": [], "files": [path], "data": b""})

    def take(self, found):
        self._say("Getting the picture…")
        self._taker = WebPictureTaker(found, self)
        self._taker.done.connect(self._got)
        self._taker.failed.connect(lambda why: self._say(f"Couldn't use that: {why}."))
        self._taker.start()

    def _got(self, data, note):
        pm = QPixmap()
        pm.loadFromData(data)
        self.data = data
        self.area.setPixmap(pm.scaled(self.area.contentsRect().size() * 0.94,
                                      Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self._say(f"Got it: {pm.width()} × {pm.height()}"
                  + (f", {note}" if note else "") + ".")
        self.ok_btn.setEnabled(True)
        self.ok_btn.setDefault(True)

    def _say(self, text):
        self.status.setText(text)

    @classmethod
    def ask(cls, parent=None, words="", title="Search the web for a cover", search_now=False):
        """Shows the window; the picture's bytes (JPEG or PNG), or None if cancelled.
        search_now: open the browser on the search straight away."""
        dlg = cls(parent, words, title, search_now)
        return dlg.data if dlg.exec() == QDialog.Accepted and dlg.data else None


# ---------------------------------------------------------------- online lyrics
LRCLIB = "https://lrclib.net/api"


def lrc_to_plain(synced):
    """Strip the [mm:ss.xx] time stamps from synced lyrics."""
    out = []
    for line in synced.replace("\r\n", "\n").split("\n"):
        if re.match(r"^\s*\[(ar|ti|al|by|offset|length|re|ve):", line, re.I):
            continue  # LRC header lines
        out.append(re.sub(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]", "", line).strip())
    return "\n".join(out).strip()


def _lrclib_pick(found):
    """Normalise one LRCLIB record to what the app needs."""
    synced = (found.get("syncedLyrics") or "").strip()
    plain = (found.get("plainLyrics") or "").strip() or (lrc_to_plain(synced) if synced else "")
    return {"synced": synced, "plain": plain,
            "instrumental": bool(found.get("instrumental")) and not (synced or plain),
            "match": f"{found.get('artistName', '')} – {found.get('trackName', '')}",
            "album": found.get("albumName") or ""}


def lookup_lyrics(artist, title, album, duration):
    """Find lyrics for one track on LRCLIB. Returns a dict, or None if not found."""
    # 1. Exact lookup: LRCLIB matches artist, title, album and length (±2 s).
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = str(int(round(duration)))
    try:
        data = json.loads(http_get(f"{LRCLIB}/get?" + urllib.parse.urlencode(params)))
        if isinstance(data, dict) and (data.get("syncedLyrics") or data.get("plainLyrics")
                                       or data.get("instrumental")):
            return _lrclib_pick(data)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # 2. Search by artist and title (the album name may differ), and take the
    #    closest length so a live or extended version isn't picked by mistake.
    q = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
    results = json.loads(http_get(f"{LRCLIB}/search?" + q))
    best, best_gap = None, None
    for r in results if isinstance(results, list) else []:
        if not (r.get("syncedLyrics") or r.get("plainLyrics") or r.get("instrumental")):
            continue
        gap = abs((r.get("duration") or 0) - duration) if duration and r.get("duration") else 0
        if duration and r.get("duration") and gap > 5:
            continue
        score = (gap, 0 if r.get("syncedLyrics") else 1)
        if best is None or score < best_gap:
            best, best_gap = r, score
    return _lrclib_pick(best) if best else None


class _LyricsBridge(QObject):
    result = Signal(int, int, object, str)  # run id, row, result (or None), error


class LyricsDialog(QDialog):
    """Look up lyrics for each selected file and let the user choose what to add."""

    # What each row can end up as. Rows in RETRYABLE can be looked up again.
    RETRYABLE = ("notfound", "error")

    def __init__(self, parent, files):
        super().__init__(parent)
        self.setWindowTitle("Download lyrics")
        self.resize(1060, 660)
        self.files = files
        self.found = {}   # row -> result dict
        self.state = {}   # row -> pending / found / instrumental / notfound / error / skipped
        self.note = {}    # row -> message shown for rows without lyrics
        self.batch_total = 0
        self.batch_done = 0
        self.run_id = 1
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.bridge = _LyricsBridge()
        self.bridge.result.connect(self._on_result)

        lay = QVBoxLayout(self)
        intro = QLabel("Lyrics come from <b>LRCLIB</b>, a free, community-run lyrics "
                       "database. Each file is looked up by its ARTIST, TITLE, ALBUM "
                       "and track length.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        opts = QHBoxLayout()
        self.synced_radio = QRadioButton("Time-synced lyrics when available")
        self.synced_radio.setToolTip("Lines carry time stamps like [01:23.45], so players "
                                     "that support it can highlight each line as it's sung")
        self.plain_radio = QRadioButton("Plain lyrics only")
        self.synced_radio.setChecked(True)
        for r in (self.synced_radio, self.plain_radio):
            r.toggled.connect(self._refresh_rows)
            opts.addWidget(r)
        opts.addStretch(1)
        self.replace_check = QCheckBox("Replace lyrics files already have")
        self.replace_check.toggled.connect(self._refresh_rows)
        opts.addWidget(self.replace_check)
        lay.addLayout(opts)

        split = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Looked up as", "Result"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.currentItemChanged.connect(lambda *_: self._show_preview())
        self.tree.itemDoubleClicked.connect(self._row_double_clicked)
        for row, f in enumerate(files):
            artist, title, album, dur = self._query(f)
            label = f"{artist} – {title}" if artist and title else "(missing ARTIST or TITLE)"
            it = QTreeWidgetItem([f.name, label, ""])
            it.setData(0, Qt.UserRole, row)
            it.setToolTip(0, f.path)
            self.tree.addTopLevelItem(it)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 130)
        self.tree.setColumnWidth(1, 210)
        split.addWidget(self.tree)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("Select a file to preview its lyrics")
        split.addWidget(self.preview)
        split.setSizes([720, 320])
        lay.addWidget(split, 1)

        # Outcome message + Try again
        msg_row = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        msg_row.addWidget(self.summary, 1)
        self.retry_btn = QPushButton("Try again")
        self.retry_btn.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.retry_btn.setToolTip("Look up again the files whose lyrics weren't found or "
                                  "couldn't be checked.\nTip: double-click one of those "
                                  "files to retry just that one.")
        self.retry_btn.clicked.connect(self.retry_failed)
        self.retry_btn.setVisible(False)
        msg_row.addWidget(self.retry_btn)
        lay.addLayout(msg_row)

        self.progress = QProgressBar()
        lay.addWidget(self.progress)

        bb = QDialogButtonBox()
        self.apply_btn = bb.addButton("Add lyrics", QDialogButtonBox.AcceptRole)
        self.apply_btn.setEnabled(False)
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self._start(range(len(files)))

    # ---- looking up
    @staticmethod
    def _query(f):
        first = lambda *keys: next((f.values(k)[0].strip() for k in keys
                                    if f.values(k) and f.values(k)[0].strip()), "")
        try:
            dur = float(f.audio.info.length)
        except Exception:
            dur = 0
        return first("ARTIST", "ALBUMARTIST"), first("TITLE"), first("ALBUM"), dur

    def _start(self, rows):
        rows = list(rows)
        if not rows:
            return
        self.batch_total, self.batch_done = len(rows), 0
        self.progress.setRange(0, len(rows))
        self.progress.setValue(0)
        self.progress.setFormat("Looking up %v of %m…")
        self.progress.setVisible(True)
        for row in rows:
            self.state[row] = "pending"
            self.found.pop(row, None)
            self.note.pop(row, None)
            item = self.tree.topLevelItem(row)
            item.setText(2, "Looking up…")
            item.setForeground(2, self.palette().text())
        self._update_summary()
        for row in rows:
            artist, title, album, dur = self._query(self.files[row])
            if not (artist and title):
                self.bridge.result.emit(self.run_id, row, None, "missing tags")
            else:
                self.pool.submit(self._lookup, self.run_id, row, artist, title, album, dur)

    def _lookup(self, run, row, artist, title, album, dur):
        try:
            self.bridge.result.emit(run, row, lookup_lyrics(artist, title, album, dur), "")
        except Exception as e:
            self.bridge.result.emit(run, row, None, _friendly_error(e))

    def _on_result(self, run, row, result, error):
        if run != self.run_id or self.state.get(row) != "pending":
            return
        self.batch_done += 1
        self.progress.setValue(self.batch_done)
        if result and (result["synced"] or result["plain"]):
            self.found[row] = result
            self.state[row] = "found"
        elif result and result["instrumental"]:
            self.state[row] = "instrumental"
            self.note[row] = "Instrumental — no lyrics"
        elif error == "missing tags":
            self.state[row] = "skipped"
            self.note[row] = "Skipped: needs ARTIST and TITLE tags"
        elif error:
            self.state[row] = "error"
            self.note[row] = f"⟳ Couldn't check: {error} — double-click to retry"
        else:
            self.state[row] = "notfound"
            self.note[row] = "No lyrics available"
        self._refresh_rows()

    def retry_failed(self):
        self._start(r for r, s in sorted(self.state.items()) if s in self.RETRYABLE)

    def _row_double_clicked(self, item, _col):
        row = item.data(0, Qt.UserRole)
        if self.state.get(row) in self.RETRYABLE and not self._busy():
            self._start([row])

    def _busy(self):
        return any(s == "pending" for s in self.state.values())

    # ---- choosing
    def chosen_text(self, row):
        r = self.found.get(row)
        if not r:
            return ""
        if self.synced_radio.isChecked() and r["synced"]:
            return r["synced"]
        return r["plain"]

    def to_apply(self):
        """[(file, lyrics)] for the files that will get lyrics."""
        out = []
        for row, f in enumerate(self.files):
            text = self.chosen_text(row)
            if not text:
                continue
            if f.values("LYRICS") and not self.replace_check.isChecked():
                continue
            out.append((f, text))
        return out

    def _refresh_rows(self):
        dim = QColor(128, 128, 128)
        bad = QColor(200, 60, 40)
        for row, f in enumerate(self.files):
            item = self.tree.topLevelItem(row)
            state = self.state.get(row)
            if state == "found":
                r = self.found[row]
                kind = ("Time-synced lyrics" if self.synced_radio.isChecked() and r["synced"]
                        else "Plain lyrics")
                lines = sum(1 for l in self.chosen_text(row).split("\n") if l.strip())
                status = f"✓ {kind} ({lines} line{'s' if lines != 1 else ''})"
                if f.values("LYRICS") and not self.replace_check.isChecked():
                    status += " — file already has lyrics, kept"
                item.setForeground(2, self.palette().text())
            elif row in self.note:
                status = self.note[row]
                item.setForeground(2, bad if state == "error" else dim)
            else:
                continue
            item.setText(2, status)
            item.setToolTip(2, status)
        n = len(self.to_apply())
        self.apply_btn.setText(f"Add lyrics to {n} file{'s' if n != 1 else ''}")
        self.apply_btn.setEnabled(n > 0)
        self._update_summary()
        self._show_preview()

    def _update_summary(self):
        busy = self._busy()
        count = lambda s: sum(1 for v in self.state.values() if v == s)
        found, missing, errors = count("found"), count("notfound"), count("error")
        instr, skipped = count("instrumental"), count("skipped")
        total = len(self.files)
        retry = missing + errors
        self.retry_btn.setVisible(not busy and retry > 0)
        self.retry_btn.setText(f"Try again ({retry})" if retry > 1 else "Try again")
        if busy:
            self.summary.setText("")
            return
        self.progress.setVisible(False)
        tracks = "this track" if total == 1 else "these tracks"
        if found == 0 and errors and not missing:
            msg = ("<b>⚠ Couldn't reach LRCLIB.</b> Check your internet connection, or if "
                   "LRCLIB is busy wait a moment, then press <b>Try again</b>.")
        elif found == 0 and not errors:
            msg = f"<b>No lyrics are available for {tracks}.</b>"
            if missing:
                msg += (" LRCLIB doesn't have them yet. You can check the ARTIST and "
                        "TITLE spelling and press <b>Try again</b>.")
            elif instr == total:
                msg += " LRCLIB lists " + ("it" if total == 1 else "them") + " as instrumental."
            elif skipped:
                msg += " Files need ARTIST and TITLE tags to be looked up."
        else:
            msg = f"<b>✓ Found lyrics for {found} of {total} file{'s' if total != 1 else ''}.</b>"
            parts = []
            if missing:
                parts.append(f"{missing} not available")
            if errors:
                parts.append(f"{errors} couldn't be checked")
            if instr:
                parts.append(f"{instr} instrumental")
            if skipped:
                parts.append(f"{skipped} skipped (missing ARTIST or TITLE)")
            if parts:
                joined = ", ".join(parts)
                msg += " " + joined[0].upper() + joined[1:] + "."
            if retry:
                msg += " Press <b>Try again</b> to retry those."
        self.summary.setText(msg)

    def _show_preview(self):
        item = self.tree.currentItem()
        if not item:
            return
        row = item.data(0, Qt.UserRole)
        text = self.chosen_text(row)
        r = self.found.get(row)
        head = ""
        if r:
            head = f"Matched: {r['match']}" + (f"  ({r['album']})" if r["album"] else "") + "\n\n"
        elif self.files[row].values("LYRICS"):
            head = "Lyrics already in the file:\n\n"
            text = self.files[row].values("LYRICS")[0]
        elif row in self.note and self.state.get(row) != "pending":
            head = self.note[row].replace("⟳ ", "")
        self.preview.setPlainText(head + text if (head or text) else "")

    def done(self, result):
        self.run_id += 1  # ignore anything still arriving
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().done(result)

def _friendly_error(e):
    if isinstance(e, urllib.error.HTTPError):
        return {404: "not found", 503: "service busy, try again shortly"}.get(
            e.code, f"HTTP error {e.code}")
    if isinstance(e, urllib.error.URLError):
        reason = e.reason
        if isinstance(reason, ssl.SSLError):
            return "secure connection failed"
        return "no connection" if "Name or service" in str(reason) or \
            "Temporary failure" in str(reason) else f"connection problem ({reason})"
    if isinstance(e, TimeoutError) or "timed out" in str(e):
        return "timed out"
    if isinstance(e, ValueError):
        return "unexpected reply"
    return str(e) or e.__class__.__name__


# ---------------------------------------------------------------- audio formats
# Every format is shown with the same tag names (TITLE, ARTIST, TRACKNUMBER, …),
# the names FLAC uses. Each format below translates to and from its own tags.
# Anything a format stores that Tagwell doesn't show (play counts, iTunes data,
# ratings, …) is left exactly as it was when saving.

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".m4b", ".mp4", ".ogg", ".oga", ".opus"}
FORMAT_NAMES = {"flac": "FLAC", "mp3": "MP3", "mp4": "M4A", "ogg": "OGG", "opus": "Opus"}

# Preferred display order for well-known tags (others follow, alphabetically).
TAG_ORDER = ["TITLE", "ARTIST", "ALBUM", "ALBUMARTIST", "TRACKNUMBER", "TOTALTRACKS",
             "DISCNUMBER", "TOTALDISCS", "DATE", "GENRE", "COMPOSER", "PERFORMER",
             "CONDUCTOR", "LYRICIST", "COMMENT", "DESCRIPTION", "GROUPING", "BPM",
             "COMPILATION", "LABEL", "COPYRIGHT", "ISRC", "ENCODER", "LYRICS"]


def _ordered(tags):
    rank = {k: i for i, k in enumerate(TAG_ORDER)}
    return sorted(tags, key=lambda kv: (rank.get(kv[0], len(rank)),
                                        kv[0] if kv[0] not in rank else ""))


def _split_pair(text):
    """'3/12' -> ('3', '12')."""
    a, _, b = str(text).partition("/")
    return a.strip(), b.strip()


def _image_info(data):
    img = QImage.fromData(data)
    return (img.width(), img.height(), img.depth()) if not img.isNull() else (0, 0, 0)


# ---- FLAC, OGG Vorbis, Opus (Vorbis comments: the same names Tagwell uses)
def _vc_read_pictures(tags):
    """Split out pictures stored as METADATA_BLOCK_PICTURE text tags."""
    kept, pics = [], []
    for k, v in tags:
        if k == "METADATA_BLOCK_PICTURE":
            try:
                pics.append(Pic.from_mutagen(Picture(base64.b64decode(v))))
                continue
            except Exception:
                pass
        kept.append((k, v))
    return kept, pics


class VorbisFormat:
    def read(self, af):
        tags = [(k.upper(), v) for k, v in (af.audio.tags or [])]
        tags, pics = _vc_read_pictures(tags)
        if af.kind == "flac":
            pics = [Pic.from_mutagen(p) for p in af.audio.pictures] + pics
        return tags, pics

    def write(self, af):
        audio = af.audio
        if audio.tags is None:
            audio.add_tags()
        audio.tags.clear()
        audio.tags.extend(af.tags)
        if af.kind == "flac":
            # FLAC has proper picture blocks
            audio.clear_pictures()
            for p in af.pics:
                audio.add_picture(p.to_mutagen())
        else:
            # OGG and Opus keep pictures in a text tag (the standard way)
            for p in af.pics:
                audio.tags.append(("METADATA_BLOCK_PICTURE",
                                   base64.b64encode(p.to_mutagen().write()).decode("ascii")))
        audio.save()


# ---- MP3 (ID3)
ID3_TEXT = {
    "TIT2": "TITLE", "TPE1": "ARTIST", "TALB": "ALBUM", "TPE2": "ALBUMARTIST",
    "TDRC": "DATE", "TCON": "GENRE", "TCOM": "COMPOSER", "TPE3": "CONDUCTOR",
    "TEXT": "LYRICIST", "TIT1": "GROUPING", "TBPM": "BPM", "TPUB": "LABEL",
    "TCOP": "COPYRIGHT", "TSRC": "ISRC", "TSSE": "ENCODER", "TENC": "ENCODEDBY",
    "TDOR": "ORIGINALDATE", "TIT3": "SUBTITLE", "TPE4": "REMIXER", "TMOO": "MOOD",
    "TCMP": "COMPILATION", "TSOP": "ARTISTSORT", "TSOA": "ALBUMSORT",
    "TSOT": "TITLESORT", "TSO2": "ALBUMARTISTSORT", "TLAN": "LANGUAGE",
}
ID3_FROM_KEY = {v: k for k, v in ID3_TEXT.items()}
# Frames that only exist in ID3v2.4. In a v2.3 file they are written as custom
# (TXXX) tags instead, so nothing is lost.
ID3_V24_ONLY = {"TMOO", "TSOA", "TSOP", "TSOT", "TSST", "TPRO", "TDRL", "TDTG", "TDEN"}


class ID3Format:
    def read(self, af):
        id3 = af.audio.tags
        tags, pics, af._consumed, af._txxx_case = [], [], [], {}
        af._id3_version = 3 if id3 is None else (id3.version[1] if id3.version[1] in (3, 4) else 4)
        if id3 is None:
            return tags, pics
        for key, frame in list(id3.items()):
            fid = frame.FrameID
            if fid == "TCON":
                tags += [("GENRE", g) for g in frame.genres]
            elif fid in ("TRCK", "TPOS"):
                n_key, t_key = (("TRACKNUMBER", "TOTALTRACKS") if fid == "TRCK"
                                else ("DISCNUMBER", "TOTALDISCS"))
                n, t = _split_pair(frame.text[0] if frame.text else "")
                if n:
                    tags.append((n_key, n))
                if t:
                    tags.append((t_key, t))
                if not n and not t:
                    tags.append((n_key, ""))  # kept blank on purpose
            elif fid in ID3_TEXT:
                tags += [(ID3_TEXT[fid], str(t)) for t in frame.text]
            elif fid == "TXXX":
                name = frame.desc.upper()
                af._txxx_case[name] = frame.desc
                tags += [(name, str(t)) for t in frame.text]
            elif fid == "COMM" and frame.desc in ("", "Comment"):
                tags += [("COMMENT", str(t)) for t in frame.text]
            elif fid == "USLT":
                tags.append(("LYRICS", frame.text))
            elif fid == "APIC":
                w, h, d = _image_info(frame.data)
                pics.append(Pic(frame.type, frame.mime or "image/jpeg", frame.desc, w, h, d,
                                bytes(frame.data)))
            else:
                continue  # not managed by Tagwell: left exactly as it is
            af._consumed.append(key)
        return _ordered(tags), pics

    def write(self, af):
        from mutagen import id3 as I
        audio = af.audio
        if audio.tags is None:
            audio.add_tags()
        id3 = audio.tags
        for key in af._consumed:
            id3.pop(key, None)
        grouped = {}
        for k, v in af.tags:
            # MP3 (ID3) tags can't hold an empty value: the library drops them.
            # A single space keeps a blank tag in the file and still looks blank.
            grouped.setdefault(k, []).append(v if v.strip() else " ")

        def pair(num_key, total_key):
            had_number = num_key in grouped
            n = (grouped.pop(num_key, [""]) or [""])[0].strip()
            t = (grouped.pop(total_key, [""]) or [""])[0].strip()
            if had_number and not n and not t:
                return " ", ""  # blank track/disc number kept on purpose
            return f"{n}/{t}" if n and t else n, t if t and not n else ""

        for fid, (nk, tk) in (("TRCK", ("TRACKNUMBER", "TOTALTRACKS")),
                              ("TPOS", ("DISCNUMBER", "TOTALDISCS"))):
            text, lone_total = pair(nk, tk)
            if text:
                id3.add(getattr(I, fid)(encoding=3, text=[text]))
            if lone_total:  # a total without a number has no standard place
                grouped[tk] = [lone_total]

        consumed = []
        for k, values in grouped.items():
            date_blank = (ID3_FROM_KEY.get(k) in ("TDRC", "TDOR")
                          and not any(v.strip() for v in values))
            if date_blank:
                # MP3 date fields only accept real dates; keep a blank one as a
                # custom tag (it moves to the date field once filled in)
                frame = I.TXXX(encoding=3, desc=k, text=values)
            elif k in ID3_FROM_KEY and not (af._id3_version == 3
                                            and ID3_FROM_KEY[k] in ID3_V24_ONLY):
                frame = getattr(I, ID3_FROM_KEY[k])(encoding=3, text=values)
            elif k == "COMMENT":
                frame = I.COMM(encoding=3, lang="eng", desc="", text=values)
            elif k == "LYRICS":
                frame = I.USLT(encoding=3, lang="eng", desc="", text="\n\n".join(values))
            else:
                frame = I.TXXX(encoding=3, desc=af._txxx_case.get(k, k), text=values)
            id3.add(frame)
            consumed.append(frame.HashKey)
        used = set()
        for p in af.pics:
            desc = p.desc
            while desc in used:  # each picture needs its own description in ID3
                desc += " "
            used.add(desc)
            frame = I.APIC(encoding=3, mime=p.mime, type=p.type, desc=desc, data=p.data)
            id3.add(frame)
            consumed.append(frame.HashKey)
        for fid in ("TRCK", "TPOS"):
            if fid in id3:
                consumed.append(fid)
        af._consumed = consumed
        audio.save(v2_version=af._id3_version)


# ---- M4A / MP4 (iTunes atoms)
MP4_TEXT = {
    "\xa9nam": "TITLE", "\xa9ART": "ARTIST", "\xa9alb": "ALBUM", "aART": "ALBUMARTIST",
    "\xa9day": "DATE", "\xa9gen": "GENRE", "\xa9wrt": "COMPOSER", "\xa9cmt": "COMMENT",
    "\xa9lyr": "LYRICS", "cprt": "COPYRIGHT", "\xa9grp": "GROUPING", "desc": "DESCRIPTION",
    "\xa9too": "ENCODER", "soar": "ARTISTSORT", "soal": "ALBUMSORT", "sonm": "TITLESORT",
    "soaa": "ALBUMARTISTSORT", "\xa9wrk": "WORK",
}
MP4_FROM_KEY = {v: k for k, v in MP4_TEXT.items()}
FREEFORM = "----:com.apple.iTunes:"


class MP4Format:
    def read(self, af):
        from mutagen.mp4 import MP4Cover
        from mutagen.id3 import TCON
        tags, pics, af._consumed, af._ff_case = [], [], [], {}
        mp4 = af.audio.tags
        if mp4 is None:
            return tags, pics
        for key, value in list(mp4.items()):
            if key in MP4_TEXT:
                tags += [(MP4_TEXT[key], str(v)) for v in value]
            elif key in ("trkn", "disk"):
                n_key, t_key = (("TRACKNUMBER", "TOTALTRACKS") if key == "trkn"
                                else ("DISCNUMBER", "TOTALDISCS"))
                n, t = (list(value[0]) + [0, 0])[:2] if value else (0, 0)
                if n:
                    tags.append((n_key, str(n)))
                if t:
                    tags.append((t_key, str(t)))
            elif key == "tmpo":
                tags += [("BPM", str(v)) for v in value]
            elif key == "cpil":
                tags.append(("COMPILATION", "1" if value else "0"))
            elif key == "gnre":
                genres = TCON.GENRES if hasattr(TCON, "GENRES") else []
                for g in value:
                    if 0 < g <= len(genres):
                        tags.append(("GENRE", genres[g - 1]))
            elif key.startswith(FREEFORM):
                name = key[len(FREEFORM):]
                af._ff_case[name.upper()] = name
                for v in value:
                    try:
                        tags.append((name.upper(), bytes(v).decode("utf-8")))
                    except UnicodeDecodeError:
                        break  # binary data: leave the atom alone
                else:
                    af._consumed.append(key)
                continue
            elif key == "covr":
                for i, c in enumerate(value):
                    png = getattr(c, "imageformat", None) == MP4Cover.FORMAT_PNG
                    w, h, d = _image_info(bytes(c))
                    pics.append(Pic(FRONT_COVER if i == 0 else 0,
                                    "image/png" if png else "image/jpeg", "", w, h, d, bytes(c)))
            else:
                continue  # not managed by Tagwell: left exactly as it is
            af._consumed.append(key)
        return _ordered(tags), pics

    def write(self, af):
        from mutagen.mp4 import MP4Cover, MP4FreeForm, AtomDataType
        audio = af.audio
        if audio.tags is None:
            audio.add_tags()
        mp4 = audio.tags
        for key in af._consumed:
            mp4.pop(key, None)
        grouped = {}
        for k, v in af.tags:
            grouped.setdefault(k, []).append(v)
        consumed = []

        def as_int(s):
            try:
                return int(str(s).strip().split("/")[0])
            except ValueError:
                return None

        for atom, (nk, tk) in (("trkn", ("TRACKNUMBER", "TOTALTRACKS")),
                               ("disk", ("DISCNUMBER", "TOTALDISCS"))):
            n = as_int((grouped.get(nk) or [""])[0]) if nk in grouped else 0
            t = as_int((grouped.get(tk) or [""])[0]) if tk in grouped else 0
            if n is not None and t is not None and (n or t):
                mp4[atom] = [(n, t)]
                consumed.append(atom)
                grouped.pop(nk, None)
                grouped.pop(tk, None)
            # anything that isn't a plain number is kept as a custom tag below

        bpm = grouped.get("BPM")
        if bpm and as_int(bpm[0]) is not None:
            mp4["tmpo"] = [as_int(b) for b in bpm if as_int(b) is not None]
            consumed.append("tmpo")
            del grouped["BPM"]
        comp = grouped.get("COMPILATION")
        if comp and comp[0].strip() in ("0", "1"):
            mp4["cpil"] = comp[0].strip() == "1"
            consumed.append("cpil")
            del grouped["COMPILATION"]

        for k, values in grouped.items():
            if k in MP4_FROM_KEY:
                atom = MP4_FROM_KEY[k]
                mp4[atom] = values
            else:
                atom = FREEFORM + af._ff_case.get(k, k)
                mp4[atom] = [MP4FreeForm(v.encode("utf-8"), dataformat=AtomDataType.UTF8)
                             for v in values]
            consumed.append(atom)
        if af.pics:
            # M4A covers have no picture types: the front cover goes first.
            ordered = sorted(af.pics, key=lambda p: p.type != FRONT_COVER)
            mp4["covr"] = [MP4Cover(p.data, imageformat=MP4Cover.FORMAT_PNG
                                    if p.mime == "image/png" else MP4Cover.FORMAT_JPEG)
                           for p in ordered]
            consumed.append("covr")
        af._consumed = consumed
        audio.save()


def open_audio(path):
    """Open a supported audio file; returns (mutagen object, kind, format handler)."""
    from mutagen import File as MutagenFile
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.oggflac import OggFLAC
    audio = MutagenFile(path, options=[FLAC, MP3, MP4, OggVorbis, OggOpus, OggFLAC])
    if audio is None:
        raise ValueError("not a supported audio file (FLAC, MP3, M4A, OGG or Opus)")
    if isinstance(audio, FLAC):
        return audio, "flac", VorbisFormat()
    if isinstance(audio, MP3):
        return audio, "mp3", ID3Format()
    if isinstance(audio, MP4):
        return audio, "mp4", MP4Format()
    if isinstance(audio, OggOpus):
        return audio, "opus", VorbisFormat()
    return audio, "ogg", VorbisFormat()


# ---------------------------------------------------------------- data model
class AudioFile:
    """One audio file: its tags as an ordered list of (KEY, value), plus its pictures."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.audio, self.kind, self.fmt = open_audio(self.path)
        tags, pics = self.fmt.read(self)
        self.original, self.tags = tags, list(tags)
        self.original_pics, self.pics = pics, list(pics)

    @property
    def format_name(self):
        return FORMAT_NAMES.get(self.kind, self.kind.upper())

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def dirty(self):
        return self.tags != self.original or self.pics != self.original_pics

    # ---- pictures
    def set_picture(self, pic):
        """Add a picture, replacing any existing picture of the same type."""
        out, placed = [], False
        for p in self.pics:
            if p.type == pic.type:
                if not placed:
                    out.append(pic)
                    placed = True
            else:
                out.append(p)
        if not placed:
            out.insert(0, pic) if pic.type == FRONT_COVER else out.append(pic)
        self.pics = out

    def remove_picture(self, pic):
        self.pics = [p for p in self.pics if p != pic]

    def retype_picture(self, pic, ptype):
        self.pics = [p.with_type(ptype) if p == pic else p for p in self.pics]

    def values(self, key):
        return [v for k, v in self.tags if k == key]

    def _map(self, key, i, fn):
        """Apply fn to the i-th entry of `key` (or every entry when i is None).
        fn returns the new (key, value) pair, or None to delete it."""
        out, n = [], 0
        for k, v in self.tags:
            if k == key:
                if i is None or n == i:
                    pair = fn(k, v)
                    if pair is not None:
                        out.append(pair)
                else:
                    out.append((k, v))
                n += 1
            else:
                out.append((k, v))
        self.tags = out

    def set_value(self, key, i, value):
        self._map(key, i, lambda k, v: (k, value))

    def replace_all(self, key, value):
        """Leave the tag with exactly one value (used for mixed rows)."""
        out, done = [], False
        for k, v in self.tags:
            if k == key:
                if not done:
                    out.append((key, value))
                    done = True
            else:
                out.append((k, v))
        if not done:
            out.append((key, value))
        self.tags = out

    def rename(self, key, new, i=None):
        self._map(key, i, lambda k, v: (new, v))

    def copy(self, src, dst, replace):
        """Copy this file's own value(s) of `src` into `dst`, exactly as they are.
        Returns False if the file has no `src` tag (nothing is changed)."""
        values = self.values(src)
        if not values:
            return False
        if replace:
            self.remove(dst)
        for v in values:
            self.tags.append((dst, v))
        return True

    def remove(self, key, i=None):
        self._map(key, i, lambda k, v: None)

    def add(self, key, value):
        self.tags.append((key, value))

    def save(self):
        self.fmt.write(self)
        self.original = list(self.tags)
        self.original_pics = list(self.pics)


FlacFile = AudioFile  # older name


# ---------------------------------------------------------------- delegates
class TagNameDelegate(QStyledItemDelegate):
    """In-place editor for the Tag column: suggests known tag names, blocks
    invalid characters and stores the name in uppercase."""

    def __init__(self, parent, names):
        super().__init__(parent)
        self.names = names  # callable returning the tag names to suggest

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.setValidator(QRegularExpressionValidator(TAG_NAME_REGEX, editor))
        completer = QCompleter(self.names(), editor)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        editor.setCompleter(completer)
        return editor

    def setModelData(self, editor, model, index):
        name = editor.text().strip().upper()
        if valid_tag_name(name):  # an empty name keeps the old one
            model.setData(index, name)


class ValueDelegate(QStyledItemDelegate):
    """Value column: a row whose value differs between files, or a preset tag
    that's missing, starts empty, and leaving it empty changes nothing."""

    def _mixed(self, index):
        meta = index.sibling(index.row(), 0).data(META)
        return bool(meta) and meta[0] in ("mixed", "missing")

    def setEditorData(self, editor, index):
        if self._mixed(index):
            editor.setText("")
            editor.setPlaceholderText("Type a value to set it in all selected files")
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        if self._mixed(index) and editor.text() == "":
            return
        super().setModelData(editor, model, index)


# ---------------------------------------------------------------- presets
NO_PRESET = "(no preset)"


def presets_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "tagwell", "presets.json")


class PresetStore:
    """Named lists of tag names, saved to ~/.config/tagwell/presets.json."""

    def __init__(self, path=None):
        self.path = path or presets_path()
        if path is None and not os.path.exists(self.path):
            # Presets saved before the app was renamed to Tagwell
            legacy = os.path.join(os.path.dirname(os.path.dirname(self.path)),
                                  "flac-tag-editor", "presets.json")
            if os.path.exists(legacy):
                try:
                    os.makedirs(os.path.dirname(self.path), exist_ok=True)
                    with open(legacy, "rb") as src, open(self.path, "wb") as dst:
                        dst.write(src.read())
                except OSError:
                    pass
        self.presets = {}   # name -> [TAG, ...]
        self.active = None  # name of the preset in use, remembered between runs
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.presets = {str(k): [str(t).upper() for t in v]
                            for k, v in data.get("presets", {}).items()}
            if data.get("active") in self.presets:
                self.active = data["active"]
        except (OSError, ValueError, AttributeError):
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"presets": self.presets, "active": self.active}, fh, indent=2)
        os.replace(tmp, self.path)


class PresetDialog(QDialog):
    """Create or edit a preset: a name plus an ordered list of tag names."""

    def __init__(self, parent, name, tags, suggestions, existing_names):
        super().__init__(parent)
        self.setWindowTitle("Edit preset" if name else "New preset")
        self.existing = set(existing_names)
        self.original_name = name
        self.resize(380, 460)
        lay = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("e.g. My album tags")
        form.addRow("Preset name:", self.name_edit)
        lay.addLayout(form)

        lay.addWidget(QLabel("Tags in this preset (drag to change the order):"))
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for t in tags:
            self.list.addItem(t)
        lay.addWidget(self.list)

        add = QHBoxLayout()
        self.add_combo = QComboBox()
        self.add_combo.setEditable(True)
        self.add_combo.addItems(suggestions)
        self.add_combo.setCurrentText("")
        self.add_combo.lineEdit().setPlaceholderText("Tag name")
        self.add_combo.lineEdit().setValidator(
            QRegularExpressionValidator(TAG_NAME_REGEX, self.add_combo))
        self.add_combo.lineEdit().returnPressed.connect(self._add)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add)
        rem_btn = QPushButton("Remove")
        rem_btn.clicked.connect(self._remove)
        add.addWidget(self.add_combo, 1)
        add.addWidget(add_btn)
        add.addWidget(rem_btn)
        lay.addLayout(add)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def tags(self):
        return [self.list.item(i).text() for i in range(self.list.count())]

    def _add(self):
        t = self.add_combo.currentText().strip().upper()
        if valid_tag_name(t) and t not in self.tags():
            self.list.addItem(t)
            self.list.scrollToBottom()
        self.add_combo.setCurrentText("")

    def _remove(self):
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))

    def _accept(self):
        name = self.name_edit.text().strip()
        if not name or name == NO_PRESET:
            QMessageBox.warning(self, "Preset", "Give the preset a name.")
            return
        if not self.tags():
            QMessageBox.warning(self, "Preset", "Add at least one tag to the preset.")
            return
        if name != self.original_name and name in self.existing:
            if QMessageBox.question(
                    self, "Preset", f"A preset called “{name}” already exists. "
                    "Replace it?") != QMessageBox.Yes:
                return
        self.accept()


# ---------------------------------------------------------------- window
VERSION = "1.1"

# ---------------------------------------------------------------- look and feel
APP_NAME = "Tagwell"
APP_AUTHOR = "bobwellapps"
APP_TAGLINE = "A simple tag editor for your music"
APP_LICENCE = "GPL-3.0-or-later"

# Breeze colours, used when the desktop doesn't provide its own colour scheme.
BREEZE = {
    "light": {"window": (239, 240, 241), "text": (35, 38, 41), "base": (255, 255, 255),
              "alt": (247, 247, 247), "button": (252, 252, 252), "btext": (35, 38, 41),
              "hl": (61, 174, 233), "hltext": (255, 255, 255), "link": (41, 128, 185),
              "tip": (247, 247, 247), "tiptext": (35, 38, 41)},
    "dark": {"window": (32, 35, 38), "text": (252, 252, 252), "base": (20, 22, 24),
             "alt": (29, 31, 34), "button": (41, 44, 48), "btext": (252, 252, 252),
             "hl": (61, 174, 233), "hltext": (255, 255, 255), "link": (29, 153, 243),
             "tip": (41, 44, 48), "tiptext": (252, 252, 252)},
}


def _read_kdeglobals():
    """Plasma's colour scheme, font and icon theme from ~/.config/kdeglobals."""
    import configparser
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = os.path.join(base, "kdeglobals")
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            cp.read_file(fh)
    except (OSError, configparser.Error):
        return None

    def rgb(section, key):
        try:
            parts = [int(x) for x in cp.get(section, key).split(",")[:3]]
            return tuple(parts) if len(parts) == 3 else None
        except (configparser.Error, ValueError):
            return None

    colours = {
        "window": rgb("Colors:Window", "BackgroundNormal"),
        "text": rgb("Colors:Window", "ForegroundNormal"),
        "base": rgb("Colors:View", "BackgroundNormal"),
        "alt": rgb("Colors:View", "BackgroundAlternate"),
        "button": rgb("Colors:Button", "BackgroundNormal"),
        "btext": rgb("Colors:Button", "ForegroundNormal"),
        "hl": rgb("Colors:Selection", "BackgroundNormal"),
        "hltext": rgb("Colors:Selection", "ForegroundNormal"),
        "link": rgb("Colors:View", "ForegroundLink"),
        "tip": rgb("Colors:Tooltip", "BackgroundNormal"),
        "tiptext": rgb("Colors:Tooltip", "ForegroundNormal"),
    }
    get = lambda s, k: cp.get(s, k, fallback=None)
    return {"colours": colours, "font": get("General", "font"),
            "icons": get("Icons", "Theme"), "scheme": get("General", "ColorScheme")}


def _is_dark(c):
    r, g, b = c
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


THEME = {}  # colours of the active theme, used by the style sheet and drawing code


def apply_theme(app):
    """Match the desktop: Plasma's colours, font and icons when available,
    otherwise Breeze light or dark following the system preference."""
    app.setStyle("Fusion")
    kde = _read_kdeglobals() if os.environ.get("TAGWELL_IGNORE_KDE") != "1" else None
    dark_pref = False
    try:
        dark_pref = app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:
        pass
    kc = (kde or {}).get("colours") or {}
    if kc.get("window"):
        base_scheme = BREEZE["dark" if _is_dark(kc["window"]) else "light"]
    else:
        base_scheme = BREEZE["dark" if dark_pref else "light"]
    c = {k: (kc.get(k) or v) for k, v in base_scheme.items()}
    dark = _is_dark(c["window"])
    c["border"] = _mix(c["window"], c["text"], 0.16)
    c["dim"] = _mix(c["window"], c["text"], 0.55)
    # Light: panels a touch brighter than the window. Dark: same as the window,
    # so lists and text boxes read as darker wells (as Breeze Dark does).
    c["panel"] = c["window"] if _is_dark(c["window"]) else _mix(c["window"], c["base"], 0.55)
    THEME.clear()
    THEME.update(c)
    THEME["dark"] = dark

    q = lambda t: QColor(*t)
    pal = QPalette()
    for group in (QPalette.Active, QPalette.Inactive):
        pal.setColor(group, QPalette.Window, q(c["window"]))
        pal.setColor(group, QPalette.WindowText, q(c["text"]))
        pal.setColor(group, QPalette.Base, q(c["base"]))
        pal.setColor(group, QPalette.AlternateBase, q(c["alt"]))
        pal.setColor(group, QPalette.Text, q(c["text"]))
        pal.setColor(group, QPalette.Button, q(c["button"]))
        pal.setColor(group, QPalette.ButtonText, q(c["btext"]))
        pal.setColor(group, QPalette.Highlight, q(c["hl"]))
        pal.setColor(group, QPalette.HighlightedText, q(c["hltext"]))
        pal.setColor(group, QPalette.Link, q(c["link"]))
        pal.setColor(group, QPalette.ToolTipBase, q(c["tip"]))
        pal.setColor(group, QPalette.ToolTipText, q(c["tiptext"]))
        pal.setColor(group, QPalette.PlaceholderText, q(c["dim"]))
        pal.setColor(group, QPalette.Light, q(_mix(c["button"], (255, 255, 255), 0.3)))
        pal.setColor(group, QPalette.Midlight, q(_mix(c["button"], c["text"], 0.08)))
        pal.setColor(group, QPalette.Mid, q(c["border"]))
        pal.setColor(group, QPalette.Dark, q(_mix(c["window"], (0, 0, 0), 0.3)))
        pal.setColor(group, QPalette.Shadow, q((0, 0, 0)))
    disabled = _mix(c["window"], c["text"], 0.4)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, q(disabled))
    for role, key in ((QPalette.Window, "window"), (QPalette.Base, "base"),
                      (QPalette.Button, "button")):
        pal.setColor(QPalette.Disabled, role, q(c[key]))
    pal.setColor(QPalette.Disabled, QPalette.Highlight, q(_mix(c["hl"], c["window"], 0.5)))
    app.setPalette(pal)

    if kde and kde.get("font"):
        f = QFont()
        if f.fromString(kde["font"]):
            app.setFont(f)

    # Look for icon themes where Linux desktops keep them. (Without KDE's own Qt
    # plugin, which an AppImage can't rely on, Qt only looks inside the app.)
    data_dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    home_data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    search = [os.path.join(home_data, "icons"), os.path.expanduser("~/.icons")]
    search += [os.path.join(d, "icons") for d in data_dirs if d]
    search += ["/usr/share/icons", "/usr/share/pixmaps"]
    if os.environ.get("SNAP"):  # icon theme bundled inside the snap
        search.insert(0, os.path.join(os.environ["SNAP"], "usr", "share", "icons"))
    current = QIcon.themeSearchPaths()
    QIcon.setThemeSearchPaths(current + [p for p in dict.fromkeys(search)
                                         if p not in current and os.path.isdir(p)])
    theme = (kde or {}).get("icons") or ("breeze-dark" if dark else "breeze")
    QIcon.setThemeName(theme)
    QIcon.setFallbackThemeName("breeze-dark" if dark else "breeze")
    app.setStyleSheet(_style_sheet(c))


def _style_sheet(c):
    css = lambda t: "rgb(%d,%d,%d)" % t
    return f"""
    QFrame#panel {{
        background: {css(c['panel'])};
        border: 1px solid {css(c['border'])};
        border-radius: 8px;
    }}
    QLabel#heading {{ font-weight: 600; }}
    QLabel#title {{ font-size: 13pt; font-weight: 600; }}
    QLabel#subtitle, QLabel#dim {{ color: {css(c['dim'])}; }}
    QLabel#pill {{
        color: {css(c['hltext'])};
        background: {css(c['hl'])};
        border-radius: 7px;
        padding: 1px 7px;
        font-size: 8pt;
        font-weight: 600;
    }}
    QListWidget, QTableWidget, QTreeWidget, QPlainTextEdit {{
        border: 1px solid {css(c['border'])};
        border-radius: 6px;
    }}
    QListWidget#inpanel, QTableWidget#inpanel {{ border: none; border-radius: 6px; }}
    QListWidget::item {{ padding: 3px 4px; }}
    QTableWidget {{ gridline-color: transparent; }}
    QHeaderView::section {{
        background: {css(c['panel'])};
        color: {css(c['dim'])};
        border: none;
        border-bottom: 1px solid {css(c['border'])};
        padding: 5px 8px;
        font-weight: 600;
    }}
    QLabel#cover {{ border: none; background: transparent; }}
    QLabel#cover[empty="true"] {{
        border: 2px dashed {css(c['border'])};
        color: {css(c['dim'])};
        background: transparent;
    }}
    QLabel#emptyTitle {{ font-size: 14pt; font-weight: 600; }}
    QPushButton#primary {{
        background: {css(c['hl'])};
        color: {css(c['hltext'])};
        border: 1px solid {css(_mix(c['hl'], (0, 0, 0), 0.15))};
        border-radius: 5px;
        padding: 5px 18px;
        font-weight: 600;
    }}
    QPushButton#primary:hover {{ background: {css(_mix(c['hl'], (255, 255, 255), 0.12))}; }}
    QPushButton#primary:disabled {{
        background: {css(_mix(c['window'], c['text'], 0.10))};
        color: {css(_mix(c['window'], c['text'], 0.45))};
        border-color: transparent;
    }}
    QToolBar {{ border: none; spacing: 2px; padding: 3px 6px; }}
    QToolBar QToolButton {{ padding: 4px 6px; border-radius: 5px; }}
    QToolButton#flat {{ border: none; padding: 3px; border-radius: 5px; }}
    QToolButton#flat:hover, QToolBar QToolButton:hover {{
        background: {css(_mix(c['window'], c['hl'], 0.18))};
    }}
    QToolButton#flat:disabled {{ color: {css(_mix(c['window'], c['text'], 0.4))}; }}
    QSplitter::handle {{ background: transparent; }}
    QStatusBar QLabel {{ color: {css(c['dim'])}; padding: 0 6px; }}
    """


def icon(*names, fallback=None):
    """An icon from the desktop's icon theme, with a built-in fallback."""
    for n in names:
        ic = QIcon.fromTheme(n)
        if not ic.isNull():
            return ic
    if fallback is not None:
        return QApplication.style().standardIcon(fallback)
    return QIcon()


_APP_ICON = None


def _icon_file():
    """tagwell.png next to the program (or inside the AppImage bundle)."""
    places = [getattr(sys, "_MEIPASS", ""), os.path.dirname(os.path.abspath(__file__)),
              os.path.dirname(os.path.abspath(sys.executable))]
    for d in places:
        p = os.path.join(d, "tagwell.png") if d else ""
        if p and os.path.isfile(p):
            return p
    return None


def make_app_icon():
    """Tagwell's icon: the illustrated well (tagwell.png). Keep tagwell.png next
    to tagwell.py; without it a simple drawn icon is used instead."""
    global _APP_ICON
    if _APP_ICON is not None:
        return _APP_ICON
    path = _icon_file()
    img = QImage(path) if path else QImage()
    if not img.isNull():
        ic = QIcon()
        for size in (16, 22, 24, 32, 48, 64, 96, 128, 256, 512):
            ic.addPixmap(QPixmap.fromImage(img.scaled(size, size, Qt.KeepAspectRatio,
                                                      Qt.SmoothTransformation)))
        _APP_ICON = ic
        return ic
    _APP_ICON = _drawn_app_icon()
    return _APP_ICON


def _drawn_app_icon():
    """Fallback icon: an orange tag with a music note on a blue tile."""
    ic = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        img = QImage(size, size, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        s = size / 256.0
        p.scale(s, s)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#2b6cb0"))
        p.drawRoundedRect(QRectF(16, 16, 224, 224), 44, 44)
        path = QPainterPath()
        path.moveTo(70, 70)
        path.lineTo(150, 70)
        path.lineTo(200, 128)
        path.lineTo(150, 186)
        path.lineTo(70, 186)
        path.closeSubpath()
        p.setBrush(QColor("#f6ad55"))
        p.drawPath(path)
        p.setBrush(QColor("#2b6cb0"))
        p.drawEllipse(QRectF(150, 118, 20, 20))
        pen = QPen(QColor("#1a202c"), 9, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(124, 90, 124, 152)
        p.drawLine(124, 90, 140, 104)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1a202c"))
        p.drawEllipse(QRectF(94, 140, 34, 26))
        p.end()
        ic.addPixmap(QPixmap.fromImage(img))
    return ic


class FileItemDelegate(QStyledItemDelegate):
    """File list rows: name, a small format badge, and a dot for unsaved changes."""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        fmt = index.data(Qt.UserRole + 10) or ""
        dirty = bool(index.data(Qt.UserRole + 11))
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        r = opt.rect.adjusted(8, 0, -8, 0)
        selected = bool(opt.state & QStyle.State_Selected)
        fg = opt.palette.color(QPalette.HighlightedText if selected else QPalette.Text)

        badge_font = QFont(opt.font)
        badge_font.setPointSizeF(max(6.5, opt.font.pointSizeF() * 0.72))
        badge_font.setBold(True)
        fm_b = QFontMetrics(badge_font)
        bw = fm_b.horizontalAdvance(fmt) + 10 if fmt else 0
        bh = fm_b.height() + 2
        if fmt:
            br = QRectF(r.right() - bw, r.center().y() - bh / 2 + 1, bw, bh)
            col = QColor(fg)
            col.setAlpha(40 if not selected else 70)
            painter.setPen(Qt.NoPen)
            painter.setBrush(col)
            painter.drawRoundedRect(br, bh / 2, bh / 2)
            painter.setPen(fg)
            painter.setFont(badge_font)
            painter.drawText(br, Qt.AlignCenter, fmt)

        x = r.left()
        if dirty:
            dot = QColor(opt.palette.color(QPalette.Highlight) if not selected else fg)
            painter.setPen(Qt.NoPen)
            painter.setBrush(dot)
            painter.drawEllipse(QRectF(x, r.center().y() - 3, 7, 7))
        x += 13
        painter.setFont(opt.font)
        painter.setPen(fg)
        avail = r.right() - bw - 8 - x
        elided = QFontMetrics(opt.font).elidedText(text, Qt.ElideMiddle, max(10, avail))
        painter.drawText(QRectF(x, r.top(), avail, r.height()),
                         Qt.AlignVCenter | Qt.AlignLeft, elided)
        painter.restore()

    def sizeHint(self, option, index):
        sh = super().sizeHint(option, index)
        return QSize(sh.width(), max(sh.height(), QFontMetrics(option.font).height() + 12))


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumWidth(500)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 16)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(16)
        logo = QLabel()
        logo.setPixmap(make_app_icon().pixmap(112, 112))
        logo.setAlignment(Qt.AlignTop)
        head.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        name = QLabel(APP_NAME)
        name.setStyleSheet("font-size: 20pt; font-weight: 700;")
        titles.addWidget(name)
        tag = QLabel(APP_TAGLINE)
        tag.setObjectName("subtitle")
        tag.setStyleSheet("font-size: 11pt;")
        titles.addWidget(tag)
        by = QLabel(f"Version {VERSION}  ·  by {APP_AUTHOR}")
        by.setObjectName("dim")
        titles.addWidget(by)
        titles.addStretch(1)
        head.addLayout(titles, 1)
        lay.addLayout(head)

        intro = QLabel(
            "Edit tags, cover art and lyrics in <b>FLAC, MP3, M4A (AAC/ALAC), OGG</b> and "
            "<b>Opus</b> files, one file or a whole album at a time. Add your own custom "
            "tags, keep presets of the tags you use, and fetch covers and lyrics online.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        def section(title):
            l = QLabel(title)
            l.setObjectName("heading")
            l.setContentsMargins(0, 8, 0, 0)
            lay.addWidget(l)

        def link(url, text):
            return f'<a href="{url}">{text}</a>'

        section("Online services")
        services = QLabel(
            f"Cover art from the {link('https://coverartarchive.org', 'Cover Art Archive')} "
            f"and {link('https://musicbrainz.org', 'MusicBrainz')}.<br>"
            f"Lyrics from {link('https://lrclib.net', 'LRCLIB')}.<br>"
            "<span>Covers and lyrics belong to their artists and publishers.</span>")
        services.setOpenExternalLinks(True)
        services.setWordWrap(True)
        lay.addWidget(services)

        section("Built with")
        built = QLabel(
            f"{link('https://doc.qt.io/qtforpython-6/', 'Qt for Python (PySide6)')} · "
            f"{link('https://mutagen.readthedocs.io', 'Mutagen')} · "
            f"{link('https://certifiio.readthedocs.io', 'certifi')}")
        built.setOpenExternalLinks(True)
        built.setWordWrap(True)
        lay.addWidget(built)

        section("Licence")
        lic = QLabel(f"{APP_NAME} is free software, released under the "
                     f"{link('https://www.gnu.org/licenses/gpl-3.0.html', 'GNU GPL')} "
                     f"({APP_LICENCE}).")
        lic.setOpenExternalLinks(True)
        lic.setWordWrap(True)
        lay.addWidget(lic)

        tech = QLabel(f"Settings: {os.path.dirname(presets_path())}<br>"
                      f"Qt {qVersion()} · Python {sys.version.split()[0]}")
        tech.setObjectName("dim")
        tech.setWordWrap(True)
        tech.setTextInteractionFlags(Qt.TextSelectableByMouse)
        tech.setContentsMargins(0, 10, 0, 0)
        lay.addWidget(tech)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


class TagTable(QTableWidget):
    """The tag list. Besides clicking (with Ctrl/Shift), tags can be selected by
    dragging over them, including starting in the empty space below the list."""

    def __init__(self, *a):
        super().__init__(*a)
        self._band = None       # selection rectangle shown while dragging from empty space
        self._band_start = None
        self._band_base = None  # selection to keep when Ctrl is held

    def mousePressEvent(self, e):
        if (e.button() == Qt.LeftButton and self.rowCount()
                and not self.indexAt(e.position().toPoint()).isValid()):
            self._band_start = e.position().toPoint()
            keep = bool(e.modifiers() & Qt.ControlModifier)
            self._band_base = QItemSelection(self.selectionModel().selection()) if keep else None
            if not keep:
                self.clearSelection()
            if self._band is None:
                self._band = QRubberBand(QRubberBand.Rectangle, self.viewport())
            self._band.setGeometry(QRect(self._band_start, QSize()))
            self._band.show()
            e.accept()
            return
        super().mousePressEvent(e)

    def _row_near(self, y):
        row = self.rowAt(y)
        if row >= 0:
            return row
        return self.rowCount() - 1 if y > 0 else 0

    def mouseMoveEvent(self, e):
        if self._band_start is not None:
            pos = e.position().toPoint()
            self._band.setGeometry(QRect(self._band_start, pos).normalized())
            top = QRect(self._band_start, pos).normalized()
            a, b = self._row_near(top.top()), self._row_near(top.bottom())
            last_row_bottom = self.visualRect(self.model().index(self.rowCount() - 1, 0)).bottom()
            sel = QItemSelection()
            if not (top.top() > last_row_bottom):  # the rectangle reaches the tags
                sel.select(self.model().index(min(a, b), 0),
                           self.model().index(max(a, b), self.columnCount() - 1))
            if self._band_base is not None:
                sel.merge(self._band_base, QItemSelectionModel.Select)
            self.selectionModel().select(sel, QItemSelectionModel.ClearAndSelect)
            if not sel.isEmpty():
                self.selectionModel().setCurrentIndex(
                    self.model().index(b, 0), QItemSelectionModel.NoUpdate)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._band_start is not None:
            self._band_start = None
            self._band.hide()
            e.accept()
            return
        super().mouseReleaseEvent(e)


class _Both:
    """Enable/disable several widgets or actions together."""

    def __init__(self, *items):
        self.items = items

    def setEnabled(self, on):
        for i in self.items:
            i.setEnabled(on)


def _flat_button(action, text_beside=True):
    b = QToolButton()
    b.setDefaultAction(action)
    b.setObjectName("flat")
    b.setAutoRaise(True)
    b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if text_beside else Qt.ToolButtonIconOnly)
    return b


def _mirror_button(action, text, icon_only=False):
    """A flat button with its own short label that runs `action` and follows
    whether it is enabled (menus keep the action's longer name)."""
    b = QToolButton()
    b.setObjectName("flat")
    b.setAutoRaise(True)
    b.setIcon(action.icon())
    b.setText(text)
    b.setToolTip(action.toolTip())
    b.setToolButtonStyle(Qt.ToolButtonIconOnly if icon_only else Qt.ToolButtonTextBesideIcon)
    b.clicked.connect(action.trigger)
    b.setEnabled(action.isEnabled())
    action.changed.connect(lambda: b.setEnabled(action.isEnabled()))
    return b


def _panel():
    frame = QFrame()
    frame.setObjectName("panel")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(10, 10, 10, 10)
    lay.setSpacing(8)
    return frame, lay


def _heading(text):
    l = QLabel(text)
    l.setObjectName("heading")
    return l


class TagEditor(QMainWindow):
    def __init__(self, paths=()):
        super().__init__()
        self.files = []  # AudioFile objects, same order as the file list
        self._auto_selected = False  # True while the app (not the user) chose the selection
        self._selecting = False
        self.settings = QSettings(os.path.join(os.path.dirname(presets_path()), "tagwell.conf"),
                                  QSettings.IniFormat)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_app_icon())
        self.resize(1060, 760)
        self.setAcceptDrops(True)
        self.store = PresetStore()

        self._build_actions()
        self._build_shortcuts()

        self.splitter = QSplitter()
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(8)
        # Left: files with the picture underneath. Right: tags.
        self.left_split = QSplitter(Qt.Vertical)
        self.left_split.setChildrenCollapsible(False)
        self.left_split.setHandleWidth(8)
        self.left_split.addWidget(self._build_files_panel())
        self.left_split.addWidget(self._build_cover_panel())
        self.left_split.setStretchFactor(0, 1)
        self.left_split.setStretchFactor(1, 1)
        self.left_split.setSizes([330, 400])
        self.splitter.addWidget(self.left_split)
        self.splitter.addWidget(self._build_tags_panel())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([310, 750])
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(8, 4, 8, 4)
        cl.addWidget(self.splitter)
        self.setCentralWidget(central)

        self.summary_label = QLabel()
        self.statusBar().addPermanentWidget(self.summary_label)
        about = QToolButton()
        about.setObjectName("flat")
        about.setAutoRaise(True)
        about.setIcon(self.act_about.icon())
        about.setText("About")
        about.setToolTip(self.act_about.text())
        about.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        about.clicked.connect(self.show_about)
        self.statusBar().addPermanentWidget(about)
        self.statusBar().setSizeGripEnabled(False)

        self._restore_settings()
        self._fill_preset_combo(self.store.active)
        if paths:
            self.add_paths(paths)
        self.refresh_table()
        self._update_labels()

    # ------------------------------------------------------------ actions
    def _action(self, text, slot, icon_names=(), fallback=None, shortcut=None, tip=None):
        a = QAction(text, self)
        if icon_names or fallback is not None:
            a.setIcon(icon(*icon_names, fallback=fallback))
        if shortcut:
            a.setShortcut(shortcut)
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        a.triggered.connect(slot)
        return a

    def _build_actions(self):
        SP = QStyle
        A = self._action
        self.act_add_files = A("Add Files…", self.add_files_dialog, ("document-open",),
                               SP.SP_DialogOpenButton, QKeySequence.Open,
                               "Add music files to the list")
        self.act_add_folder = A("Add Folder…", self.add_folder_dialog, ("folder-open", "folder"),
                                SP.SP_DirOpenIcon, QKeySequence("Ctrl+Shift+O"),
                                "Add every music file in a folder (and its subfolders)")
        self.act_remove_files = A("Remove from List", self.remove_files,
                                  ("list-remove",), SP.SP_DialogCancelButton, None,
                                  "Take the selected files off the list (files aren't deleted)")
        self.act_clear_files = A("Clear All", self.clear_files,
                                 ("edit-clear-all", "edit-clear-list", "edit-clear"),
                                 QStyle.SP_DialogResetButton, None,
                                 "Empty the file list (files aren't deleted)")
        self.act_save = A("Save", self.save_all, ("document-save",), SP.SP_DialogSaveButton,
                          QKeySequence.Save, "Save every changed file (Ctrl+S)")
        self.act_quit = A("Quit", self.close, ("application-exit",), None,
                          QKeySequence("Ctrl+Q"))

        self.act_add_tag = A("Add Tag", self._focus_add_tag, ("list-add",),
                             None, QKeySequence("Ctrl+T"), "Add a tag to the selected files")
        self.act_rename = A("Rename Tag…", self.rename_selected, ("edit-rename",), None,
                            QKeySequence(Qt.Key_F2), "Rename the selected tag (F2)")
        self.act_copy = A("Copy Tag To…", self.copy_selected, ("edit-copy",), None, None,
                          "Copy the selected tag into another tag. "
                          "Each file keeps its own value.")
        self.act_remove_tag = A("Remove Tag", self.remove_selected_tags, ("edit-delete",),
                                SP.SP_TrashIcon, None,
                                "Remove the selected tag from the selected files (Delete)")
        self.act_delete_key = A("Delete", self._delete_key, (), None, QKeySequence.Delete)
        self.addAction(self.act_delete_key)

        self.act_find_cover = A("Find Cover Online…", self.find_cover_online,
                                ("edit-find", "search"), SP.SP_FileDialogContentsView, None,
                                "Search the Cover Art Archive (MusicBrainz) and embed the "
                                "cover you pick as the front cover of every selected file")
        self.act_lyrics = A("Download Lyrics…", self.download_lyrics,
                            ("view-media-lyrics", "text-x-generic"), None, None,
                            "Look up lyrics for the selected files on LRCLIB and add them "
                            "as the LYRICS tag")
        self.act_import_pic = A("Import Picture…", self.import_picture_dialog,
                                ("document-import", "insert-image"), None, None,
                                "Embed an image in every selected file "
                                "(replaces a picture of the same type)")
        self.act_export_pic = A("Export Picture…", self.export_picture,
                                ("document-export", "document-save-as"), None, None,
                                "Save the picture shown to an image file")
        self.act_remove_pic = A("Remove Picture", self.remove_picture, ("edit-delete",),
                                SP.SP_TrashIcon, None, "Remove the picture shown")
        self.act_preset_new = A("New Preset…", self.new_preset, ("document-new",), None,
                                None, "Make a preset from the tags shown now")
        self.act_preset_edit = A("Edit Preset…", self.edit_preset, ("document-edit",),
                                 None, None, "Change the tags in the current preset")
        self.act_preset_delete = A("Delete Preset", self.delete_preset, ("edit-delete",),
                                   None, None, "Delete the current preset")
        self.act_about = A(f"About {APP_NAME}", self.show_about, ("help-about",),
                           SP.SP_MessageBoxInformation)
        self.act_about_qt = A("About Qt", lambda: QMessageBox.aboutQt(self))

        # Older code switches these on and off by these names
        self.rename_btn, self.copy_btn = self.act_rename, self.act_copy
        self.remove_btn, self.lyrics_btn = self.act_remove_tag, self.act_lyrics
        self.cover_import_btn, self.cover_export_btn = self.act_import_pic, self.act_export_pic
        self.cover_remove_btn, self.cover_find_btn = self.act_remove_pic, self.act_find_cover
        self.preset_edit_btn = self.act_preset_edit
        self.preset_delete_btn = self.act_preset_delete

    def _build_shortcuts(self):
        """No menu bar: keyboard shortcuts are attached to the window itself."""
        for a in (self.act_add_files, self.act_add_folder, self.act_save, self.act_quit,
                  self.act_add_tag, self.act_rename):
            a.setShortcutContext(Qt.WindowShortcut)
            self.addAction(a)

    # ------------------------------------------------------------ panels
    def _build_files_panel(self):
        frame, lay = _panel()
        head = QHBoxLayout()
        head.addWidget(_heading("Files"))
        self.count_label = QLabel()
        self.count_label.setObjectName("dim")
        head.addWidget(self.count_label)
        head.addStretch(1)
        for a in (self.act_add_files, self.act_add_folder, self.act_remove_files):
            head.addWidget(_flat_button(a, text_beside=False))
        head.addWidget(_mirror_button(self.act_clear_files, "Clear All"))
        lay.addLayout(head)

        self.file_list = QListWidget()
        self.file_list.setObjectName("inpanel")
        self.file_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.file_list.setItemDelegate(FileItemDelegate(self.file_list))
        self.file_list.setUniformItemSizes(True)
        self.file_list.itemSelectionChanged.connect(self._files_selected)
        self.file_list.setContextMenuPolicy(Qt.ActionsContextMenu)
        self.file_list.addAction(self.act_remove_files)
        self.file_list.addAction(self.act_clear_files)
        lay.addWidget(self.file_list, 1)
        hint = QLabel("Ctrl+A selects every file, to edit them together")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        return frame

    def _build_tags_panel(self):
        frame, lay = _panel()

        self.header_bar = QWidget()
        head = QHBoxLayout(self.header_bar)
        head.setContentsMargins(0, 0, 0, 0)
        self.title_label = QLabel()
        self.title_label.setObjectName("title")
        self.title_label.setTextFormat(Qt.PlainText)
        head.addWidget(self.title_label)
        self.format_pill = QLabel()
        self.format_pill.setObjectName("pill")
        head.addWidget(self.format_pill)
        head.addStretch(1)
        lay.addWidget(self.header_bar)
        self.info_label = QLabel()
        self.info_label.setObjectName("subtitle")
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.info_label)

        self.preset_bar = QWidget()
        pr = QHBoxLayout(self.preset_bar)
        pr.setContentsMargins(0, 0, 0, 0)
        pl = QLabel("Preset")
        pl.setObjectName("dim")
        pr.addWidget(pl)
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(180)
        self.preset_combo.setToolTip(
            "Pick a preset to show any of its tags a file is missing, ready to fill in")
        self.preset_combo.currentTextChanged.connect(self._preset_chosen)
        pr.addWidget(self.preset_combo, 1)
        for a, label in ((self.act_preset_new, "New…"), (self.act_preset_edit, "Edit…"),
                         (self.act_preset_delete, "Delete")):
            pr.addWidget(_mirror_button(a, label))
        lay.addWidget(self.preset_bar)

        self.tag_stack = QStackedWidget()
        self.tag_stack.addWidget(self._build_empty_state())

        self.table = TagTable(0, 2)
        self.table.setObjectName("inpanel")
        self.table.setHorizontalHeaderLabels(["Tag", "Value"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setHighlightSections(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(
            QFontMetrics(self.font()).height() + 14)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setItemDelegateForColumn(0, TagNameDelegate(self.table, self.known_tags))
        self.table.setItemDelegateForColumn(1, ValueDelegate(self.table))
        self.table.setToolTip("Double-click a tag name or value to edit it")
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellDoubleClicked.connect(self._cell_double_clicked)
        self.table.setContextMenuPolicy(Qt.ActionsContextMenu)
        for a in (self.act_rename, self.act_copy, self.act_remove_tag):
            self.table.addAction(a)
        self.tag_stack.addWidget(self.table)
        lay.addWidget(self.tag_stack, 1)

        self.edit_bar = QWidget()
        eb = QVBoxLayout(self.edit_bar)
        eb.setContentsMargins(0, 0, 0, 0)
        add_row = QHBoxLayout()
        self.tag_combo = QComboBox()
        self.tag_combo.setEditable(True)
        self.tag_combo.addItems(COMMON_TAGS)
        self.tag_combo.setCurrentText("")
        self.tag_combo.lineEdit().setPlaceholderText("New tag name")
        self.tag_combo.lineEdit().setValidator(
            QRegularExpressionValidator(TAG_NAME_REGEX, self.tag_combo))
        self.tag_combo.setMinimumWidth(180)
        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("Value")
        self.add_btn = QPushButton(icon("list-add"), "Add Tag")
        self.add_btn.clicked.connect(self.add_tag)
        self.tag_combo.lineEdit().returnPressed.connect(self.value_edit.setFocus)
        self.value_edit.returnPressed.connect(self.add_tag)
        add_row.addWidget(self.tag_combo, 2)
        add_row.addWidget(self.value_edit, 3)
        add_row.addWidget(self.add_btn)
        eb.addLayout(add_row)

        btns = QHBoxLayout()
        btns.setSpacing(2)
        for a, label in ((self.act_rename, "Rename…"), (self.act_copy, "Copy To…"),
                         (self.act_remove_tag, "Remove")):
            btns.addWidget(_mirror_button(a, label))
        btns.addSpacing(8)
        btns.addWidget(_mirror_button(self.act_lyrics, "Lyrics…"))
        btns.addStretch(1)
        save = QPushButton(icon("document-save", fallback=QStyle.SP_DialogSaveButton), "Save")
        save.setObjectName("primary")
        save.setToolTip("Save every changed file (Ctrl+S)")
        save.clicked.connect(self.save_all)
        self.save_btn = _Both(save, self.act_save)
        btns.addWidget(save)
        eb.addLayout(btns)
        lay.addWidget(self.edit_bar)
        return frame

    def _build_empty_state(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addStretch(2)
        logo = QLabel()
        logo.setPixmap(make_app_icon().pixmap(128, 128))
        logo.setAlignment(Qt.AlignCenter)
        v.addWidget(logo)
        t = QLabel("Add some music to get started")
        t.setObjectName("emptyTitle")
        t.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        s = QLabel("Drop files or folders anywhere on this window, or use the buttons below.\n"
                   "Tagwell edits FLAC, MP3, M4A, OGG and Opus files.")
        s.setObjectName("subtitle")
        s.setAlignment(Qt.AlignCenter)
        s.setWordWrap(True)
        v.addWidget(s)
        row = QHBoxLayout()
        row.addStretch(1)
        for a in (self.act_add_files, self.act_add_folder):
            b = QPushButton(a.icon(), a.text())
            b.clicked.connect(a.trigger)
            row.addWidget(b)
        row.addStretch(1)
        v.addSpacing(6)
        v.addLayout(row)
        v.addStretch(3)
        return w

    # ------------------------------------------------------------ cover art
    def _build_cover_panel(self):
        frame, v = _panel()
        head = QHBoxLayout()
        head.addWidget(_heading("Picture"))
        head.addStretch(1)
        lay_btns = QHBoxLayout()
        self.cover_prev = QToolButton()
        self.cover_prev.setObjectName("flat")
        self.cover_prev.setIcon(icon("go-previous", fallback=QStyle.SP_ArrowLeft))
        self.cover_prev.setToolTip("Previous picture")
        self.cover_next = QToolButton()
        self.cover_next.setObjectName("flat")
        self.cover_next.setIcon(icon("go-next", fallback=QStyle.SP_ArrowRight))
        self.cover_next.setToolTip("Next picture")
        self.cover_prev.clicked.connect(lambda: self._step_cover(-1))
        self.cover_next.clicked.connect(lambda: self._step_cover(1))
        head.addWidget(self.cover_prev)
        head.addWidget(self.cover_next)
        v.addLayout(head)

        self.cover_label = QLabel()
        self.cover_label.setObjectName("cover")
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setMinimumSize(200, 200)
        self.cover_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.cover_label.setWordWrap(True)
        self.cover_label.setToolTip("Double-click to view full size.\n"
                                    "Drop a picture here from a folder or your web browser to import it.")
        self.cover_label.installEventFilter(self)
        v.addWidget(self.cover_label, 1)

        self.cover_info = QLabel()
        self.cover_info.setObjectName("dim")
        self.cover_info.setAlignment(Qt.AlignCenter)
        self.cover_info.setWordWrap(True)
        v.addWidget(self.cover_info)

        self.cover_type = QComboBox()
        self.cover_type.addItems(PICTURE_TYPES)
        self.cover_type.setToolTip("Picture type. Changing it re-labels the picture shown; "
                                   "with no picture, it's the type used for Import.")
        self.cover_type.currentIndexChanged.connect(self._cover_type_changed)
        v.addWidget(self.cover_type)

        cb = QHBoxLayout()
        cb.setSpacing(2)
        for a, label in ((self.act_import_pic, "Import…"), (self.act_export_pic, "Export…"),
                         (self.act_remove_pic, "Remove")):
            cb.addWidget(_mirror_button(a, label))
        cb.addStretch(1)
        v.addLayout(cb)
        find = QPushButton(self.act_find_cover.icon(), "Find Cover Online…")
        find.setToolTip(self.act_find_cover.toolTip())
        find.clicked.connect(self.act_find_cover.trigger)
        self.act_find_cover.changed.connect(
            lambda: find.setEnabled(self.act_find_cover.isEnabled()))
        v.addWidget(find)

        self.cover_index = 0
        self.cover_pixmap = None
        return frame

    # ------------------------------------------------------------ small helpers
    def _focus_add_tag(self):
        if self.tag_combo.isEnabled():
            self.tag_combo.setFocus()
            self.tag_combo.lineEdit().selectAll()

    def show_about(self):
        AboutDialog(self).exec()

    def _restore_settings(self):
        geo = self.settings.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        state = self.settings.value("window/main_split")
        if state is not None:
            self.splitter.restoreState(state)
        state = self.settings.value("window/left_split")
        if state is not None:
            self.left_split.restoreState(state)

    def _save_settings(self):
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/main_split", self.splitter.saveState())
        self.settings.setValue("window/left_split", self.left_split.saveState())
        self.settings.sync()


    def _files_selected(self):
        if not self._selecting:
            self._auto_selected = False  # the user picked files themselves
        self.cover_index = 0
        self.refresh_table()

    def _shown_pics(self):
        """(pictures shared by every selected file, or None if they differ)."""
        files = self.selected_files()
        if not files:
            return None
        first = files[0].pics
        return list(first) if all(f.pics == first for f in files) else None

    def refresh_cover(self):
        files = self.selected_files()
        pics = self._shown_pics()
        self.cover_pixmap = None
        show_nav = False
        current = None

        if not files:
            self.cover_label.setText("No file selected")
            self.cover_info.setText("")
        elif pics is None:
            without = sum(1 for f in files if not f.pics)
            self.cover_label.setText("Different pictures in the selected files")
            self.cover_info.setText(
                f"{without} of {len(files)} have no picture" if without else
                "Import… sets one picture in all of them")
        elif not pics:
            self.cover_label.setText("No picture\n\nImport… or drop an image here")
            self.cover_info.setText("")
        else:
            self.cover_index = max(0, min(self.cover_index, len(pics) - 1))
            current = pics[self.cover_index]
            img = QImage.fromData(current.data)
            if img.isNull():
                self.cover_label.setText("Picture can't be displayed")
            else:
                self.cover_pixmap = QPixmap.fromImage(img)
                self._scale_cover()
            counter = f"{self.cover_index + 1} of {len(pics)} · " if len(pics) > 1 else ""
            self.cover_info.setText(counter + current.summary())
            show_nav = len(pics) > 1

        self.cover_prev.setVisible(show_nav)
        self.cover_next.setVisible(show_nav)
        self.cover_type.blockSignals(True)
        self.cover_type.setCurrentIndex(current.type if current and 0 <= current.type < len(
            PICTURE_TYPES) else FRONT_COVER if not current else 0)
        self.cover_type.blockSignals(False)
        self.cover_type.setEnabled(bool(files))
        self.cover_import_btn.setEnabled(bool(files))
        self.cover_find_btn.setEnabled(bool(files))
        self.cover_export_btn.setEnabled(current is not None)
        self.cover_remove_btn.setEnabled(current is not None or
                                         (pics is None and any(f.pics for f in files)))
        empty = self.cover_pixmap is None
        if self.cover_label.property("empty") != empty:
            self.cover_label.setProperty("empty", empty)
            self.cover_label.style().unpolish(self.cover_label)
            self.cover_label.style().polish(self.cover_label)

    def _scale_cover(self):
        if self.cover_pixmap is None:
            return
        size = self.cover_label.contentsRect().size()
        self.cover_label.setPixmap(self.cover_pixmap.scaled(
            size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def eventFilter(self, obj, event):
        if obj is self.cover_label:
            if event.type() == QEvent.Resize:
                self._scale_cover()
            elif event.type() == QEvent.MouseButtonDblClick and self.cover_pixmap:
                self._view_full_size()
        return super().eventFilter(obj, event)

    def _view_full_size(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Picture — " + self.cover_info.text())
        lay = QVBoxLayout(dlg)
        area = QScrollArea()
        lbl = QLabel()
        lbl.setPixmap(self.cover_pixmap)
        area.setWidget(lbl)
        area.setAlignment(Qt.AlignCenter)
        lay.addWidget(area)
        screen = self.screen().availableGeometry()
        dlg.resize(min(self.cover_pixmap.width() + 40, screen.width() - 80),
                   min(self.cover_pixmap.height() + 40, screen.height() - 80))
        dlg.exec()

    def _step_cover(self, d):
        pics = self._shown_pics() or []
        if pics:
            self.cover_index = (self.cover_index + d) % len(pics)
            self.refresh_cover()

    def _cover_type_changed(self, ptype):
        pics = self._shown_pics()
        if not pics:
            return  # no picture shown: the combo just sets the type for Import
        pic = pics[self.cover_index]
        if pic.type == ptype:
            return
        for f in self.selected_files():
            f.retype_picture(pic, ptype)
        self._pictures_changed(f"Picture type changed to {type_name(ptype)}")

    def _pictures_changed(self, message):
        self.refresh_cover()
        self._update_labels()
        self.statusBar().showMessage(message + " — not saved yet", 6000)

    def import_picture_dialog(self):
        files = self.selected_files()
        if not files:
            return
        start = os.path.dirname(files[0].path)
        path, _ = QFileDialog.getOpenFileName(
            self, "Import picture", start,
            "Images (*.jpg *.jpeg *.png *.gif *.bmp *.webp);;All files (*)")
        if path:
            self.import_picture(path)

    def import_picture(self, path):
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            QMessageBox.warning(self, "Import picture", f"Couldn't read that image:\n{e}")
            return
        self.import_picture_data(data)

    def import_picture_data(self, data, note=""):
        files = self.selected_files()
        if not files:
            QMessageBox.information(self, "Import picture",
                                    "Select the file(s) to add the picture to first.")
            return
        pics = self._shown_pics()
        ptype = self.cover_type.currentIndex()
        if pics or any(f.pics for f in files):
            # Files already have pictures: ask which type this one is.
            # A picture of the same type is replaced; other types are kept.
            default = pics[self.cover_index].type if pics else FRONT_COVER
            choice, ok = QInputDialog.getItem(
                self, "Import picture",
                "Picture type (replaces a picture of the same type):",
                PICTURE_TYPES, default if 0 <= default < len(PICTURE_TYPES) else 0, False)
            if not ok:
                return
            ptype = PICTURE_TYPES.index(choice)
        try:
            pic = Pic.from_bytes(data, ptype)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Import picture", f"Couldn't use that image:\n{e}")
            return
        for f in files:
            f.set_picture(pic)
        new = self._shown_pics() or []
        self.cover_index = next((i for i, p in enumerate(new) if p == pic), 0)
        n = len(files)
        self._pictures_changed(
            f"{type_name(ptype)} imported into {n} file{'s' if n != 1 else ''}"
            + (f" ({note})" if note else ""))

    def _common_value(self, files, *keys):
        """The first of `keys` the files have, using the most common value."""
        for key in keys:
            counts = {}
            for f in files:
                vals = f.values(key)
                if vals and vals[0].strip():
                    counts[vals[0].strip()] = counts.get(vals[0].strip(), 0) + 1
            if counts:
                return max(counts, key=counts.get)
        return ""

    def find_cover_online(self):
        files = self.selected_files()
        if not files:
            return
        artist = self._common_value(files, "ALBUMARTIST", "ARTIST", "ALBUM ARTIST")
        album = self._common_value(files, "ALBUM")
        dlg = CoverSearchDialog(self, artist, album)
        if dlg.exec() != QDialog.Accepted or not dlg.image_data:
            return
        try:
            pic = Pic.from_bytes(dlg.image_data, FRONT_COVER)
        except ValueError as e:
            QMessageBox.warning(self, "Find cover online", str(e))
            return
        replaced = sum(1 for f in files if any(p.type == FRONT_COVER for p in f.pics))
        for f in files:
            f.set_picture(pic)
        self.cover_index = 0
        n = len(files)
        msg = f"Front cover ({pic.summary()}) added to {n} file{'s' if n != 1 else ''}"
        if replaced:
            msg += f", replacing the old one in {replaced}"
        self._pictures_changed(msg)

    def export_picture(self):
        pics = self._shown_pics()
        if not pics:
            return
        pic = pics[self.cover_index]
        f = self.selected_files()[0]
        base = {FRONT_COVER: "cover", 4: "back"}.get(pic.type, "picture")
        dest, _ = QFileDialog.getSaveFileName(
            self, "Export picture", os.path.join(os.path.dirname(f.path), base + pic.ext),
            f"Image (*{pic.ext});;All files (*)")
        if not dest:
            return
        if not os.path.splitext(dest)[1]:
            dest += pic.ext
        try:
            with open(dest, "wb") as fh:
                fh.write(pic.data)  # the embedded bytes exactly, no re-encoding
        except OSError as e:
            QMessageBox.critical(self, "Export picture", str(e))
            return
        self.statusBar().showMessage(f"Picture saved to {dest}", 6000)

    def remove_picture(self):
        files = self.selected_files()
        pics = self._shown_pics()
        if pics:
            pic = pics[self.cover_index]
            for f in files:
                f.remove_picture(pic)
            self._pictures_changed(f"{type_name(pic.type)} removed")
        elif pics is None:
            if QMessageBox.question(
                    self, "Remove pictures",
                    "The selected files have different pictures.\n"
                    "Remove all pictures from all of them?") != QMessageBox.Yes:
                return
            for f in files:
                f.pics = []
            self._pictures_changed("All pictures removed")

    # ------------------------------------------------------------ files
    def add_files_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add music files", "",
            "Music files (*.flac *.mp3 *.m4a *.m4b *.mp4 *.ogg *.oga *.opus);;"
            "FLAC (*.flac);;MP3 (*.mp3);;M4A (*.m4a *.m4b *.mp4);;OGG / Opus (*.ogg *.oga *.opus);;"
            "All files (*)")
        if paths:
            self.add_paths(paths)

    def add_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(self, "Add a folder of music files")
        if folder:
            self.add_paths([folder])

    def add_paths(self, paths):
        """Add files and folders (folders are searched for supported music files)."""
        found = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, names in os.walk(p):
                    found += [os.path.join(root, n) for n in names
                              if os.path.splitext(n)[1].lower() in AUDIO_EXTS]
            else:
                found.append(p)
        found = sorted(dict.fromkeys(os.path.abspath(p) for p in found)) \
            if any(os.path.isdir(p) for p in paths) else \
            list(dict.fromkeys(os.path.abspath(p) for p in found))

        loaded = {f.path for f in self.files}
        errors, added = [], False
        blocked_by_snap = False
        # A folder the snap isn't allowed to list shows up as no files at all
        if os.environ.get("SNAP"):
            for p in paths:
                if os.path.isdir(p) and not os.access(p, os.R_OK | os.X_OK):
                    blocked_by_snap = True
                    errors.append(f"{p}: Permission denied")
        for path in found:
            if path in loaded:
                continue
            try:
                f = AudioFile(path)
            except (MutagenError, ValueError, OSError) as e:
                if os.environ.get("SNAP") and ("Permission denied" in str(e) or
                                               "Errno 13" in str(e)):
                    blocked_by_snap = True
                errors.append(f"{os.path.basename(path)}: {e}")
                continue
            self.files.append(f)
            loaded.add(path)
            added = True

        if added:
            self._rebuild_file_list()
        if errors:
            text = "\n".join(errors[:20]) + ("\n…" if len(errors) > 20 else "")
            if blocked_by_snap:
                text += ("\n\nTagwell (installed as a snap) isn't allowed into that "
                         "location yet. For music on other drives, run this once in a "
                         "terminal:\n\n    sudo snap connect tagwell:removable-media\n\n"
                         "Hidden folders (names starting with a dot) can't be opened by "
                         "snaps.")
            QMessageBox.warning(self, "Some files couldn't be opened", text)
        self._update_labels()

    def _rebuild_file_list(self):
        """Keep the list in folder/name order (track 2 before track 10), and keep
        what's selected. If the app picked the selection itself (nobody has
        clicked a file yet), move it to the first file in the list."""
        selected = set(map(id, self.selected_files()))
        self.files.sort(key=lambda f: natural_key(f.path))
        auto = self._auto_selected or not selected
        self._selecting = True
        self.file_list.blockSignals(True)
        self.file_list.clear()
        for f in self.files:
            item = QListWidgetItem(f.name)
            item.setToolTip(f.path)
            item.setData(Qt.UserRole + 10, f.format_name)
            item.setData(Qt.UserRole + 11, f.dirty)
            self.file_list.addItem(item)
            if not auto and id(f) in selected:
                item.setSelected(True)
        self.file_list.blockSignals(False)
        if auto and self.files:
            self.file_list.setCurrentRow(0)
        self._files_selected()
        self._selecting = False
        if auto and self.files:
            self._auto_selected = True

    def clear_files(self):
        if not self.files:
            return
        unsaved = sum(f.dirty for f in self.files)
        if unsaved:
            r = QMessageBox.question(
                self, "Clear All",
                f"{unsaved} file{'s have' if unsaved != 1 else ' has'} unsaved changes. "
                "Clear the list anyway and lose those changes?",
                QMessageBox.Discard | QMessageBox.Cancel)
            if r != QMessageBox.Discard:
                return
        self.file_list.blockSignals(True)
        self.file_list.clear()
        self.files = []
        self.file_list.blockSignals(False)
        self._auto_selected = False
        self.cover_index = 0
        self.refresh_table()
        self._update_labels()
        self.statusBar().showMessage("File list cleared", 4000)

    def remove_files(self):
        rows = sorted({self.file_list.row(i) for i in self.file_list.selectedItems()},
                      reverse=True)
        if not rows:
            return
        if any(self.files[r].dirty for r in rows):
            r = QMessageBox.question(
                self, "Unsaved changes",
                "Some of these files have unsaved changes. Remove them from the list anyway?",
                QMessageBox.Discard | QMessageBox.Cancel)
            if r != QMessageBox.Discard:
                return
        self.file_list.blockSignals(True)
        for r in rows:
            self.file_list.takeItem(r)
            del self.files[r]
        self.file_list.blockSignals(False)
        self.refresh_table()
        self._update_labels()

    def known_tags(self, exclude=()):
        """Tag names to offer in lists: every tag used in the loaded files
        (including custom ones), then the common names not already there."""
        used = sorted({k for f in self.files for k, _ in f.tags} |
                      {t for tags in self.store.presets.values() for t in tags})
        names = used + [t for t in COMMON_TAGS if t not in used]
        return [n for n in names if n not in exclude and n not in NEVER_SUGGEST]

    def _refresh_tag_combo(self):
        text = self.tag_combo.currentText()
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItems(self.known_tags())
        self.tag_combo.setCurrentText(text)
        self.tag_combo.blockSignals(False)

    def selected_files(self):
        rows = sorted(self.file_list.row(i) for i in self.file_list.selectedItems())
        return [self.files[r] for r in rows]

    def _handle_blank_tags(self):
        """Before saving, ask what to do with blank tags: ones added with no value,
        and preset tags left unfilled. Returns False if the user cancelled."""
        dirty = [f for f in self.files if f.dirty]
        selected = self.selected_files()
        # Preset tags still missing: from the files being saved and the files shown
        targets = list(dict.fromkeys(dirty + selected))
        preset = self.active_preset_tags()
        missing = {f: [k for k in preset if not f.values(k)] for f in targets}
        missing = {f: ks for f, ks in missing.items() if ks}
        # Tags in the files being saved that have no value. Blank tags already
        # saved in the file earlier (kept on purpose) aren't asked about again.
        blank = {f: [k for k, v in f.tags if not v.strip() and (k, v) not in f.original]
                 for f in dirty}
        blank = {f: ks for f, ks in blank.items() if ks}
        if not missing and not blank:
            return True

        names = []
        for ks in list(blank.values()) + list(missing.values()):
            names += [k for k in ks if k not in names]
        files_involved = set(blank) | set(missing)
        nfiles = len(files_involved)
        shown = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")

        box = QMessageBox(self)
        box.setWindowTitle("Blank tags")
        box.setIcon(QMessageBox.Question)
        box.setText(f"{len(names)} tag{'s are' if len(names) != 1 else ' is'} blank "
                    f"in {nfiles} file{'s' if nfiles != 1 else ''}:\n{shown}")
        box.setInformativeText(
            "Save them as empty tags (ready to fill in later), or leave them out "
            "and save everything else?")
        details = []
        for f in sorted(files_involved, key=lambda f: natural_key(f.path)):
            ks = blank.get(f, []) + [k for k in missing.get(f, []) if k not in blank.get(f, [])]
            details.append(f"{f.name}: {', '.join(ks)}")
        box.setDetailedText("\n".join(details))
        keep = box.addButton("Save Blank Tags", QMessageBox.AcceptRole)
        drop = box.addButton("Leave Them Out", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(drop)
        box.exec()
        clicked = box.clickedButton()
        if clicked == keep:
            for f, ks in missing.items():
                for k in ks:
                    f.add(k, "")
        elif clicked == drop:
            for f in blank:
                f.tags = [(k, v) for k, v in f.tags if v.strip() or (k, v) in f.original]
        else:
            return False
        return True

    def save_all(self):
        if not self._handle_blank_tags():
            self.statusBar().showMessage("Not saved", 3000)
            return
        dirty = [f for f in self.files if f.dirty]
        errors = []
        for f in dirty:
            try:
                f.save()
            except Exception as e:
                errors.append(f"{f.name}: {e}")
        if errors:
            QMessageBox.critical(self, "Some files couldn't be saved", "\n".join(errors))
        saved = len(dirty) - len(errors)
        if saved:
            self.statusBar().showMessage(
                f"Saved {saved} file{'s' if saved != 1 else ''}", 4000)
        elif not dirty and not errors:
            self.statusBar().showMessage("Nothing to save", 3000)
        self.refresh_table()
        self._update_labels()

    # ------------------------------------------------------------ table
    def refresh_table(self):
        files = self.selected_files()
        current = self.table.currentRow()

        self.table.blockSignals(True)
        self.table.setRowCount(0)
        keys = []
        for f in files:
            for k, _ in f.tags:
                if k not in keys:
                    keys.append(k)
        # With a preset in use, its tags come first in its order, including
        # ones none of the selected files have yet.
        preset = self.active_preset_tags() if files else []
        keys = preset + [k for k in keys if k not in preset]
        for key in keys:
            lists = [f.values(key) for f in files]
            if not any(lists):
                self._append_missing_row(key)
            elif all(l == lists[0] for l in lists):
                # Same in every selected file: one row per value
                for i, v in enumerate(lists[0]):
                    self._append_row(key, v, ("same", key, i))
            else:
                row = self._append_row(key, MIXED_TEXT, ("mixed", key))
                cell = self.table.item(row, 1)
                font = QFont(cell.font())
                font.setItalic(True)
                cell.setFont(font)
                cell.setForeground(QColor(128, 128, 128))
                lines = [f"{f.name}: {' | '.join(f.values(key)) or '(not set)'}"
                         for f in files[:25]]
                if len(files) > 25:
                    lines.append("…")
                cell.setToolTip("\n".join(lines))
                lacking = sum(1 for l in lists if not l)
                if key in preset and lacking:
                    tint = QColor(255, 196, 0, 60)
                    for c in (0, 1):
                        self.table.item(row, c).setBackground(tint)
                    cell.setText(f"<missing in {lacking} of {len(files)} files>")
                    cell.setToolTip(cell.toolTip() +
                                    "\n\nType a value to set it in all selected files")
        self.table.blockSignals(False)
        if self.table.rowCount():
            self.table.setCurrentCell(min(max(current, 0), self.table.rowCount() - 1), 0)
        self.refresh_cover()

        n = len(files)
        self.tag_stack.setCurrentIndex(1 if self.files else 0)
        for w in (self.header_bar, self.info_label, self.preset_bar, self.edit_bar):
            w.setVisible(bool(self.files))
        if n == 0:
            self.title_label.setText("No file selected" if self.files else "Welcome to Tagwell")
            self.format_pill.hide()
            self.info_label.setText("Select a file in the list to see its tags."
                                    if self.files else "")
        elif n == 1:
            self.title_label.setText(files[0].name)
            self.format_pill.setText(files[0].format_name)
            self.format_pill.show()
            self.info_label.setText(os.path.dirname(files[0].path))
            self.info_label.setToolTip(files[0].path)
        else:
            kinds = {}
            for f in files:
                kinds[f.format_name] = kinds.get(f.format_name, 0) + 1
            self.title_label.setText(f"{n} files selected")
            self.format_pill.setText(" · ".join(f"{c} {k}" for k, c in kinds.items())
                                     if len(kinds) > 1 else next(iter(kinds)))
            self.format_pill.show()
            self.info_label.setText(
                "Changes apply to all of them. Rows showing "
                f"{MIXED_TEXT.replace('<', '&lt;').replace('>', '&gt;')} differ between "
                "files; hover to see each file's value.")
            self.info_label.setToolTip("")
        for w in (self.tag_combo, self.value_edit, self.add_btn, self.act_add_tag,
                  self.rename_btn, self.copy_btn, self.remove_btn, self.lyrics_btn):
            w.setEnabled(n > 0)
        has_row = n > 0 and self.table.rowCount() > 0
        for a in (self.act_rename, self.act_copy, self.act_remove_tag):
            a.setEnabled(has_row)
        if hasattr(self, "summary_label"):
            self._update_labels()

    def _append_missing_row(self, key):
        """A preset tag that none of the selected files has: an empty,
        highlighted row. Nothing is written unless a value is typed in."""
        row = self._append_row(key, "", ("missing", key))
        name = self.table.item(row, 0)
        name.setFlags(name.flags() & ~Qt.ItemIsEditable)
        tint = QColor(255, 196, 0, 60)
        for c in (0, 1):
            self.table.item(row, c).setBackground(tint)
        name.setToolTip(f"{key} is in the preset but not in this file yet")
        self.table.item(row, 1).setToolTip("Double-click to fill in")
        return row

    def _append_row(self, key, value, meta):
        row = self.table.rowCount()
        self.table.insertRow(row)
        k = QTableWidgetItem(key)
        k.setData(META, meta)
        self.table.setItem(row, 0, k)
        cell = QTableWidgetItem(value)
        if is_long_text(key, value):
            # A table cell can't hold line breaks, so show a one-line summary
            # and edit the full text in its own window (double-click).
            cell = QTableWidgetItem(text_summary(value) if meta[0] == "same" else value)
            cell.setData(FULL_TEXT, value if meta[0] == "same" else "")
            cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
            cell.setToolTip("Double-click to view or edit the full text")
        self.table.setItem(row, 1, cell)
        return row

    def _cell_double_clicked(self, row, col):
        cell = self.table.item(row, 1)
        if col != 1 or cell is None or cell.data(FULL_TEXT) is None:
            return
        kind, key = self._row_meta(row)[:2]
        old = cell.data(FULL_TEXT)
        hint = ""
        if kind == "mixed":
            hint = "The selected files have different text here. " \
                   "Whatever you type replaces it in all of them."
        elif kind == "missing":
            hint = f"None of the selected files has {key} yet."
        dlg = TextEditDialog(self, key, old, hint)
        if dlg.exec() != QDialog.Accepted:
            return
        new = dlg.text()
        if new == old or (kind != "same" and new == ""):
            return
        cell.setText(new)  # goes through the normal edit path (_item_changed)

    def _row_meta(self, row):
        item = self.table.item(row, 0)
        return item.data(META) if item else None

    def _item_changed(self, item):
        meta = self._row_meta(item.row())
        if not meta:
            return
        kind, key = meta[0], meta[1]
        i = meta[2] if kind == "same" else None
        files = self.selected_files()
        text = item.text()
        if item.column() == 1:
            if kind == "missing":
                if text == "":
                    return
                for f in files:
                    f.add(key, text)
            elif kind == "mixed":
                if text in ("", MIXED_TEXT) or text.startswith("<missing in "):
                    return
                lacking = [f for f in files if not f.values(key)]
                targets = files
                if lacking and key in self.active_preset_tags():
                    box = QMessageBox(self)
                    box.setWindowTitle("Fill in missing tag")
                    box.setIcon(QMessageBox.Question)
                    box.setText(f"{len(lacking)} of the {len(files)} selected files "
                                f"don't have {key} yet.")
                    box.setInformativeText(
                        f"Fill in only those files, or set {key} to this value "
                        "in every selected file (replacing what the others have)?")
                    only = box.addButton("Only missing files", QMessageBox.AcceptRole)
                    box.addButton("All selected files", QMessageBox.AcceptRole)
                    cancel = box.addButton(QMessageBox.Cancel)
                    box.setDefaultButton(only)
                    box.exec()
                    if box.clickedButton() == cancel:
                        self._after_edit()
                        return
                    if box.clickedButton() == only:
                        targets = lacking
                for f in targets:
                    f.replace_all(key, text)
            else:
                for f in files:
                    f.set_value(key, i, text)
        else:
            if kind == "missing" or text == key or not valid_tag_name(text):
                return
            for f in files:
                f.rename(key, text, i)
        self._after_edit()

    def _after_edit(self):
        # Rebuild after Qt has finished closing the cell editor.
        QTimer.singleShot(0, lambda: (self.refresh_table(), self._update_labels()))

    def add_tag(self):
        files = self.selected_files()
        if not files:
            return
        name = self.tag_combo.currentText().strip().upper()
        if not valid_tag_name(name):
            QMessageBox.warning(self, "Invalid tag name",
                                "Enter a tag name using plain ASCII characters, without '='.")
            return
        value = self.value_edit.text()
        for f in files:
            f.add(name, value)
        self.tag_combo.setCurrentText("")
        self.value_edit.clear()
        self.refresh_table()
        self._update_labels()
        # Select the row that was just added
        for r in range(self.table.rowCount() - 1, -1, -1):
            if self.table.item(r, 0).text() == name:
                self.table.setCurrentCell(r, 1)
                break
        self.tag_combo.setFocus()

    def rename_selected(self):
        files = self.selected_files()
        row = self.table.currentRow()
        if not files or row < 0:
            QMessageBox.information(self, "Rename tag", "Select a tag to rename first.")
            return
        meta = self._row_meta(row)
        kind, old = meta[0], meta[1]
        if kind == "missing":
            QMessageBox.information(self, "Rename tag",
                                    f"{old} isn't in the file yet — fill in a value first.")
            return

        new, ok = QInputDialog.getItem(
            self, "Rename tag", f"New name for {old}\n(pick one or type your own):",
            self.known_tags(exclude=[old]), -1, True)
        if not ok:
            return
        new = new.strip().upper()
        if new == old:
            return
        if not valid_tag_name(new):
            QMessageBox.warning(self, "Invalid tag name",
                                "Use plain ASCII letters, numbers, spaces or symbols, and no '='.")
            return

        index = None  # None = every value of the tag
        if kind == "same" and len(files[0].values(old)) > 1:
            count = len(files[0].values(old))
            answer = QMessageBox.question(
                self, "Rename tag",
                f"There are {count} {old} entries. Rename all of them to {new}?\n\n"
                "Choose No to rename only the selected one.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.No:
                index = meta[2]
        for f in files:
            f.rename(old, new, index)
        self.refresh_table()
        self._update_labels()

    def copy_selected(self):
        files = self.selected_files()
        row = self.table.currentRow()
        if not files or row < 0:
            QMessageBox.information(self, "Copy tag", "Select a tag to copy first.")
            return
        kind, src = self._row_meta(row)[:2]
        if kind == "missing":
            QMessageBox.information(self, "Copy tag",
                                    f"{src} isn't in the file yet, so there's nothing to copy.")
            return

        dst, ok = QInputDialog.getItem(
            self, "Copy tag",
            f"Copy {src} into which tag? (pick one or type your own)\n"
            "Each file gets its own value, copied exactly.",
            self.known_tags(exclude=[src]), -1, True)
        if not ok:
            return
        dst = dst.strip().upper()
        if not valid_tag_name(dst):
            QMessageBox.warning(self, "Invalid tag name",
                                "Use plain ASCII letters, numbers, spaces or symbols, and no '='.")
            return
        if dst == src:
            QMessageBox.information(self, "Copy tag", "Choose a different tag name to copy into.")
            return

        sources = [f for f in files if f.values(src)]
        # If some files already have the destination tag, ask what to do with it.
        replace = True
        clash = [f for f in sources if f.values(dst)]
        if clash:
            box = QMessageBox(self)
            box.setWindowTitle("Copy tag")
            box.setIcon(QMessageBox.Question)
            box.setText(f"{len(clash)} of the files already have a {dst} tag.")
            box.setInformativeText(
                f"Replace their {dst} value with the copied one, "
                "or keep it and add the copied value alongside?")
            rep = box.addButton("Replace", QMessageBox.AcceptRole)
            box.addButton("Keep both", QMessageBox.AcceptRole)
            cancel = box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(rep)
            box.exec()
            if box.clickedButton() == cancel:
                return
            replace = box.clickedButton() == rep

        for f in sources:
            f.copy(src, dst, replace)
        self.refresh_table()
        self._update_labels()

        missing = len(files) - len(sources)
        msg = f"Copied {src} to {dst} in {len(sources)} file{'s' if len(sources) != 1 else ''}"
        if missing:
            msg += f" ({missing} skipped: no {src} tag)"
        self.statusBar().showMessage(msg + " — not saved yet", 8000)

    def download_lyrics(self):
        files = self.selected_files()
        if not files:
            return
        dlg = LyricsDialog(self, files)
        if dlg.exec() != QDialog.Accepted:
            return
        chosen = dlg.to_apply()
        for f, text in chosen:
            f.replace_all("LYRICS", text)
        self.refresh_table()
        self._update_labels()
        n = len(chosen)
        self.statusBar().showMessage(
            f"Lyrics added to {n} file{'s' if n != 1 else ''} — not saved yet", 8000)

    def remove_selected_tags(self):
        files = self.selected_files()
        rows = {i.row() for i in self.table.selectedIndexes()}
        metas = [self._row_meta(r) for r in rows]
        # Remove later entries first so the positions of earlier ones don't shift
        metas.sort(key=lambda m: -(m[2] if m[0] == "same" else -1))
        for m in metas:
            for f in files:
                f.remove(m[1], m[2] if m[0] == "same" else None)
        if metas:
            self.refresh_table()
            self._update_labels()

    # ------------------------------------------------------------ presets
    def active_preset_tags(self):
        return list(self.store.presets.get(self.store.active, []))

    def _fill_preset_combo(self, select=None):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem(NO_PRESET)
        self.preset_combo.addItems(sorted(self.store.presets, key=str.lower))
        self.preset_combo.setCurrentText(select if select in self.store.presets else NO_PRESET)
        self.preset_combo.blockSignals(False)
        has = self.store.active in self.store.presets
        self.preset_edit_btn.setEnabled(has)
        self.preset_delete_btn.setEnabled(has)

    def _preset_chosen(self, name):
        self.store.active = name if name in self.store.presets else None
        self._save_presets()
        self._fill_preset_combo(self.store.active)
        self.refresh_table()
        self._announce_missing()

    def _announce_missing(self):
        missing = sum(1 for r in range(self.table.rowCount())
                      if self._row_meta(r)[0] == "missing")
        if self.store.active and self.selected_files():
            self.statusBar().showMessage(
                f"{missing} tag{'s' if missing != 1 else ''} from the preset "
                f"{'are' if missing != 1 else 'is'} missing — highlighted, ready to fill in"
                if missing else "This file already has every tag in the preset", 6000)

    def _save_presets(self):
        try:
            self.store.save()
        except OSError as e:
            QMessageBox.warning(self, "Presets", f"Couldn't save presets:\n{e}")

    def _edit_preset_dialog(self, name, tags):
        dlg = PresetDialog(self, name, tags, self.known_tags(), self.store.presets)
        if dlg.exec() != QDialog.Accepted:
            return
        new_name = dlg.name_edit.text().strip()
        if name and new_name != name:
            self.store.presets.pop(name, None)
        self.store.presets[new_name] = dlg.tags()
        self.store.active = new_name
        self._save_presets()
        self._fill_preset_combo(new_name)
        self.refresh_table()
        self._announce_missing()

    def new_preset(self):
        # Start from the tags shown for the selected file(s)
        tags = []
        for r in range(self.table.rowCount()):
            k = self._row_meta(r)[1]
            if k not in tags:
                tags.append(k)
        self._edit_preset_dialog("", tags)

    def edit_preset(self):
        if self.store.active:
            self._edit_preset_dialog(self.store.active, self.active_preset_tags())

    def delete_preset(self):
        name = self.store.active
        if not name:
            return
        if QMessageBox.question(self, "Delete preset",
                                f"Delete the preset “{name}”?") != QMessageBox.Yes:
            return
        self.store.presets.pop(name, None)
        self.store.active = None
        self._save_presets()
        self._fill_preset_combo(None)
        self.refresh_table()

    def _delete_key(self):
        if self.file_list.hasFocus():
            self.remove_files()
        else:
            self.remove_selected_tags()

    # ------------------------------------------------------------ state
    def _update_labels(self):
        self._refresh_tag_combo()
        for r, f in enumerate(self.files):
            item = self.file_list.item(r)
            item.setData(Qt.UserRole + 10, f.format_name)
            item.setData(Qt.UserRole + 11, f.dirty)
            item.setToolTip(f.path + ("\n(unsaved changes)" if f.dirty else ""))
        unsaved = sum(f.dirty for f in self.files)
        self.setWindowTitle(("● " if unsaved else "") + APP_NAME +
                            (f" — {unsaved} unsaved file{'s' if unsaved != 1 else ''}"
                             if unsaved else ""))
        preset = self.active_preset_tags() if hasattr(self, "store") else []
        preset_gaps = any(not f.values(k) for f in self.selected_files() for k in preset)
        # Also available when only blank preset tags could be saved
        self.save_btn.setEnabled(unsaved > 0 or preset_gaps)
        n, sel = len(self.files), len(self.selected_files())
        self.count_label.setText(str(n) if n else "")
        bits = []
        if n:
            bits.append(f"{n} file{'s' if n != 1 else ''}")
            if sel:
                bits.append(f"{sel} selected")
            if unsaved:
                bits.append(f"{unsaved} unsaved")
        self.summary_label.setText("  ·  ".join(bits))
        self.act_remove_files.setEnabled(sel > 0)
        self.act_clear_files.setEnabled(n > 0)
        self.file_list.viewport().update()

    def closeEvent(self, event):
        self._close_event(event)
        if event.isAccepted():
            self._save_settings()

    def _close_event(self, event):
        if not any(f.dirty for f in self.files):
            event.accept()
            return
        r = QMessageBox.question(
            self, "Unsaved changes", "Save changes before closing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            self.save_all()
            if any(f.dirty for f in self.files):
                event.ignore()
                return
            event.accept()
        elif r == QMessageBox.Discard:
            event.accept()
        else:
            event.ignore()

    # ------------------------------------------------------------ drag & drop
    # Dolphin and other KDE apps hand files to sandboxed apps (snaps, Flatpaks)
    # through the desktop's file-transfer portal instead of a plain file list.
    PORTAL_MIME = "application/vnd.portal.filetransfer"

    def _drop_debug(self, what, md):
        if os.environ.get("TAGWELL_DEBUG"):
            print(f"[drop] {what}: formats={md.formats()} "
                  f"urls={[u.toString() for u in md.urls()]}", file=sys.stderr, flush=True)

    def _can_take(self, md):
        if any(md.hasFormat(f) for f in _OWN_DRAG_FORMATS):
            return False
        return md.hasUrls() or md.hasFormat(self.PORTAL_MIME) or web_accepts(md) or (
            md.hasText() and md.text().strip().startswith(("file://", "/")))

    def dragEnterEvent(self, event):
        md = event.mimeData()
        self._drop_debug("enter", md)
        if self._can_take(md):
            # Always a copy: the dragged files stay where they are
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._can_take(event.mimeData()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def _portal_paths(self, md):
        """Files handed over through the file-transfer portal."""
        try:
            key = bytes(md.data(self.PORTAL_MIME)).decode("utf-8", "replace").strip("\0 \n")
            if not key:
                return []
            from PySide6.QtDBus import QDBusConnection, QDBusMessage
            msg = QDBusMessage.createMethodCall(
                "org.freedesktop.portal.Documents", "/org/freedesktop/portal/documents",
                "org.freedesktop.portal.FileTransfer", "RetrieveFiles")
            msg.setArguments([key, {}])
            reply = QDBusConnection.sessionBus().call(msg, timeout=10000)
            if reply.type() == QDBusMessage.ErrorMessage:
                self._drop_debug("portal error " + reply.errorMessage(), md)
                return []
            args = reply.arguments()
            return [str(p) for p in (args[0] if args else [])]
        except Exception as e:  # never let a drop crash the app
            if os.environ.get("TAGWELL_DEBUG"):
                print(f"[drop] portal failed: {e}", file=sys.stderr, flush=True)
            return []

    def dropEvent(self, event):
        md = event.mimeData()
        self._drop_debug("drop", md)
        paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        if not paths and md.hasFormat(self.PORTAL_MIME):
            paths = self._portal_paths(md)
        if not paths and md.hasText():
            for line in md.text().splitlines():
                line = line.strip()
                if line.startswith("file://"):
                    paths.append(QUrl(line).toLocalFile())
                elif line.startswith("/"):
                    paths.append(line)
        event.setDropAction(Qt.CopyAction)
        event.accept()
        if not paths and web_accepts(md):
            self._take_web_picture(web_extract(md))
            return
        if not paths:
            QMessageBox.information(
                self, "Drag and drop",
                "Tagwell couldn't get those files from the drag. Use Add Files… or "
                "Add Folder… instead.")
            return
        images = [p for p in paths if os.path.splitext(p)[1].lower() in IMAGE_EXTS]
        others = [p for p in paths if p not in images]
        if others:
            self.add_paths(others)
        if images:
            self.import_picture(images[0])

    def _take_web_picture(self, found):
        """A picture dragged in from a web browser: download it, then import it."""
        if not self.selected_files():
            QMessageBox.information(self, "Import picture",
                                    "Select the file(s) to add the picture to first.")
            return
        self.statusBar().showMessage("Getting the picture…")
        taker = WebPictureTaker(found, self)

        def done(data, note):
            self.statusBar().clearMessage()
            self.import_picture_data(data, note)

        def failed(why):
            self.statusBar().clearMessage()
            QMessageBox.warning(self, "Import picture", f"Couldn't use that: {why}.")
        taker.done.connect(done)
        taker.failed.connect(failed)
        taker.start()


# ---------------------------------------------------------------- single instance
# Opening several files from a file manager often starts the app once per file.
# The first copy becomes the window; every later copy hands its files to it and
# quits, so all the files end up in one window.
INSTANCE_NAME = f"tagwell-{os.getuid()}"


def _send_to_running(paths):
    """Give `paths` to an already-running window. True if one took them."""
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_NAME)
    if not sock.waitForConnected(500):
        return False
    data = json.dumps(paths).encode("utf-8")
    if sock.write(data) != len(data):
        return False
    sock.flush()
    while sock.bytesToWrite() > 0:  # rarely needed: flush usually sends it all
        if not sock.waitForBytesWritten(3000):
            return False
    ok = True
    sock.disconnectFromServer()
    if sock.state() != QLocalSocket.UnconnectedState:
        sock.waitForDisconnected(1000)
    return ok


class _InstanceServer(QLocalServer):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.newConnection.connect(self._accept)

    def _accept(self):
        while self.hasPendingConnections():
            sock = self.nextPendingConnection()
            sock.setProperty("buf", b"")
            sock.readyRead.connect(lambda s=sock: s.setProperty(
                "buf", s.property("buf") + bytes(s.readAll())))
            sock.disconnected.connect(lambda s=sock: self._received(s))

    def _received(self, sock):
        data = sock.property("buf") + bytes(sock.readAll())
        sock.deleteLater()
        try:
            paths = [str(p) for p in json.loads(data.decode("utf-8"))]
        except (ValueError, TypeError):
            return
        w = self.window
        if paths:
            w.add_paths(paths)
        if w.isMinimized():
            w.showNormal()
        w.raise_()
        w.activateWindow()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName("tagwell")
    app.setWindowIcon(make_app_icon())
    apply_theme(app)
    paths = [os.path.abspath(p) for p in sys.argv[1:] if not p.startswith("-")]

    # Copies started at the same moment take turns here, so exactly one of
    # them becomes the window and the rest find it.
    lock = QLockFile(os.path.join(QDir.tempPath(), INSTANCE_NAME + ".lock"))
    lock.setStaleLockTime(10000)
    lock.tryLock(15000)
    try:
        if _send_to_running(paths):
            return 0
        w = TagEditor(paths)
        server = _InstanceServer(w)
        QLocalServer.removeServer(INSTANCE_NAME)  # left over from a crash
        server.listen(INSTANCE_NAME)
    finally:
        if lock.isLocked():
            lock.unlock()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
