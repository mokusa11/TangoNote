from __future__ import annotations

import argparse
import base64
import binascii
import http.client
import html
from html.parser import HTMLParser
import ipaddress
import json
import mimetypes
import re
import secrets
import socket
import ssl
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import (
    SplitResult,
    parse_qs,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlsplit,
)

from highlight_manager import HighlightManager
from sync_manager import SyncManager, default_sync_state_path
from note_store import (
    DEFAULT_GROUP as MYNOTE_DEFAULT_GROUP,
    MAX_BODY_LENGTH as MYNOTE_MAX_BODY_LENGTH,
    MAX_GROUP_LENGTH as MYNOTE_MAX_GROUP_LENGTH,
    MAX_TERM_LENGTH as MYNOTE_MAX_TERM_LENGTH,
    NoteStore as MyNoteStore,
    StoreError as MyNoteStoreError,
)


PROJECT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_DIR / "网页"
DATA_DIR = PROJECT_DIR / "数据"
DEFAULT_DB_PATH = DATA_DIR / "notes.sqlite3"
DEFAULT_NOTES_DIR = Path(__file__).resolve().parent / "note"
DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"
DEFAULT_PORT = 8766
MAX_REQUEST_BYTES = 30_000_000
MAX_PASTE_IMAGE_BYTES = 20_000_000
MAX_PASTE_IMAGE_COUNT = 5
MAX_PASTE_IMAGE_REDIRECTS = 2
PASTE_IMAGE_FETCH_TIMEOUT = 5
PASTE_IMAGE_FETCH_BUDGET = 20
MAX_TERM_LENGTH = MYNOTE_MAX_TERM_LENGTH
MAX_GROUP_LENGTH = MYNOTE_MAX_GROUP_LENGTH
MAX_BODY_LENGTH = MYNOTE_MAX_BODY_LENGTH
DEFAULT_GROUP = MYNOTE_DEFAULT_GROUP
_UNSET_SETTING = object()
LINK_PATTERN = re.compile(r"@@@LINK=([^\r\n<>\[]+)")
NOTE_LOOKUP_LINK_PATTERN = re.compile(
    r"\[http://(?:127\.0\.0\.1|localhost)(?::\d+)?/lookup\?([^\]\r\n]+)\]",
    re.IGNORECASE,
)
HTML_TAG_PATTERN = re.compile(
    r"</?(?:p|br|strong|b|em|i|u|s|del|ins|small|ul|ol|li|dl|dt|dd|"
    r"blockquote|code|pre|h[1-6]|hr|sub|sup|table|thead|tbody|tfoot|"
    r"tr|th|td|a|div|span|font|center|img|audio|video|source|figure|"
    r"figcaption|details|summary)\b",
    re.IGNORECASE,
)
ALLOWED_HTML_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s", "del", "ins", "small",
    "ul", "ol", "li", "dl", "dt", "dd", "blockquote", "code", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "sub", "sup", "table",
    "thead", "tbody", "tfoot", "tr", "th", "td", "a", "div", "span",
    "font", "center", "img", "audio", "video", "source", "figure", "figcaption",
    "details", "summary",
}
VOID_HTML_TAGS = {"br", "hr", "img", "source"}
BLOCKED_HTML_TAGS = {"script", "style", "iframe", "object", "svg", "math", "template"}
PASTE_NH3_TAGS = {
    "p", "div", "span", "br", "b", "strong", "i", "em", "u", "s", "strike",
    "ul", "ol", "li", "dl", "dt", "dd", "blockquote", "pre", "code", "a",
    "img", "video", "audio", "source", "figure", "figcaption", "table",
    "thead", "tbody", "tfoot", "tr", "th", "td",
}
PASTE_SANITIZER_TAGS = PASTE_NH3_TAGS | {
    "h1", "h2", "h3", "h4", "h5", "h6", "hr",
}
PASTE_NH3_ATTRIBUTES = {
    "*": {"title"},
    "a": {"href", "target", "rel", "title"},
    "img": {"src", "alt", "title", "width", "height", "style"},
    "video": {"src", "poster", "controls", "width", "height", "style"},
    "audio": {"src", "controls"},
    "source": {"src", "type"},
    "table": {"border", "cellpadding", "cellspacing", "style"},
    "tr": {"style"},
    "th": {"colspan", "rowspan", "style"},
    "td": {"colspan", "rowspan", "style"},
}
PASTE_SANITIZER_ATTRIBUTES = {
    "a": {"href", "target", "rel"},
    "img": {"src", "alt", "title", "width", "height", "style"},
    "video": {"src", "poster", "controls", "width", "height", "style"},
    "audio": {"src", "controls"},
    "source": {"src", "type"},
    "table": {"border", "cellpadding", "cellspacing", "style"},
    "tr": {"style"},
    "th": {"colspan", "rowspan", "style"},
    "td": {"colspan", "rowspan", "style"},
    "div": set(),
    "span": set(),
    "p": set(),
}
PASTE_STYLE_PROPERTIES = {
    "display", "text-align", "margin", "padding", "border",
    "border-width", "border-style", "border-color",
}
PASTE_NH3_STYLE_TAGS = {
    "img", "video", "audio", "source", "table", "tr", "th", "td",
}
PASTE_SANITIZER_STYLE_TAGS = {
    "img", "video", "audio", "source", "table", "th", "td",
}
PASTE_BLOCKED_TAGS = BLOCKED_HTML_TAGS | {
    "head", "title", "meta", "link", "embed", "noscript",
}
PASTE_BLOCKED_VOID_TAGS = {"meta", "link", "embed"}
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/highlights": ("highlights.html", "text/html; charset=utf-8"),
    "/highlights.js": ("highlights.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/editor.js": ("editor.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class NoteHtmlSanitizer(HTMLParser):
    def __init__(
        self,
        group: str = MYNOTE_DEFAULT_GROUP,
        link_renderer: Callable[[str], str] | None = None,
        allowed_tags: set[str] | frozenset[str] | None = None,
        blocked_tags: set[str] | frozenset[str] | None = None,
        blocked_void_tags: set[str] | frozenset[str] | None = None,
        paste_mode: str | None = None,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.open_tags: list[str] = []
        self.blocked_depth = 0
        self.group = group
        self.link_renderer = link_renderer
        self.allowed_tags = ALLOWED_HTML_TAGS if allowed_tags is None else allowed_tags
        self.blocked_tags = BLOCKED_HTML_TAGS if blocked_tags is None else blocked_tags
        self.blocked_void_tags = (
            set() if blocked_void_tags is None else blocked_void_tags
        )
        self.paste_mode = paste_mode

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if self.blocked_depth:
            if tag in self.blocked_tags and tag not in self.blocked_void_tags:
                self.blocked_depth += 1
            return
        if tag in self.blocked_tags:
            if tag not in self.blocked_void_tags:
                self.blocked_depth = 1
            return
        if tag not in self.allowed_tags:
            return
        allowed_attributes = {
            "alt", "align", "border", "colspan", "controls", "height", "href",
            "loop", "muted", "open", "poster", "preload", "rowspan", "src",
            "start", "style", "title", "type", "width", "color", "face", "size",
        }
        paste_attributes = None
        if self.paste_mode is not None:
            paste_attributes = (
                PASTE_SANITIZER_ATTRIBUTES
                if self.paste_mode == "sanitizer"
                else PASTE_NH3_ATTRIBUTES
            )
        rendered_attrs = []
        paste_style = ""
        for name, value in attrs:
            attribute = name.casefold()
            if value is None or attribute not in allowed_attributes:
                continue
            if paste_attributes is not None and attribute not in (
                paste_attributes.get("*", set()) | paste_attributes.get(tag, set())
            ):
                continue
            clean_value = value.strip()
            if attribute in {"href", "src", "poster"}:
                clean_value = sanitize_resource_url(clean_value, self.group)
                if not clean_value:
                    continue
            elif attribute == "style":
                if self.paste_mode is not None:
                    allowed_style_tags = (
                        PASTE_SANITIZER_STYLE_TAGS
                        if self.paste_mode == "sanitizer"
                        else PASTE_NH3_STYLE_TAGS
                    )
                    if tag not in allowed_style_tags:
                        continue
                clean_value = sanitize_inline_style(
                    clean_value,
                    PASTE_STYLE_PROPERTIES if self.paste_mode is not None else None,
                )
                if not clean_value:
                    continue
                if self.paste_mode is not None:
                    paste_style = clean_value
                    continue
            elif attribute in {
                "width", "height", "cellpadding", "cellspacing", "colspan",
                "rowspan", "start", "size",
            }:
                if not re.fullmatch(r"\d{1,5}", clean_value):
                    continue
            elif attribute == "type":
                if not re.fullmatch(r"[\w.+/-]{1,40}", clean_value):
                    continue
            elif attribute in {"controls", "loop", "muted", "open"}:
                if clean_value.casefold() not in {"", attribute, "true", "1"}:
                    continue
                clean_value = ""
            rendered_attrs.append(
                f' {attribute}="{html.escape(clean_value, quote=True)}"'
            )
        if self.paste_mode in {"nh3", "klembord"} and tag in {"span", "font"} and not rendered_attrs:
            return
        if self.paste_mode is not None and tag == "img":
            paste_style = ";".join(
                item for item in (paste_style, "max-width:100%", "height:auto") if item
            )
        if paste_style:
            rendered_attrs.append(
                f' style="{html.escape(paste_style, quote=True)}"'
            )
        if tag == "a" and not any(item.startswith(' href=') for item in rendered_attrs):
            self.output.append("<a>")
        else:
            self.output.append(f"<{tag}{''.join(rendered_attrs)}>")
        if tag not in VOID_HTML_TAGS:
            self.open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.blocked_depth:
            if tag in self.blocked_tags and tag not in self.blocked_void_tags:
                self.blocked_depth -= 1
            return
        if tag not in self.allowed_tags or tag in VOID_HTML_TAGS or tag not in self.open_tags:
            return
        while self.open_tags:
            opened = self.open_tags.pop()
            self.output.append(f"</{opened}>")
            if opened == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.blocked_depth:
            return
        render_note_text(data, self.output, self.link_renderer)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&#{name};"))

    def close(self) -> None:
        super().close()
        while self.open_tags:
            self.output.append(f"</{self.open_tags.pop()}>")


def sanitize_link_href(href: str | None) -> str | None:
    if not href:
        return None
    clean_href = href.strip()
    parsed = urlsplit(clean_href)
    if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc:
        return clean_href
    if not parsed.scheme and not parsed.netloc and clean_href.startswith(
        "/lookup?"
    ):
        return clean_href
    if not parsed.scheme and not parsed.netloc and clean_href.startswith("/assets/"):
        return clean_href
    if not parsed.scheme and not parsed.netloc and clean_href.startswith("#"):
        return clean_href
    return None


def sanitize_inline_style(
    style: str,
    allowed_properties: set[str] | frozenset[str] | None = None,
) -> str:
    if re.search(r"url\s*\(|expression\s*\(|@import|javascript:|behavior\s*:", style, re.I):
        return ""
    declarations = []
    for declaration in style.split(";"):
        name, separator, value = declaration.partition(":")
        property_name = name.strip().lower()
        property_value = value.strip()
        if (
            separator
            and re.fullmatch(r"[a-z-]{1,40}", property_name, re.I)
            and property_value
            and (
                allowed_properties is None
                or property_name in allowed_properties
            )
        ):
            if allowed_properties is not None:
                property_value = re.sub(
                    r"\s*!important\s*$", "", property_value, flags=re.I
                ).strip()
                if not property_value or "!" in property_value:
                    continue
            declarations.append(f"{property_name}:{property_value}")
    return ";".join(declarations)


def sanitize_resource_url(value: str, group: str) -> str:
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc:
        return value
    if value.casefold().startswith("data:image/") and not value.casefold().startswith(
        "data:image/svg+xml"
    ):
        return value
    if not parsed.scheme and not parsed.netloc and value.startswith(
        ("/lookup?", "/assets/", "#")
    ):
        return value
    if not parsed.scheme and not parsed.netloc and value and not value.startswith("//"):
        clean_parts = [part for part in value.replace("\\", "/").split("/") if part not in {"", "."}]
        if clean_parts and all(part != ".." for part in clean_parts):
            folder = "Default" if group == MYNOTE_DEFAULT_GROUP else group
            return "/assets/" + quote(folder, safe="") + "/" + "/".join(
                quote(part, safe="") for part in clean_parts
            )
    return ""


def sanitize_note_html(
    body: str,
    group: str = MYNOTE_DEFAULT_GROUP,
    link_renderer: Callable[[str], str] | None = None,
) -> str:
    sanitizer = NoteHtmlSanitizer(group, link_renderer)
    sanitizer.feed(body)
    sanitizer.close()
    return "".join(sanitizer.output)


def sanitize_paste_html(
    source: str,
    group: str,
    mode: str,
    asset_saver: Callable[[str, str, bytes], str] | None = None,
    warnings: list[str] | None = None,
    localize_images: bool = True,
) -> str:
    if mode not in {"nh3", "klembord", "sanitizer"}:
        raise StoreError("粘贴清理模式无效")
    preserved_sections: dict[str, str] = {}

    def preserve_section(match: re.Match[str]) -> str:
        placeholder = f"MYNOTEPASTE{secrets.token_hex(12)}"
        preserved_sections[placeholder] = match.group(0)
        return placeholder

    source = re.sub(
        r"(?:@@@.*?@@@|###.*?###|%%%.*?%%%)",
        preserve_section,
        source,
        flags=re.DOTALL,
    )
    allowed_tags = PASTE_SANITIZER_TAGS if mode == "sanitizer" else PASTE_NH3_TAGS
    sanitizer = NoteHtmlSanitizer(
        group=group,
        allowed_tags=allowed_tags,
        blocked_tags=PASTE_BLOCKED_TAGS,
        blocked_void_tags=PASTE_BLOCKED_VOID_TAGS,
        paste_mode=mode,
    )
    sanitizer.feed(source)
    sanitizer.close()
    result = "".join(sanitizer.output)
    if asset_saver is not None and localize_images:
        result = localize_paste_images(
            result,
            group,
            asset_saver,
            warnings if warnings is not None else [],
        )
    for placeholder, section in preserved_sections.items():
        result = result.replace(placeholder, section)
    return result


class PasteImageLocalizationError(Exception):
    pass


PASTE_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"BM", "image/bmp", ".bmp"),
)


def detect_paste_image_type(content: bytes) -> tuple[str, str]:
    for signature, mime_type, extension in PASTE_IMAGE_SIGNATURES:
        if content.startswith(signature):
            return mime_type, extension
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise PasteImageLocalizationError("不支持的图片格式")


def decode_paste_image_data_url(source: str) -> tuple[bytes, str, str]:
    match = re.fullmatch(
        r"data:(image/[a-z0-9.+-]+);base64,([\s\S]*)",
        source,
        re.IGNORECASE,
    )
    if not match:
        raise PasteImageLocalizationError("图片 data URL 格式无效")
    declared_type = match.group(1).casefold()
    if declared_type == "image/jpg":
        declared_type = "image/jpeg"
    if declared_type not in {
        "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    }:
        raise PasteImageLocalizationError("不支持的图片格式")
    encoded = re.sub(r"\s+", "", match.group(2))
    if len(encoded) > ((MAX_PASTE_IMAGE_BYTES + 2) // 3) * 4:
        raise PasteImageLocalizationError("图片超过 20 MB 限制")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise PasteImageLocalizationError("图片 data URL 编码无效") from error
    if not content or len(content) > MAX_PASTE_IMAGE_BYTES:
        raise PasteImageLocalizationError("图片超过 20 MB 限制或内容为空")
    mime_type, extension = detect_paste_image_type(content)
    if mime_type != declared_type:
        raise PasteImageLocalizationError("图片声明类型与文件内容不匹配")
    return content, mime_type, extension


def resolve_public_paste_image_url(
    source: str,
) -> tuple[
    SplitResult,
    str,
    int,
    Sequence[
        tuple[
            socket.AddressFamily,
            socket.SocketKind,
            int,
            str,
            tuple[object, ...],
        ]
    ],
]:
    if len(source) > 2048 or any(ord(character) < 32 for character in source):
        raise PasteImageLocalizationError("图片地址无效")
    try:
        parsed = urlsplit(source)
        hostname = parsed.hostname
        requested_port = parsed.port
    except ValueError as error:
        raise PasteImageLocalizationError("图片地址无效") from error
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "%" in hostname
    ):
        raise PasteImageLocalizationError("仅支持公网 HTTP(S) 图片地址")
    default_port = 80 if scheme == "http" else 443
    if requested_port not in {None, default_port}:
        raise PasteImageLocalizationError("仅允许 HTTP(S) 默认端口")
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").rstrip(".")
        addresses = socket.getaddrinfo(
            ascii_hostname,
            default_port,
            type=socket.SOCK_STREAM,
        )
    except (OSError, UnicodeError) as error:
        raise PasteImageLocalizationError("无法解析图片地址") from error
    if not addresses:
        raise PasteImageLocalizationError("无法解析图片地址")
    for _, _, _, _, sockaddr in addresses:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError as error:
            raise PasteImageLocalizationError("图片地址解析结果无效") from error
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise PasteImageLocalizationError("图片地址解析到非公网网络，已阻止下载")
    return parsed, ascii_hostname, default_port, addresses


def request_paste_image(
    source: str,
    deadline: float,
) -> tuple[int, str | None, str, bytes]:
    parsed, hostname, port, addresses = resolve_public_paste_image_url(source)
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        path += "?" + quote(parsed.query, safe="=&%/:@!$'()*+,;~-._")
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    last_error: OSError | http.client.HTTPException | ssl.SSLError | None = None

    for family, socket_type, protocol, _, sockaddr in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PasteImageLocalizationError("图片下载超时")
        connection_timeout = min(PASTE_IMAGE_FETCH_TIMEOUT, remaining)
        raw_socket: socket.socket | None = None
        response: http.client.HTTPResponse | None = None
        try:
            raw_socket = socket.socket(family, socket_type, protocol)
            raw_socket.settimeout(connection_timeout)
            raw_socket.connect(sockaddr)
            if parsed.scheme.casefold() == "https":
                tls_context = ssl.create_default_context()
                raw_socket = tls_context.wrap_socket(
                    raw_socket,
                    server_hostname=hostname,
                )
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "User-Agent: DictTango-Note-Image-Localizer/1.0\r\n"
                "Accept: image/*\r\n"
                "Connection: close\r\n"
                "\r\n"
            )
            raw_socket.sendall(request.encode("ascii"))
            response = http.client.HTTPResponse(raw_socket)
            response.begin()
            location = response.getheader("Location")
            if response.status in {301, 302, 303, 307, 308}:
                return response.status, location, "", b""
            if response.status != 200:
                raise PasteImageLocalizationError("图片服务器返回了非成功状态")
            content_length = response.getheader("Content-Length")
            if content_length:
                if not re.fullmatch(r"\d{1,12}", content_length):
                    raise PasteImageLocalizationError("图片响应长度无效")
                if int(content_length) > MAX_PASTE_IMAGE_BYTES:
                    raise PasteImageLocalizationError("图片超过 20 MB 限制")
            content = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PasteImageLocalizationError("图片下载超时")
                if raw_socket is None:
                    raise PasteImageLocalizationError("图片连接已关闭")
                raw_socket.settimeout(min(PASTE_IMAGE_FETCH_TIMEOUT, remaining))
                chunk = response.read1(min(64 * 1024, MAX_PASTE_IMAGE_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_PASTE_IMAGE_BYTES:
                    raise PasteImageLocalizationError("图片超过 20 MB 限制")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().casefold()
            return response.status, None, content_type, bytes(content)
        except PasteImageLocalizationError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            last_error = error
        finally:
            if response is not None:
                response.close()
            if raw_socket is not None:
                raw_socket.close()
    if time.monotonic() >= deadline:
        raise PasteImageLocalizationError("图片下载超时") from last_error
    raise PasteImageLocalizationError("图片下载失败或连接超时") from last_error


def download_paste_image(source: str, deadline: float) -> tuple[bytes, str, str]:
    current_url = source
    for redirect_count in range(MAX_PASTE_IMAGE_REDIRECTS + 1):
        status, location, content_type, content = request_paste_image(
            current_url,
            deadline,
        )
        if status in {301, 302, 303, 307, 308}:
            if not location or redirect_count >= MAX_PASTE_IMAGE_REDIRECTS:
                raise PasteImageLocalizationError("图片重定向次数过多或地址无效")
            current_url = urljoin(current_url, location)
            continue
        if content_type == "image/svg+xml" or content_type.startswith("text/"):
            raise PasteImageLocalizationError("服务器返回的不是受支持的位图图片")
        if content_type in {"image/jpg", "image/pjpeg"}:
            content_type = "image/jpeg"
        mime_type, extension = detect_paste_image_type(content)
        if content_type and content_type not in {
            mime_type,
            "application/octet-stream",
            "binary/octet-stream",
        }:
            raise PasteImageLocalizationError("图片响应类型与文件内容不匹配")
        return content, mime_type, extension
    raise PasteImageLocalizationError("图片重定向次数过多")


class PasteImageLocalizer(HTMLParser):
    def __init__(
        self,
        group: str,
        asset_saver: Callable[[str, str, bytes], str],
        warnings: list[str],
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.group = group
        self.asset_saver = asset_saver
        self.warnings = warnings
        self.output: list[str] = []
        self.image_count = 0
        self.localization_count = 0
        self.reported_limit = False
        self.deadline = time.monotonic() + PASTE_IMAGE_FETCH_BUDGET

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "img":
            self.image_count += 1
            src = next((value for name, value in attrs if name.casefold() == "src"), None)
            try:
                source_scheme = urlsplit(src).scheme.casefold() if src else ""
            except ValueError:
                source_scheme = ""
            if src and (src.casefold().startswith("data:image/") or source_scheme in {"http", "https"}):
                self.localization_count += 1
                if self.localization_count > MAX_PASTE_IMAGE_COUNT:
                    if not self.reported_limit:
                        self.warnings.append(
                            f"单次最多本地化 {MAX_PASTE_IMAGE_COUNT} 张图片，更多图片保留原链接。"
                        )
                        self.reported_limit = True
                else:
                    try:
                        if src.casefold().startswith("data:image/"):
                            content, _, extension = decode_paste_image_data_url(src)
                        else:
                            content, _, extension = download_paste_image(
                                src,
                                self.deadline,
                            )
                        filename = f"pasted-image-{secrets.token_hex(8)}{extension}"
                        local_name = self.asset_saver(self.group, filename, content)
                        attrs = [
                            (name, local_name if name.casefold() == "src" else value)
                            for name, value in attrs
                        ]
                    except (PasteImageLocalizationError, MyNoteStoreError) as error:
                        self.warnings.append(
                            f"第 {self.image_count} 张图片未本地化，保留原内容：{error}"
                        )
        rendered_attrs = "".join(
            f' {name.casefold()}="{html.escape(value, quote=True)}"'
            for name, value in attrs
            if value is not None
        )
        self.output.append(f"<{tag}{rendered_attrs}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag not in VOID_HTML_TAGS:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.output.append(html.escape(data))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


def localize_paste_images(
    source: str,
    group: str,
    asset_saver: Callable[[str, str, bytes], str],
    warnings: list[str],
) -> str:
    localizer = PasteImageLocalizer(group, asset_saver, warnings)
    localizer.feed(source)
    localizer.close()
    return "".join(localizer.output)


class NotePlainTextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "blockquote", "dd", "div", "dl", "dt", "figcaption", "h1", "h2",
        "h3", "h4", "h5", "h6", "li", "ol", "p", "pre", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def _append_newline(self) -> None:
        if self.parts and self.parts[-1].endswith("\n"):
            return
        self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" or tag in self.BLOCK_TAGS:
            self._append_newline()
        elif tag == "img":
            alt = next((value for name, value in attrs if name == "alt"), None)
            if alt:
                self.parts.append(alt)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK_TAGS:
            self._append_newline()

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def extract_note_plain_text(markup: str) -> str:
    extractor = NotePlainTextExtractor()
    extractor.feed(markup)
    extractor.close()
    return "".join(extractor.parts).strip("\n")


def is_html_note_body(body: str) -> bool:
    return bool(HTML_TAG_PATTERN.search(body))


NoteStore = MyNoteStore
StoreError = MyNoteStoreError

def normalize(value: str) -> str:
    return value.strip().casefold()


def render_note_text(
    text: str,
    output: list[str],
    link_renderer: Callable[[str], str] | None = None,
) -> None:
    matches = sorted(
        [*LINK_PATTERN.finditer(text), *NOTE_LOOKUP_LINK_PATTERN.finditer(text)],
        key=lambda match: match.start(),
    )
    cursor = 0
    for match in matches:
        if match.start() < cursor:
            continue
        output.append(html.escape(text[cursor : match.start()]))
        if match.re is LINK_PATTERN:
            target = match.group(1).strip()
            if target and link_renderer:
                output.append(link_renderer(target))
            elif target:
                href = "/lookup?word=" + quote(target, safe="")
                output.append(
                    f'<a class="note-link" href="{html.escape(href, quote=True)}">'
                    f'{html.escape(target)}</a>'
                )
        else:
            parameters = parse_qs(match.group(1), keep_blank_values=True)
            target = next(
                (
                    parameters[key][0]
                    for key in ("word", "q", "keyword", "search", "term")
                    if key in parameters
                ),
                "",
            ).strip()
            if target:
                group = parameters.get("group", [""])[0].strip()
                query = {"word": target}
                if group:
                    query["group"] = group
                href = "/lookup?" + urlencode(query)
                output.append(
                    f'<a class="note-link" href="{html.escape(href, quote=True)}">'
                    f'{html.escape(target)}</a>'
                )
            else:
                output.append(html.escape(match.group(0)))
        cursor = match.end()
    output.append(html.escape(text[cursor:]))


def decode_visual_text_entities(body: str) -> str:
    replacements = {"&#38;": "&", "&#60;": "<", "&#62;": ">"}
    return re.sub(
        r"&#38;|&#60;|&#62;",
        lambda match: replacements[match.group(0)],
        body,
    )


def remove_mynote_display_markers(body: str) -> str:
    def preserve_plain_lines(match: re.Match[str]) -> str:
        lines = match.group(1).split("\n")
        for index in range(len(lines) - 1):
            if not re.search(r"<br\s*/?>\s*$", lines[index], re.IGNORECASE):
                lines[index] += "<br>"
        return "\n".join(lines)

    body = re.sub(r"###(.*?)###", preserve_plain_lines, body, flags=re.DOTALL)
    return re.sub(
        r"@@@(.*?)@@@|%%%(.*?)%%%",
        lambda match: (
            match.group(1) if match.group(1) is not None else (match.group(2) or "")
        ),
        body,
        flags=re.DOTALL,
    )


def decode_escaped_note_html(body: str) -> str:
    escaped_tag_pattern = re.compile(
        r"&(?:amp;)?lt;(/?\s*(?:p|br|strong|b|em|i|u|s|del|ins|small|ul|ol|li|"
        r"dl|dt|dd|blockquote|code|pre|h[1-6]|hr|sub|sup|table|thead|tbody|"
        r"tfoot|tr|th|td|a|div|span|font|center|img|audio|video|source|"
        r"figure|figcaption|details|summary)\b[^<>]*?)&(?:amp;)?gt;",
        re.IGNORECASE,
    )
    if not escaped_tag_pattern.search(body):
        return body
    body = re.sub(r"&amp;lt;", "&lt;", body, flags=re.IGNORECASE)
    body = re.sub(r"&amp;gt;", "&gt;", body, flags=re.IGNORECASE)
    return escaped_tag_pattern.sub(r"<\1>", body)


def render_note_body(
    body: str,
    group: str = DEFAULT_GROUP,
    link_renderer: Callable[[str], str] | None = None,
) -> str:
    body = decode_escaped_note_html(body)
    body = remove_mynote_display_markers(body)
    if is_html_note_body(body):
        rendered = sanitize_note_html(body, group, link_renderer)
        return re.sub(r"(<br\s*/?>)\r?\n", r"\1", rendered, flags=re.IGNORECASE)
    body = decode_visual_text_entities(body)
    chunks: list[str] = []
    render_note_text(body, chunks, link_renderer)
    return "<br>".join("".join(chunks).split("\n"))


def render_lookup_page(
    term: str,
    notes: list[dict[str, object]],
    group_name: str = "",
    lookup: Callable[[str], list[dict[str, object]]] | None = None,
    resource_loader: Callable[[str], dict[str, list[str]]] | None = None,
    allow_note_scripts: bool = False,
) -> bytes:
    safe_term = html.escape(term)
    resource_groups: set[str] = set()
    if not term:
        body = '<p class="empty">请通过在线词典传入查询词条。</p>'
    elif not notes:
        body = f'<p class="empty">没有找到“{safe_term}”的笔记。</p>'
    else:
        entries = []
        for note in notes:
            group = html.escape(str(note["group"]))
            reference_count = [0]

            def resolve_reference(target: str, visited: frozenset[str] = frozenset()) -> str:
                target_key = normalize(target)
                if target_key in visited or len(visited) >= 8 or reference_count[0] >= 64:
                    return f'<span class="reference-warning">[循环或过深引用：{html.escape(target)}]</span>'
                reference_count[0] += 1
                if lookup is None:
                    href = "/lookup?word=" + quote(target, safe="")
                    return f'<a class="note-link" href="{html.escape(href, quote=True)}">{html.escape(target)}</a>'
                references = lookup(target)
                if not references:
                    return f'<span class="reference-warning">[未找到引用：{html.escape(target)}]</span>'
                next_visited = visited | {target_key}
                fragments = []
                for reference in references:
                    reference_group = str(reference["group"])
                    resource_groups.add(reference_group)
                    reference_html = render_note_body(
                        str(reference["body"]),
                        reference_group,
                        lambda nested: resolve_reference(nested, next_visited),
                    )
                    fragments.append(
                        '<section class="reference"><strong>引用：'
                        f'{html.escape(reference_group)}</strong>{reference_html}</section>'
                    )
                return "".join(fragments)

            rendered = render_note_body(
                str(note["body"]),
                str(note["group"]),
                lambda target: resolve_reference(target, frozenset({normalize(term)})),
            )
            resource_groups.add(str(note["group"]))
            content_html = rendered or '<span class="empty">（空笔记）</span>'
            edit_href = "/editor?" + urlencode({"note_id": str(note["id"])})
            note_id = str(note["id"])
            entries.append(
                '<article class="note">'
                '<div class="note-header">'
                f'<div class="group">{group}</div>'
                '<div class="note-actions">'
                f'<button class="delete-link" type="button" data-delete-note '
                f'data-note-id="{html.escape(note_id, quote=True)}" '
                f'data-note-term="{html.escape(str(note["term"]), quote=True)}">删除</button>'
                f'<a class="edit-link" href="{html.escape(edit_href, quote=True)}">编辑</a>'
                "</div>"
                "</div>"
                f'<div class="content">{content_html}</div>'
                "</article>"
            )
        body = "".join(entries)
    delete_dialog = ""
    if notes:
        delete_dialog = f"""<dialog class="lookup-delete-dialog" id="lookup-delete-dialog"
aria-labelledby="lookup-delete-title" aria-describedby="lookup-delete-message">
<form method="post" action="/delete-note">
<h2 id="lookup-delete-title">删除笔记</h2>
<p id="lookup-delete-message">确定删除“<span id="lookup-delete-term"></span>”这条笔记吗？此操作无法撤销。</p>
<input type="hidden" name="note_id" id="lookup-delete-note-id">
<input type="hidden" name="return_term" value="{html.escape(term, quote=True)}">
<input type="hidden" name="return_group" value="{html.escape(group_name, quote=True)}">
<div class="lookup-delete-actions">
<button class="lookup-delete-cancel" type="button" data-close-delete>取消</button>
<button class="lookup-delete-confirm" type="submit">确认删除</button>
</div>
</form></dialog>"""
    stylesheets: list[str] = []
    font_faces: list[str] = []
    scripts: list[str] = []
    if resource_loader:
        for resource_group in sorted(resource_groups, key=str.casefold):
            folder = "Default" if resource_group == DEFAULT_GROUP else resource_group
            resource_base = "/assets/" + quote(folder, safe="") + "/"
            resources = resource_loader(resource_group)
            for filename in resources["css"]:
                resource_url = resource_base + quote(filename, safe="")
                stylesheets.append(
                    f'<link rel="stylesheet" href="{html.escape(resource_url, quote=True)}">'
                )
            for filename in resources["fonts"]:
                extension = Path(filename).suffix.casefold()
                font_format = {
                    ".ttf": "truetype",
                    ".otf": "opentype",
                    ".woff": "woff",
                    ".woff2": "woff2",
                }[extension]
                family = re.sub(r"[^\w -]", "_", Path(filename).stem).strip() or "NoteFont"
                resource_url = resource_base + quote(filename, safe="")
                font_faces.append(
                    f'@font-face{{font-family:"{family}";'
                    f'src:url("{resource_url}") format("{font_format}");}}'
                )
            if allow_note_scripts:
                for filename in resources["js"]:
                    resource_url = resource_base + quote(filename, safe="")
                    scripts.append(
                        f'<script defer src="{html.escape(resource_url, quote=True)}"></script>'
                    )

    nonce = base64.b64encode(secrets.token_bytes(18)).decode("ascii") if notes else ""
    mathjax_config = ""
    if scripts:
        mathjax_config = (
            f'<script nonce="{nonce}">window.MathJax={{tex:{{'
            'inlineMath:[["$","$"],["\\\\(","\\\\)"]],'
            'displayMath:[["$$","$$"],["\\\\[","\\\\]"]]},'
            'options:{skipHtmlTags:["script","noscript","style","textarea","pre","code"]}}};'
            "</script>"
        )
    delete_script = ""
    if notes:
        delete_script = f"""<script nonce="{nonce}">
document.addEventListener("DOMContentLoaded", () => {{
  document.querySelectorAll("[data-delete-note]").forEach((button) => {{
    button.addEventListener("click", () => {{
      document.getElementById("lookup-delete-note-id").value = button.dataset.noteId;
      document.getElementById("lookup-delete-term").textContent = button.dataset.noteTerm;
      document.getElementById("lookup-delete-dialog").showModal();
    }});
  }});
  document.querySelector("[data-close-delete]").addEventListener("click", () => {{
    document.getElementById("lookup-delete-dialog").close();
  }});
}});
</script>"""
    script_markup = mathjax_config + "".join(scripts) + delete_script
    script_source = "'self'"
    if nonce:
        script_source += f" 'nonce-{nonce}'"
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
<meta name="color-scheme" content="light dark">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' data: https:; media-src 'self' data: https:; font-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src {script_source}; base-uri 'none'; form-action 'self'">
<title>{safe_term or "本地笔记"}</title>
{"".join(stylesheets)}
<style>{"".join(font_faces)}</style>
<style>
body{{font:16px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:14px;color:#243148;background:#f7f8fb}}
.term{{font-size:1.35rem;font-weight:700;margin:0 0 12px;overflow-wrap:anywhere}}
.note{{background:#fff;border:1px solid #e3e7ef;border-radius:12px;padding:12px;margin:10px 0;overflow-wrap:anywhere}}
.note-header{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:6px}}
.note-actions{{display:flex;align-items:center;gap:10px}}
.group{{font-size:.78rem;color:#5b6578}}
.edit-link{{flex:0 0 auto;color:#3559b7;font-size:.8rem;text-decoration:none}}
.edit-link:hover{{text-decoration:underline}}
.delete-link{{padding:0;border:0;color:#b94b4b;background:transparent;font:inherit;font-size:.8rem;cursor:pointer}}
.delete-link:hover{{text-decoration:underline}}
.lookup-delete-dialog{{width:min(420px,calc(100vw - 32px));padding:20px;border:1px solid #e3e7ef;border-radius:12px;color:#243148;background:#fff;box-shadow:0 12px 40px rgb(34 49 76 / 18%)}}
.lookup-delete-dialog::backdrop{{background:rgb(16 24 40 / 58%)}}
.lookup-delete-dialog h2{{margin:0;font-size:1.1rem}}
.lookup-delete-dialog p{{margin:12px 0 20px;overflow-wrap:anywhere}}
.lookup-delete-actions{{display:flex;justify-content:flex-end;gap:8px}}
.lookup-delete-actions button{{min-height:36px;padding:0 12px;border:1px solid #e0e5ed;border-radius:8px;background:#fff;color:#59667b;font:inherit;font-size:.85rem;cursor:pointer}}
.lookup-delete-actions .lookup-delete-confirm{{border-color:#b94b4b;background:#b94b4b;color:#fff}}
.content{{white-space:normal}}
.content img,.content video{{max-width:100%;height:auto}}.content audio{{max-width:100%}}
.reference{{margin:8px 0;padding:8px;border-left:3px solid #d5dcf4}}.reference-warning{{color:#8a5360}}
.note-link{{color:#3559b7;text-decoration:underline}}
.empty{{color:#687386}}
@media(prefers-color-scheme:dark){{body{{background:#171a21;color:#e4e9f2}}.note{{background:#222733;border-color:#343b49}}.group,.empty{{color:#abb4c4}}.note-link,.edit-link{{color:#9db7ff}}.delete-link{{color:#ff9b9b}}.lookup-delete-dialog{{background:#222733;border-color:#343b49;color:#e4e9f2}}.lookup-delete-actions button{{background:#2b3240;border-color:#414958;color:#e4e9f2}}.lookup-delete-actions .lookup-delete-confirm{{background:#b94b4b;border-color:#b94b4b;color:#fff}}}}
</style>{script_markup}</head><body><h1 class="term">{safe_term or "本地笔记"}</h1>{body}{delete_dialog}</body></html>"""
    return document.encode("utf-8")


def render_error_page(message: str) -> bytes:
    safe_message = html.escape(message)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>无法保存笔记</title>
<style>body{{margin:0;padding:24px;background:#f4f6fa;color:#283247;font:16px/1.6 system-ui,sans-serif}}main{{max-width:640px;margin:auto;padding:20px;border-radius:12px;background:#fff}}a{{color:#3559b7}}</style>
</head><body><main><h1>无法保存笔记</h1><p>{safe_message}</p><p><a href="/">返回笔记管理页</a></p></main></body></html>""".encode("utf-8")


def render_confirmation_page(
    title: str,
    message: str,
    action: str,
    field_name: str,
    field_value: str,
    cancel_href: str,
    submit_label: str,
) -> bytes:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{html.escape(title)}</title><link rel="stylesheet" href="/styles.css">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; base-uri 'self'; object-src 'none">
</head><body><main class="standalone-page card"><h1>{html.escape(title)}</h1>
<p>{html.escape(message)}</p><form method="post" action="{html.escape(action, quote=True)}">
<input type="hidden" name="{html.escape(field_name, quote=True)}" value="{html.escape(field_value, quote=True)}">
<div class="page-actions"><a class="button button-quiet cancel" href="{html.escape(cancel_href, quote=True)}">取消</a>
<button class="button button-danger" type="submit">{html.escape(submit_label)}</button></div>
</form></main></body></html>""".encode("utf-8")

def load_notes_directory_setting(settings_path: Path) -> Path | None:
    settings_path = Path(settings_path)
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取笔记根文件夹设置：{error}") from error
    if not isinstance(settings, dict) or not isinstance(settings.get("notes_directory"), str):
        raise ValueError("笔记根文件夹设置格式无效")
    directory = settings["notes_directory"].strip()
    if not directory:
        raise ValueError("笔记根文件夹设置格式无效")
    try:
        configured_path = Path(directory).expanduser()
    except (RuntimeError, ValueError) as error:
        raise ValueError("笔记根文件夹设置格式无效") from error
    if not configured_path.is_absolute():
        configured_path = settings_path.parent / configured_path
    try:
        notes_directory = configured_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("设置的笔记根文件夹不存在或无法访问") from error
    if not notes_directory.is_dir():
        raise ValueError("设置的笔记根文件夹不存在或不是文件夹")
    return notes_directory


def load_highlight_rules_path(settings_path: Path) -> Path:
    settings_path = Path(settings_path)
    default_path = settings_path.with_name("highlight-rules.json").resolve()
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_path
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取高光数据路径设置：{error}") from error
    if not isinstance(settings, dict):
        raise ValueError("高光数据路径设置格式无效")
    if "highlight_rules_path" not in settings:
        return default_path
    configured_path = settings["highlight_rules_path"]
    if not isinstance(configured_path, str) or not configured_path.strip():
        raise ValueError("高光数据路径设置格式无效")
    try:
        path = Path(configured_path.strip()).expanduser()
        if not path.is_absolute():
            path = settings_path.parent / path
        return path.resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("高光数据路径设置格式无效") from error


def load_sync_directory_setting(settings_path: Path) -> Path | None:
    settings_path = Path(settings_path)
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取同步文件夹设置：{error}") from error
    if not isinstance(settings, dict):
        raise ValueError("同步文件夹设置格式无效")
    if "sync_directory" not in settings:
        return None
    directory = settings["sync_directory"]
    if not isinstance(directory, str):
        raise ValueError("同步文件夹设置格式无效")
    if not directory.strip():
        return None
    try:
        path = Path(directory.strip()).expanduser()
        if not path.is_absolute():
            path = settings_path.parent / path
        return path.resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("同步文件夹设置格式无效") from error


def load_group_resource_directories(settings_path: Path) -> dict[str, str]:
    settings_path = Path(settings_path)
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取分组资源文件夹设置：{error}") from error
    if not isinstance(settings, dict):
        raise ValueError("分组资源文件夹设置格式无效")
    configured = settings.get("group_resource_directories", {})
    if not isinstance(configured, dict):
        raise ValueError("分组资源文件夹设置格式无效")
    directories: dict[str, str] = {}
    for group, directory in configured.items():
        if not isinstance(group, str) or not isinstance(directory, str) or not directory.strip():
            raise ValueError("分组资源文件夹设置格式无效")
        try:
            if group != MYNOTE_DEFAULT_GROUP:
                MyNoteStore._validate_group_name(group)
            path = Path(directory.strip()).expanduser()
            if not path.is_absolute():
                path = settings_path.parent / path
            directories[group] = str(path.resolve())
        except (OSError, RuntimeError, ValueError, MyNoteStoreError) as error:
            raise ValueError("分组资源文件夹设置格式无效") from error
    return directories


def load_group_note_paths(settings_path: Path) -> dict[str, str]:
    settings_path = Path(settings_path)
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取分组笔记文件路径设置：{error}") from error
    if not isinstance(settings, dict):
        raise ValueError("分组笔记文件路径设置格式无效")
    configured = settings.get("group_note_paths", {})
    if not isinstance(configured, dict):
        raise ValueError("分组笔记文件路径设置格式无效")
    paths: dict[str, str] = {}
    for filename, path_value in configured.items():
        if (
            not isinstance(filename, str)
            or not MyNoteStore._is_group_filename(filename)
            or not isinstance(path_value, str)
            or not path_value.strip()
        ):
            raise ValueError("分组笔记文件路径设置格式无效")
        try:
            path = Path(path_value.strip()).expanduser()
            if not path.is_absolute():
                path = settings_path.parent / path
            resolved_path = path.resolve()
            if path.name != filename or resolved_path.name != filename:
                raise ValueError("分组笔记文件名必须符合 MyNote 命名格式")
            paths[filename] = str(resolved_path)
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("分组笔记文件路径设置格式无效") from error
    return paths


def render_editor_page(
    term: str,
    group: str,
    note: dict[str, object] | None,
    modal: bool = False,
    groups: list[dict[str, object]] | None = None,
) -> bytes:
    safe_term = html.escape(term, quote=True)
    safe_group = html.escape(group, quote=True)
    asset_folder = "Default" if group == DEFAULT_GROUP else group
    asset_base = "/assets/" + quote(asset_folder, safe="")
    note_id = str(note["id"]) if note else ""
    body = render_note_body(str(note["body"]), group) if note else ""
    visual_body = html.escape(extract_note_plain_text(body))
    original_body_value = str(note["body"]) if note else ""
    original_body = html.escape(original_body_value)
    editable_body = html.escape(decode_escaped_note_html(original_body_value))
    group_options = "".join(
        f'<option value="{html.escape(str(item["name"]), quote=True)}"></option>'
        for item in groups or []
    )
    action = "/editor"
    cancel_href = f"/?note={note_id}" if note else f"/?word={quote(term, safe='')}" if term else "/"
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
<meta name="color-scheme" content="light dark">
<base href="{asset_base}/">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; connect-src 'self'; img-src 'self' data: https:; media-src 'self' data: https:; style-src 'unsafe-inline'; script-src 'self'; form-action 'self'; base-uri 'self'; object-src 'none'">
<title>编辑笔记 · {safe_term}</title>
<style>
*{{box-sizing:border-box}}html,body{{width:100%;height:100%;min-width:0;overflow-x:hidden}}body{{margin:0;padding:0;background:#fff;color:#202b3c;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{width:100%;max-width:none;min-height:100%;margin:0}}.editor{{min-width:0;min-height:100%;max-width:100%;padding:16px;border:0;border-radius:0;background:#fff;box-shadow:none}}
h1{{margin:0 0 20px;font-size:1.35rem}}label{{display:block;margin:15px 0 6px;font-size:.88rem;font-weight:700}}
input,textarea{{display:block;width:100%;min-width:0;max-width:100%;padding:11px 12px;border:1px solid #dfe4ec;border-radius:9px;background:#fff;color:#202b3c;font:inherit}}
textarea[hidden]{{display:none}}
.editor-mode-row{{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:8px 0}}
.source-tools{{display:flex;width:100%;min-width:0;flex-wrap:wrap;gap:7px;margin:6px 0}}
.source-tools[hidden]{{display:none}}
.paste-mode-control{{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:0;font-size:.84rem}}
.paste-mode-control select{{min-height:36px;max-width:100%;padding:0 10px;border:1px solid #dfe4ec;border-radius:8px;background:#fff;color:#283247;font:inherit}}
.paste-mode-control button{{min-height:36px;padding:0 12px;font-size:.82rem}}
.paste-localization-control{{display:inline-flex;align-items:center;gap:5px;white-space:nowrap;cursor:pointer}}
.paste-localization-control input{{display:inline-block;width:16px;height:16px;margin:0;padding:0;border:0;background:transparent;accent-color:#4968c8}}
.source-action{{min-height:36px;padding:0 12px;font-size:.82rem}}
.source-action[hidden]{{display:none}}
.toolbar{{position:relative;display:flex;width:100%;min-width:0;flex-wrap:wrap;align-items:flex-start;gap:6px;margin:0;padding:0;border:0;background:transparent}}
.tool-group{{position:static}}
.tool-group>summary{{display:flex;align-items:center;gap:7px;min-height:36px;padding:0 11px;border:1px solid #dfe4ec;border-radius:8px;background:#fff;color:#283247;font-size:.84rem;font-weight:650;cursor:pointer;list-style:none}}
.tool-group>summary::-webkit-details-marker{{display:none}}
.tool-group>summary::after{{content:"▾";color:#778399;font-size:.7rem}}
.tool-group[open]>summary{{border-color:#8194dc;background:#f3f5ff}}
.tool-panel{{position:absolute;z-index:10;top:0;left:0;display:flex;flex-wrap:wrap;align-items:center;gap:6px;width:max-content;max-width:calc(100% - 16px);max-height:calc(100vh - 16px);overflow:auto;padding:9px;border:1px solid #dfe4ec;border-radius:10px;background:#fff;box-shadow:0 8px 24px #17203324}}
.tool-group[data-panel-placement="above"]>.tool-panel{{top:auto}}
.tool-panel button,.tool-panel label{{display:inline-flex;align-items:center;justify-content:center;min-width:34px;min-height:34px;margin:0;padding:0 9px;border:1px solid #dfe4ec;border-radius:7px;background:#fff;color:#283247;font:600 .82rem system-ui,sans-serif;white-space:normal;overflow-wrap:anywhere}}
.tool-panel button:active,.tool-panel label:active{{background:#e8edff}}
.tool-panel label{{cursor:pointer}}
.tool-panel input[type=file]{{display:none}}
.tool-panel .link-row{{display:flex;flex-wrap:wrap;width:min(360px,100%);min-width:0;gap:7px;margin:0}}
.tool-panel .link-row input{{width:auto;min-width:0;flex:1 1 170px;padding:8px 10px}}
.tool-panel .link-row button{{min-height:36px;padding:0 10px;background:#4968c8;color:#fff}}
.tool-panel .replace-row{{display:grid;width:min(360px,100%);min-width:0;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin:0}}
.tool-panel .replace-row input{{min-width:0;padding:8px 10px}}
.tool-panel .replace-row button{{min-height:34px}}
.format-panel{{max-width:min(390px,calc(100% - 16px))}}
.rich-editor,.html-source{{width:100%;min-width:0;max-width:100%;height:min(45vh,420px);min-height:200px;max-height:45vh;overflow:auto;resize:vertical}}
.rich-editor{{padding:12px;border:1px solid #dfe4ec;border-radius:9px;background:#fff;color:#202b3c;line-height:1.7;overflow-wrap:anywhere;white-space:pre-wrap}}
.rich-editor:focus{{outline:2px solid #c5d0ff;border-color:#738bdc}}
.html-source{{font-family:Consolas,"Cascadia Code",monospace;font-size:.88rem;line-height:1.55;resize:vertical}}
.hint{{color:#788397;font-size:.8rem}}.actions{{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:10px;margin-top:18px}}
.actions.source-mode{{gap:8px}}.actions.source-mode>button,.actions.source-mode>.cancel{{min-height:36px;padding:0 12px;font-size:.82rem}}
button,.cancel{{display:inline-flex;align-items:center;justify-content:center;min-height:42px;padding:0 16px;border:0;border-radius:9px;background:#4968c8;color:#fff;font:700 .9rem system-ui,sans-serif;text-decoration:none}}
.cancel{{border:1px solid #e0e5ed;background:transparent;color:#59667b}}
@media(prefers-color-scheme:dark){{body{{background:#171a21;color:#e4e9f2}}.editor{{background:#171a21}}input,textarea,.rich-editor{{background:#171b23;color:#e4e9f2;border-color:#414958}}.tool-group>summary,.tool-panel,.tool-panel button,.tool-panel label{{background:#202631;color:#e4e9f2;border-color:#414958}}.paste-mode-control select{{background:#202631;color:#e4e9f2;border-color:#414958}}.tool-group[open]>summary,.tool-panel button:active,.tool-panel label:active{{background:#303744}}.tool-panel .link-row button{{background:#4968c8}}.hint{{color:#abb4c4}}.cancel{{color:#d3dae5;border-color:#414958}}}}
</style></head><body><main><form class="editor" method="post" action="{action}">
<h1>{'编辑笔记' if note else '新建笔记'}</h1>
<input type="hidden" name="note_id" value="{note_id}">
<input type="hidden" name="modal" value="{'1' if modal else '0'}">
<input type="hidden" name="original_term" value="{safe_term}">
<input type="hidden" name="original_group" value="{safe_group}">
<input type="hidden" id="deleted-image-assets" name="deleted_image_assets" value="[]">
<label for="term">词头</label><input id="term" name="term" inputmode="text" autocomplete="off" maxlength="{MAX_TERM_LENGTH}" required value="{safe_term}">
<label for="group">分组</label><input id="group" name="group" inputmode="text" list="group-options" autocomplete="off" maxlength="{MAX_GROUP_LENGTH}" required value="{safe_group}">
<datalist id="group-options">{group_options}</datalist>
<p class="hint group-hint">可从列表选择，也可输入新分组名称；保存时会自动创建。</p>
<label for="body-editor">笔记内容</label>
<div class="editor-mode-row"><button id="toggle-html-source" type="button" aria-pressed="false">编辑 HTML 源码</button><span class="hint">纯文本模式；源码工具按类别折叠。</span></div>
<div class="source-tools" hidden>
<div class="paste-mode-control"><label for="paste-mode">📋 粘贴模式</label>
<select id="paste-mode" aria-label="粘贴清理模式" title="仅在 HTML 源码模式粘贴时清理剪贴板 HTML 或纯文本">
<option value="nh3">NH3 清理</option><option value="klembord">Klembord 兼容</option><option value="sanitizer">HTML Sanitizer</option>
</select><label class="paste-localization-control" for="localize-pasted-images" title="关闭后仍清理 HTML，但不下载粘贴内容中的图片"><input id="localize-pasted-images" type="checkbox" checked>本地化图片</label></div>
<div class="toolbar" role="toolbar" aria-label="笔记工具">
<details class="tool-group" name="note-source-tool-groups"><summary>格式</summary><div class="tool-panel format-panel">
<button type="button" data-command="bold" aria-label="粗体"><strong>B</strong></button>
<button type="button" data-command="italic" aria-label="斜体"><em>I</em></button>
<button type="button" data-command="underline" aria-label="下划线"><u>U</u></button>
<button type="button" data-command="insertUnorderedList" aria-label="项目列表">• 列表</button>
<button type="button" data-command="insertOrderedList" aria-label="编号列表">1. 列表</button>
<button type="button" data-command="formatBlock" data-value="blockquote" aria-label="引用">引用</button>
<button type="button" data-command="formatBlock" data-value="h2" aria-label="标题">标题</button>
<button type="button" data-command="formatBlock" data-value="p" aria-label="正文">正文</button>
</div></details>
<details class="tool-group" name="note-source-tool-groups"><summary>插入</summary><div class="tool-panel">
<label>插入图片<input type="file" accept="image/*" data-upload-kind="image"></label>
<label>插入音频<input type="file" accept="audio/*" data-upload-kind="audio"></label>
<label>插入视频<input type="file" accept="video/*" data-upload-kind="video"></label>
</div></details>
<details class="tool-group" name="note-source-tool-groups"><summary>链接</summary><div class="tool-panel"><div class="link-row"><input id="link-url" type="url" inputmode="url" placeholder="https://... 或本地查询链接" aria-label="链接地址"><button id="insert-link" type="button">为选中文字添加链接</button></div></div></details>
<details class="tool-group" name="note-source-tool-groups"><summary>查找/替换</summary><div class="tool-panel"><div class="replace-row"><input id="find-text" type="search" placeholder="查找正文" aria-label="查找正文"><input id="replace-text" type="text" placeholder="替换为" aria-label="替换为"><button id="replace-next" type="button">替换当前</button><button id="replace-all" type="button">全部替换</button></div></div></details>
</div></div>
<textarea id="body-editor" class="rich-editor" aria-label="笔记纯文本内容">{visual_body}</textarea>
<textarea id="body-source" class="html-source" aria-label="笔记 HTML 源码" hidden>{editable_body}</textarea>
<textarea id="body" name="body" data-max-length="{MAX_BODY_LENGTH}" hidden>{original_body}</textarea>
<p id="editor-status" class="hint">每个内容换行只保存为一个 &lt;br&gt;；源码中的换行仅供阅读，不会额外显示。格式工具在源码模式使用。</p>
<div class="actions"><a class="cancel" href="{html.escape(cancel_href, quote=True)}">取消</a><button id="delete-current-source" class="source-action" type="button" hidden>删除代码</button><button id="clean-current-source" class="source-action" type="button" hidden title="整理源码；图片是否下载由“本地化图片”选项控制">整理代码</button><button type="submit">保存笔记</button></div>
</form></main><script src="/editor.js" defer></script></body></html>"""
    return document.encode("utf-8")


class LocalNoteServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        notes_directory: Path,
        web_dir: Path,
        legacy_database_path: Path | None = None,
        allow_note_scripts: bool = False,
        settings_path: Path = DEFAULT_SETTINGS_PATH,
        test_mode: bool = False,
        sync_state_path: Path | None = None,
    ) -> None:
        self.store = NoteStore(notes_directory, legacy_database_path)
        self.web_dir = web_dir.resolve()
        self.allow_note_scripts = allow_note_scripts
        self.settings_path = Path(settings_path).resolve()
        self.store.set_group_note_paths(load_group_note_paths(self.settings_path))
        self.store.set_group_resource_directories(
            load_group_resource_directories(self.settings_path)
        )
        self.highlight_manager = HighlightManager(
            self.store,
            load_highlight_rules_path(self.settings_path),
        )
        self.sync_manager = SyncManager(
            self.store,
            self.highlight_manager,
            load_sync_directory_setting(self.settings_path),
            sync_state_path or default_sync_state_path(),
        )
        self.test_mode = test_mode
        self._settings_lock = threading.Lock()
        super().__init__(address, NoteHandler)

    def _write_settings(
        self,
        notes_directory: Path,
        group_note_paths: dict[str, str],
        group_resource_directories: dict[str, str],
        highlight_rules_path: Path | None = None,
        sync_directory: Path | None | object = _UNSET_SETTING,
    ) -> None:
        temporary_path: Path | None = None
        configured_sync_directory = (
            self.sync_manager.sync_directory
            if sync_directory is _UNSET_SETTING
            else sync_directory
        )
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.settings_path.parent,
                prefix=f".{self.settings_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(
                    json.dumps(
                        {
                            "notes_directory": str(notes_directory),
                            "group_note_paths": group_note_paths,
                            "group_resource_directories": group_resource_directories,
                            "highlight_rules_path": str(
                                highlight_rules_path or self.highlight_manager.settings_path
                            ),
                            "sync_directory": str(
                                configured_sync_directory or ""
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                )
                stream.flush()
            temporary_path.replace(self.settings_path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise StoreError(f"无法保存笔记设置：{error}") from error

    def set_sync_directory(self, directory: str) -> Path | None:
        with self._settings_lock, self.sync_manager._lock:
            configured_path = self.sync_manager.set_sync_directory(directory)
            self._write_settings(
                self.store.notes_directory,
                self.store.group_note_paths(),
                self.store.group_resource_directories(),
                sync_directory=configured_path,
            )
            self.sync_manager.sync_directory = configured_path
        return configured_path

    def set_highlight_rules_path(self, path_value: str) -> Path:
        path_value = path_value.strip()
        if not path_value:
            raise StoreError("高光数据文件路径不能为空")
        try:
            path = Path(path_value).expanduser()
            if not path.is_absolute():
                path = self.settings_path.parent / path
            path = path.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise StoreError("高光数据文件路径无效") from error
        if path.suffix.lower() != ".json":
            raise StoreError("高光数据文件必须使用 .json 扩展名")

        with self._settings_lock:
            try:
                manager = self.highlight_manager.copy_to(path)
            except ValueError as error:
                raise StoreError(str(error)) from error
            self._write_settings(
                self.store.notes_directory,
                self.store.group_note_paths(),
                self.store.group_resource_directories(),
                path,
            )
            self.highlight_manager = manager
        return path

    def set_notes_directory(self, directory: str) -> None:
        directory = directory.strip()
        if not directory:
            raise StoreError("笔记根文件夹路径不能为空")
        try:
            requested_path = Path(directory).expanduser()
        except (RuntimeError, ValueError) as error:
            raise StoreError("笔记根文件夹路径无效") from error
        try:
            notes_directory = requested_path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise StoreError("笔记根文件夹不存在或无法访问") from error
        if not notes_directory.is_dir():
            raise StoreError("笔记根文件夹不存在或不是文件夹")
        with self._settings_lock:
            self._write_settings(
                notes_directory,
                self.store.group_note_paths(),
                self.store.group_resource_directories(),
            )
            self.store.set_notes_directory(notes_directory)

    def create_group(
        self,
        name: object,
        note_path: str = "",
        resource_directory: str = "",
    ) -> dict[str, object]:
        clean_name = MyNoteStore._validate_group_name(name)
        if not isinstance(note_path, str) or not isinstance(resource_directory, str):
            raise StoreError("分组路径设置格式无效")
        filename = MyNoteStore._group_file(clean_name)
        default_note_path = (self.store.notes_directory / filename).resolve()
        note_path = note_path.strip()
        custom_note_path: Path | None = None
        if note_path:
            try:
                requested_note_path = Path(note_path).expanduser().resolve()
            except (OSError, RuntimeError, ValueError) as error:
                raise StoreError("分组 TXT 文件路径无效") from error
            if requested_note_path.name != filename:
                raise StoreError(
                    f"TXT 文件名必须为 {filename}，以保持 MyNote 分组对应关系"
                )
            if requested_note_path != default_note_path:
                custom_note_path = requested_note_path

        resource_directory = resource_directory.strip()
        custom_resource_directory: Path | None = None
        if resource_directory:
            try:
                custom_resource_directory = Path(resource_directory).expanduser().resolve(
                    strict=True
                )
            except (OSError, RuntimeError, ValueError) as error:
                raise StoreError("分组资源文件夹不存在或无法访问") from error
            if not custom_resource_directory.is_dir():
                raise StoreError("分组资源路径不是文件夹")

        with self._settings_lock:
            if any(
                normalize(str(group["name"])) == normalize(clean_name)
                for group in self.store.list_groups()
            ):
                raise StoreError("该分组已存在", 409)

            current_note_paths = self.store.group_note_paths()
            current_resources = self.store.group_resource_directories()
            updated_note_paths = dict(current_note_paths)
            if custom_note_path is None:
                updated_note_paths.pop(filename, None)
            else:
                updated_note_paths[filename] = str(custom_note_path)
            updated_resources = dict(current_resources)
            if custom_resource_directory is None:
                updated_resources.pop(clean_name, None)
            else:
                updated_resources[clean_name] = str(custom_resource_directory)

            target_note_path = custom_note_path or default_note_path
            note_file_preexisted = target_note_path.is_file()
            group = self.store.create_group(clean_name, custom_note_path)
            try:
                if (
                    updated_note_paths != current_note_paths
                    or updated_resources != current_resources
                ):
                    self._write_settings(
                        self.store.notes_directory,
                        updated_note_paths,
                        updated_resources,
                    )
            except StoreError as settings_error:
                self.store.set_group_note_paths(current_note_paths)
                self.store.set_group_resource_directories(current_resources)
                if not note_file_preexisted:
                    try:
                        target_note_path.unlink(missing_ok=True)
                    except OSError as rollback_error:
                        raise StoreError(
                            f"分组创建失败，且无法清理新建的 TXT 文件：{rollback_error}",
                            500,
                        ) from settings_error
                raise
            self.store.set_group_note_paths(updated_note_paths)
            self.store.set_group_resource_directories(updated_resources)
            return group

    def set_group_paths(self, group: str, note_path: str, resource_directory: str) -> None:
        if not isinstance(group, str):
            raise StoreError("分组名称无效")
        with self._settings_lock:
            if group not in {str(item["name"]) for item in self.store.list_groups()}:
                raise StoreError("没有找到这个分组", 404)
            filename = MyNoteStore._group_file(group)
            default_note_path = (self.store.notes_directory / filename).resolve()
            note_path = note_path.strip()
            updated_note_paths = self.store.group_note_paths()
            if note_path:
                try:
                    requested_note_path = Path(note_path).expanduser().resolve()
                except (OSError, RuntimeError, ValueError) as error:
                    raise StoreError("分组 TXT 文件路径无效") from error
                if requested_note_path.name != filename:
                    raise StoreError(
                        f"TXT 文件名必须为 {filename}，以保持 MyNote 分组对应关系"
                    )
                if requested_note_path == default_note_path:
                    if (
                        filename in updated_note_paths
                        and not default_note_path.is_file()
                    ):
                        raise StoreError(
                            "根文件夹中没有该分组的 TXT 文件；请先将文件放回默认位置再恢复"
                        )
                    updated_note_paths.pop(filename, None)
                else:
                    try:
                        requested_note_path = requested_note_path.resolve(strict=True)
                    except (OSError, RuntimeError, ValueError) as error:
                        raise StoreError("分组 TXT 文件不存在或无法访问") from error
                    if not requested_note_path.is_file():
                        raise StoreError("分组 TXT 路径不是文件")
                    for other_filename in self.store._group_files():
                        if other_filename == filename:
                            continue
                        if self.store._note_path(other_filename) == requested_note_path:
                            raise StoreError("此 TXT 文件已指定给另一个分组", 409)
                    updated_note_paths[filename] = str(requested_note_path)
            else:
                if not default_note_path.is_file():
                    raise StoreError(
                        "根文件夹中没有该分组的 TXT 文件；请先将文件放回默认位置再恢复"
                    )
                updated_note_paths.pop(filename, None)

            updated_resources = self.store.group_resource_directories()
            resource_directory = resource_directory.strip()
            if resource_directory:
                try:
                    requested_resource_directory = (
                        Path(resource_directory).expanduser().resolve(strict=True)
                    )
                except (OSError, RuntimeError, ValueError) as error:
                    raise StoreError("分组资源文件夹不存在或无法访问") from error
                if not requested_resource_directory.is_dir():
                    raise StoreError("分组资源路径不是文件夹")
                updated_resources[group] = str(requested_resource_directory)
            else:
                updated_resources.pop(group, None)
            self._write_settings(
                self.store.notes_directory,
                updated_note_paths,
                updated_resources,
            )
            self.store.set_group_note_paths(updated_note_paths)
            self.store.set_group_resource_directories(updated_resources)

    def set_group_resource_directory(self, group: str, directory: str) -> None:
        group_paths = self.store.group_paths().get(group)
        if group_paths is None:
            raise StoreError("没有找到这个分组", 404)
        self.set_group_paths(group, group_paths["note_path"], directory)

    def rename_group(self, group_id: str, name: object) -> dict[str, object]:
        with self._settings_lock:
            groups = {str(group["id"]): str(group["name"]) for group in self.store.list_groups()}
            old_name = groups.get(group_id)
            old_filename = MyNoteStore._group_file(old_name) if old_name else None
            had_custom_paths = (
                old_filename in self.store.group_note_paths()
                or old_name in self.store.group_resource_directories()
            )
            renamed = self.store.rename_group(group_id, name)
            if not had_custom_paths:
                return renamed
            try:
                self._write_settings(
                    self.store.notes_directory,
                    self.store.group_note_paths(),
                    self.store.group_resource_directories(),
                )
            except StoreError as settings_error:
                try:
                    self.store.rename_group(str(renamed["id"]), old_name)
                except StoreError as rollback_error:
                    raise StoreError(
                        f"分组已改名但设置保存失败，且无法回滚：{settings_error}；{rollback_error}",
                        500,
                    ) from rollback_error
                raise settings_error
            return renamed

    def delete_group(self, group_id: str) -> None:
        with self._settings_lock:
            groups = {str(group["id"]): str(group["name"]) for group in self.store.list_groups()}
            group_name = groups.get(group_id)
            if group_name is None or group_name == MYNOTE_DEFAULT_GROUP:
                self.store.delete_group(group_id)
                return
            filename = MyNoteStore._group_file(group_name)
            current_note_paths = self.store.group_note_paths()
            current_resources = self.store.group_resource_directories()
            if (
                filename not in current_note_paths
                and group_name not in current_resources
            ):
                self.store.delete_group(group_id)
                return
            updated_note_paths = dict(current_note_paths)
            updated_note_paths.pop(filename, None)
            updated_resources = dict(current_resources)
            updated_resources.pop(group_name, None)
            self._write_settings(
                self.store.notes_directory,
                updated_note_paths,
                updated_resources,
            )
            try:
                self.store.delete_group(group_id)
            except StoreError as delete_error:
                try:
                    self._write_settings(
                        self.store.notes_directory,
                        current_note_paths,
                        current_resources,
                    )
                except StoreError as rollback_error:
                    raise StoreError(
                        f"分组删除失败，且无法恢复分组路径设置：{delete_error}；{rollback_error}",
                        500,
                    ) from rollback_error
                raise
            self.store.set_group_note_paths(updated_note_paths)
            self.store.set_group_resource_directories(updated_resources)


class NoteHandler(BaseHTTPRequestHandler):
    server: LocalNoteServer
    protocol_version = "HTTP/1.1"
    server_version = "DictTangoNotes/1.0"

    def log_message(self, format_string: str, *args: object) -> None:
        sys.stdout.write(f"{self.address_string()} - {format_string % args}\n")

    def _send(
        self,
        status: int,
        content: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if self.server.test_mode and content_type.startswith("text/html"):
            content = self._with_debug_panel(content, status)
        headers = dict(extra_headers or {})
        if self.server.test_mode:
            headers.update(self._debug_headers(status))
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _debug_headers(self, status: int) -> dict[str, str]:
        requested_url = quote(self.path, safe="/?=&%")
        return {
            "X-Test-Mode-Page": quote(self._debug_page_label(status), safe=""),
            "X-Test-Mode-Request": f"{self.command} {requested_url}",
            "X-Test-Mode-Status": str(status),
        }

    def _with_debug_panel(self, content: bytes, status: int) -> bytes:
        page_label = self._debug_page_label(status)
        requested_url = html.escape(self.path, quote=True)
        user_agent = html.escape(self.headers.get("User-Agent", "（未提供）")[:300])
        notes_directory = html.escape(str(self.server.store.notes_directory), quote=True)
        panel = f"""<style>
.local-debug-panel{{position:fixed!important;left:14px!important;right:auto!important;
top:auto!important;bottom:14px!important;z-index:2147483647;
max-width:min(460px,calc(100vw - 28px));max-height:45vh;overflow:auto;
padding:10px 12px;border:1px solid #7182a6;border-radius:10px;
color:#172033;background:rgba(245,248,255,.97);box-shadow:0 4px 18px #17203340;
font:12px/1.5 system-ui,sans-serif;overflow-wrap:anywhere}}
.local-debug-panel summary{{cursor:pointer;font-weight:700;color:#25458d}}
.local-debug-panel dl{{display:grid;grid-template-columns:max-content minmax(0,1fr);
gap:3px 10px;margin:8px 0 0}}.local-debug-panel dt{{font-weight:700}}
.local-debug-panel dd{{margin:0}}@media(prefers-color-scheme:dark){{
.local-debug-panel{{color:#e4e9f2;background:rgba(28,34,46,.97);border-color:#63749a}}
.local-debug-panel summary{{color:#b6c6ff}}}}
</style><aside class="local-debug-panel" aria-label="测试模式调试信息">
<details><summary>测试模式 · {html.escape(page_label)}</summary>
<dl><dt>当前页面</dt><dd>{html.escape(page_label)}</dd>
<dt>HTTP 请求</dt><dd>{html.escape(self.command)} {requested_url}</dd>
<dt>响应状态</dt><dd>{status}</dd>
<dt>本机笔记目录</dt><dd>{notes_directory}</dd>
<dt>浏览器</dt><dd>{user_agent}</dd>
<dt>客户端地址</dt><dd>{html.escape(self.client_address[0])}</dd></dl>
</details></aside>"""
        closing_body = re.search(rb"</body\s*>", content, re.IGNORECASE)
        if closing_body:
            return content[: closing_body.start()] + panel.encode("utf-8") + content[closing_body.start() :]
        return content + panel.encode("utf-8")

    def _debug_page_label(self, status: int) -> str:
        if status >= 400:
            return "错误提示页"
        path = urlsplit(self.path).path
        if path in {"/", "/index.html"}:
            return "笔记管理首页"
        if path == "/delete-note":
            return "笔记删除确认页"
        if path == "/editor":
            return "笔记编辑页"
        if path == "/lookup":
            return "笔记查询显示页"
        if path == "/highlights":
            return "高光管理页"
        if path in {"/groups", "/groups/delete"}:
            return "分组兼容重定向"
        if path == "/health":
            return "服务状态接口"
        if path.startswith("/api/"):
            return "笔记 API 接口"
        if path.startswith("/assets/"):
            return "笔记附件资源"
        if path in STATIC_FILES:
            return "网页静态资源"
        return "网页响应"

    def _json(
        self,
        status: int,
        value: object,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._send(
            status,
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            extra_headers,
        )

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _check_local_request(self, write: bool = False) -> bool:
        try:
            request_host = urlsplit("//" + self.headers.get("Host", ""))
            host = request_host.hostname
        except ValueError:
            request_host = None
            host = None
        if host not in {"127.0.0.1", "localhost", "::1"}:
            self._error(403, "只允许从本机访问")
            return False
        if write:
            origin = self.headers.get("Origin")
            referer = self.headers.get("Referer")
            fetch_site = self.headers.get("Sec-Fetch-Site", "").casefold()
            candidate_origin = (
                referer if origin == "null" else origin or referer
            )
            if candidate_origin or fetch_site:
                try:
                    parsed_origin = urlsplit(candidate_origin or "")
                    origin_port = parsed_origin.port or (
                        443 if parsed_origin.scheme == "https" else 80
                    )
                    request_port = request_host.port or 80 if request_host else None
                    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
                    allowed_origin = (
                        parsed_origin.scheme in {"http", "https"}
                        and parsed_origin.hostname in loopback_hosts
                        and (
                            origin_port == request_port
                            or (
                                request_host is not None
                                and request_host.port is None
                                and origin_port == self.server.server_address[1]
                            )
                        )
                        and not parsed_origin.username
                        and not parsed_origin.password
                    )
                except ValueError:
                    allowed_origin = False
                allowed_origin = allowed_origin or fetch_site == "same-origin"
                if not allowed_origin:
                    self.log_error(
                        "Rejected write origin %r, referer %r for host %r",
                        origin,
                        referer,
                        self.headers.get("Host", ""),
                    )
                    self._error(403, "请求来源不被允许")
                    return False
        return True

    def _dictionary_cors_headers(self) -> dict[str, str] | None:
        origin = self.headers.get("Origin")
        if not origin:
            return {}
        try:
            parsed_origin = urlsplit(origin)
            port = parsed_origin.port
        except ValueError:
            return None
        if (
            parsed_origin.scheme != "http"
            or parsed_origin.hostname not in {"127.0.0.1", "localhost", "::1"}
            or port is None
            or not 1 <= port <= 65535
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
            or parsed_origin.username
            or parsed_origin.password
        ):
            return None
        return {
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
        }

    def _read_json(self) -> object:
        content_length = self.headers.get("Content-Length")
        if not content_length:
            raise StoreError("请求正文不能为空")
        try:
            length = int(content_length)
        except ValueError as error:
            raise StoreError("请求长度无效") from error
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise StoreError("请求正文过大", 413)
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StoreError("请求必须是有效的 JSON") from error
        if not isinstance(value, dict):
            raise StoreError("请求正文必须是 JSON 对象")
        return value

    def _read_form_fields(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise StoreError("请求长度无效") from error
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise StoreError("表单内容长度无效", 413)
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/x-www-form-urlencoded":
            raise StoreError("表单格式无效", 415)
        fields = parse_qs(
            self.rfile.read(length).decode("utf-8"),
            keep_blank_values=True,
        )
        return {key: values[-1] for key, values in fields.items()}

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        if self.server.test_mode:
            for name, value in self._debug_headers(303).items():
                self.send_header(name, value)
        self.end_headers()

    @staticmethod
    def _identifier(path: str, prefix: str) -> str:
        suffix = path.removeprefix(prefix)
        if not suffix or "/" in suffix:
            raise StoreError("资源编号无效")
        return suffix

    def do_GET(self) -> None:
        if not self._check_local_request():
            return
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[parsed.path]
            content = (self.server.web_dir / filename).read_bytes()
            extra_headers = {}
            if content_type.startswith("text/html"):
                extra_headers["Content-Security-Policy"] = (
                    "default-src 'self'; connect-src 'self'; img-src 'self' data: https:; "
                    "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                    "base-uri 'self'; object-src 'none'"
                )
            self._send(200, content, content_type, extra_headers)
            return
        if parsed.path == "/health":
            self._json(200, {"ok": True})
            return
        if parsed.path == "/api/dictionary/j5":
            cors_headers = self._dictionary_cors_headers()
            if cors_headers is None:
                self._error(403, "词典接口只允许本机 AnkiDroid 页面读取")
                return
            try:
                with self.server._settings_lock:
                    terms, source_count = self.server.highlight_manager.dictionary_terms()
            except StoreError as error:
                self._json(
                    error.status,
                    {"error": str(error)},
                    cors_headers,
                )
                return
            if not source_count:
                self._json(
                    404,
                    {"error": "J5 词典没有可用词头"},
                    cors_headers,
                )
                return
            self._json(200, {"terms": terms}, cors_headers)
            return
        if parsed.path.startswith("/assets/"):
            try:
                parts = [unquote(part) for part in parsed.path.removeprefix("/assets/").split("/")]
                if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
                    raise StoreError("附件路径无效", 404)
                asset_group = DEFAULT_GROUP if parts[0] == "Default" else parts[0]
                asset = self.server.store.asset_path(asset_group, "/".join(parts[1:]))
                if not asset.is_file():
                    raise StoreError("没有找到这个附件", 404)
                extension = asset.suffix.casefold()
                content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                if extension == ".css":
                    content_type = "text/css; charset=utf-8"
                elif extension == ".js":
                    if not self.server.allow_note_scripts:
                        raise StoreError("笔记脚本未启用", 403)
                    content_type = "text/javascript; charset=utf-8"
                elif extension in {".ttf", ".otf", ".woff", ".woff2"}:
                    content_type = {
                        ".ttf": "font/ttf",
                        ".otf": "font/otf",
                        ".woff": "font/woff",
                        ".woff2": "font/woff2",
                    }[extension]
                elif not (
                    content_type.startswith(("image/", "audio/", "video/"))
                    and content_type != "image/svg+xml"
                ):
                    raise StoreError("不支持显示此附件类型", 415)
                self._send(
                    200,
                    asset.read_bytes(),
                    content_type,
                )
            except StoreError as error:
                self._error(error.status, str(error))
            return
        if parsed.path in {"/groups", "/groups/delete"}:
            self._redirect("/")
            return
        if parsed.path == "/delete-note":
            try:
                note_id = self._identifier(
                    "/api/notes/" + query.get("note_id", [""])[0],
                    "/api/notes/",
                )
                note = self.server.store.get_note(note_id)
                if note is None:
                    raise StoreError("没有找到这条笔记", 404)
            except StoreError as error:
                self._send(error.status, render_error_page(str(error)), "text/html; charset=utf-8")
                return
            page = render_confirmation_page(
                "删除笔记",
                f"确定删除“{note['term']}”这条笔记吗？此操作无法撤销。",
                "/delete-note",
                "note_id",
                str(note_id),
                "/?note=" + str(note_id),
                "确认删除笔记",
            )
            self._send(200, page, "text/html; charset=utf-8")
            return
        if parsed.path == "/editor":
            note_id_value = query.get("note_id", [""])[0]
            if note_id_value:
                try:
                    note_id = self._identifier(f"/api/notes/{note_id_value}", "/api/notes/")
                except StoreError as error:
                    self._send(error.status, render_error_page(str(error)), "text/html; charset=utf-8")
                    return
                note = self.server.store.get_note(note_id)
                if note is None:
                    self._send(404, render_error_page("没有找到这条笔记"), "text/html; charset=utf-8")
                    return
                term = str(note["term"])
                group_name = str(note["group"])
            else:
                note = None
                term = query.get("term", [""])[0]
                group_name = query.get("group", [DEFAULT_GROUP])[0] or DEFAULT_GROUP
            modal = query.get("modal", [""])[0] == "1"
            self._send(
                200,
                render_editor_page(
                    term,
                    group_name,
                    note,
                    modal,
                    self.server.store.list_groups(),
                ),
                "text/html; charset=utf-8",
            )
            return
        if parsed.path == "/api/groups":
            self._json(200, self.server.store.list_groups())
            return
        if parsed.path == "/api/highlights":
            try:
                page = int(query.get("page", ["1"])[0])
                page_size = int(query.get("page_size", ["50"])[0])
                with self.server._settings_lock:
                    result = self.server.highlight_manager.list_highlights(
                        query.get("q", [""])[0],
                        query.get("status", ["all"])[0],
                        query.get("category", [""])[0],
                        page,
                        page_size,
                    )
                self._json(200, result)
            except ValueError:
                self._error(400, "分页参数无效")
            except StoreError as error:
                self._error(error.status, str(error))
            return
        if parsed.path == "/api/settings/notes-directory":
            self._json(
                200,
                {"notes_directory": str(self.server.store.notes_directory)},
            )
            return
        if parsed.path == "/api/settings/highlight-rules":
            with self.server._settings_lock:
                self._json(
                    200,
                    {
                        "highlight_rules_path": str(
                            self.server.highlight_manager.settings_path.resolve()
                        )
                    },
                )
            return
        if parsed.path == "/api/settings/sync-directory":
            with self.server.sync_manager._lock:
                self._json(
                    200,
                    {
                        "sync_directory": (
                            str(self.server.sync_manager.sync_directory)
                            if self.server.sync_manager.sync_directory
                            else ""
                        )
                    },
                )
            return
        if parsed.path == "/api/sync/status":
            try:
                self._json(200, self.server.sync_manager.status())
            except StoreError as error:
                self._error(error.status, str(error))
            return
        if parsed.path == "/api/settings/group-resources":
            self._json(
                200,
                {"group_resource_directories": self.server.store.group_resource_directories()},
            )
            return
        if parsed.path == "/api/settings/group-paths":
            self._json(200, {"group_paths": self.server.store.group_paths()})
            return
        if parsed.path == "/api/backup/export":
            self._json(200, self.server.store.export_data())
            return
        if parsed.path == "/backup/export.json":
            filename = f"dict-tango-notes-{datetime.now().date().isoformat()}.json"
            self._send(
                200,
                json.dumps(self.server.store.export_data(), ensure_ascii=False, indent=2).encode("utf-8"),
                "application/json; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="{filename}"'},
            )
            return
        if parsed.path == "/api/notes":
            group_value = query.get("group_id", [""])[0]
            group_id = group_value or None
            try:
                notes = self.server.store.list_notes(query.get("q", [""])[0], group_id)
            except StoreError as error:
                self._error(error.status, str(error))
                return
            for note in notes:
                note["body_html"] = render_note_body(str(note["body"]), str(note["group"]))
            self._json(200, notes)
            return
        if parsed.path.startswith("/api/notes/"):
            try:
                note_id = self._identifier(parsed.path, "/api/notes/")
            except StoreError as error:
                self._error(error.status, str(error))
                return
            note = self.server.store.get_note(note_id)
            if note is None:
                self._error(404, "没有找到这条笔记")
                return
            note["body_html"] = render_note_body(str(note["body"]), str(note["group"]))
            self._json(200, note)
            return
        if parsed.path == "/lookup":
            term = next(
                (
                    query[key][0]
                    for key in ("word", "q", "keyword", "search", "term")
                    if key in query
                ),
                "",
            )
            if term.endswith("?edit=true"):
                term = term[: -len("?edit=true")]
            group_name = query.get("group", [""])[0]
            notes = self.server.store.lookup(term, group_name)
            self._send(
                200,
                render_lookup_page(
                    term,
                    notes,
                    group_name,
                    self.server.store.lookup,
                    self.server.store.group_resources,
                    self.server.allow_note_scripts,
                ),
                "text/html; charset=utf-8",
            )
            return
        self._error(404, "没有找到这个页面")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        if not self._check_local_request(write=True):
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/groups":
            try:
                form = self._read_form_fields()
                action = form.get("action", "")
                if action == "set_notes_directory":
                    self.server.set_notes_directory(form.get("notes_directory", ""))
                elif action == "create":
                    self.server.create_group(form.get("name", ""))
                elif action == "rename":
                    group_id = self._identifier(
                        "/api/groups/" + form.get("group_id", ""),
                        "/api/groups/",
                    )
                    self.server.rename_group(group_id, form.get("name", ""))
                else:
                    raise StoreError("分组操作无效")
                self._redirect("/")
            except (StoreError, UnicodeDecodeError) as error:
                status = error.status if isinstance(error, StoreError) else 400
                self._send(status, render_error_page(str(error)), "text/html; charset=utf-8")
            return
        if parsed.path == "/groups/delete":
            try:
                form = self._read_form_fields()
                group_id = self._identifier(
                    "/api/groups/" + form.get("group_id", ""),
                    "/api/groups/",
                )
                self.server.delete_group(group_id)
                self._redirect("/")
            except (StoreError, UnicodeDecodeError) as error:
                status = error.status if isinstance(error, StoreError) else 400
                self._send(status, render_error_page(str(error)), "text/html; charset=utf-8")
            return
        if parsed.path == "/delete-note":
            try:
                form = self._read_form_fields()
                note_id = self._identifier(
                    "/api/notes/" + form.get("note_id", ""),
                    "/api/notes/",
                )
                if not self.server.store.delete_note(note_id):
                    raise StoreError("没有找到这条笔记", 404)
                return_term = form.get("return_term", "")
                if return_term:
                    return_group = form.get("return_group", "")
                    return_query = {"word": return_term}
                    if return_group:
                        return_query["group"] = return_group
                    self._redirect("/lookup?" + urlencode(return_query))
                else:
                    self._redirect("/")
            except (StoreError, UnicodeDecodeError) as error:
                status = error.status if isinstance(error, StoreError) else 400
                self._send(status, render_error_page(str(error)), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/assets":
            try:
                data = self._read_json()
                encoded = data.get("content")
                if not isinstance(encoded, str):
                    raise StoreError("附件内容无效")
                try:
                    content = base64.b64decode(encoded, validate=True)
                except ValueError as error:
                    raise StoreError("附件编码无效") from error
                filename = self.server.store.save_asset(
                    str(data.get("group", DEFAULT_GROUP)),
                    str(data.get("filename", "")),
                    content,
                )
                self._json(201, {"filename": filename})
            except StoreError as error:
                self._error(error.status, str(error))
            return
        if parsed.path == "/api/paste":
            try:
                data = self._read_json()
                source = data.get("html")
                mode = data.get("mode")
                group = data.get("group", DEFAULT_GROUP)
                localize_images = data.get("localize_images", True)
                if not isinstance(source, str):
                    raise StoreError("剪贴板 HTML 内容无效")
                if len(source) > MAX_BODY_LENGTH:
                    raise StoreError("剪贴板内容过长", 413)
                if not isinstance(mode, str):
                    raise StoreError("粘贴清理模式无效")
                if not isinstance(group, str) or len(group) > MAX_GROUP_LENGTH:
                    raise StoreError("分组名称无效")
                if not isinstance(localize_images, bool):
                    raise StoreError("图片本地化选项无效")
                localization_warnings: list[str] = []
                localized_files: list[str] = []

                def save_pasted_image(
                    image_group: str,
                    filename: str,
                    content: bytes,
                ) -> str:
                    saved_filename = self.server.store.save_asset(
                        image_group,
                        filename,
                        content,
                    )
                    localized_files.append(saved_filename)
                    return saved_filename

                cleaned = sanitize_paste_html(
                    source,
                    group,
                    mode,
                    save_pasted_image,
                    localization_warnings,
                    localize_images,
                )
                if len(cleaned) > MAX_BODY_LENGTH:
                    raise StoreError("清理后的剪贴板内容过长", 413)
                self._json(
                    200,
                    {
                        "html": cleaned,
                        "warnings": localization_warnings,
                        "localized_images": len(localized_files),
                        "localization_enabled": localize_images,
                    },
                )
            except StoreError as error:
                self._error(error.status, str(error))
            return
        if parsed.path == "/edit":
            self._error(404, "DictTango 查询页仅用于显示笔记，请在浏览器管理页编辑")
            return
        if parsed.path == "/editor":
            try:
                form = self._read_form_fields()
                try:
                    image_cleanup_candidates = json.loads(
                        form.get("deleted_image_assets", "[]")
                    )
                except json.JSONDecodeError as error:
                    raise StoreError("待清理图片列表无效") from error
                if (
                    not isinstance(image_cleanup_candidates, list)
                    or len(image_cleanup_candidates) > 500
                ):
                    raise StoreError("待清理图片列表无效")
                cleanup_groups: dict[str, list[str]] = {}
                for candidate in image_cleanup_candidates:
                    if (
                        not isinstance(candidate, dict)
                        or not isinstance(candidate.get("group"), str)
                        or not isinstance(candidate.get("filename"), str)
                    ):
                        raise StoreError("待清理图片列表无效")
                    candidate_group = candidate["group"]
                    filename = candidate["filename"]
                    media_type = mimetypes.guess_type(filename)[0] or ""
                    if (
                        Path(filename).name != filename
                        or "/" in filename
                        or "\\" in filename
                        or not media_type.startswith("image/")
                        or media_type == "image/svg+xml"
                    ):
                        raise StoreError("待清理图片列表无效")
                    self.server.store.asset_path(candidate_group, filename)
                    cleanup_groups.setdefault(candidate_group, []).append(filename)
                note_data = {
                    "term": form.get("term", ""),
                    "group": form.get("group", DEFAULT_GROUP),
                    "body": form.get("body", ""),
                }
                note_id = form.get("note_id", "").strip()
                if note_id:
                    saved_note = self.server.store.update_note(note_id, note_data)
                else:
                    saved_note = self.server.store.create_note(note_data)
                for candidate_group, filenames in cleanup_groups.items():
                    self.server.store.delete_unreferenced_image_assets(
                        candidate_group,
                        filenames,
                    )
                if form.get("modal") == "1":
                    location = "/editor?" + urlencode(
                        {
                            "modal": "1",
                            "saved": "1",
                            "note_id": saved_note["id"],
                        }
                    )
                else:
                    location = "/?" + urlencode({"note": saved_note["id"]})
                self._redirect(location)
            except (StoreError, UnicodeDecodeError, ValueError) as error:
                status = error.status if isinstance(error, StoreError) else 400
                self._send(
                    status,
                    render_error_page(str(error)),
                    "text/html; charset=utf-8",
                )
            return
        try:
            data = self._read_json()
            if parsed.path == "/api/sync/run":
                if data:
                    raise StoreError("同步请求参数无效")
                with self.server._settings_lock:
                    result = self.server.sync_manager.sync()
                self._json(200, result)
                return
            if parsed.path == "/api/sync/resolve":
                logical_path = data.get("path")
                choice = data.get("choice")
                if not isinstance(logical_path, str) or not isinstance(choice, str):
                    raise StoreError("冲突解决请求格式无效")
                with self.server._settings_lock:
                    result = self.server.sync_manager.resolve_conflict(
                        logical_path, choice
                    )
                self._json(200, result)
                return
            if parsed.path == "/api/notes":
                note = self.server.store.create_note(data)
                self._json(201, note)
                return
            if parsed.path == "/api/groups":
                note_path = data.get("note_path", "")
                resource_directory = data.get("resource_directory", "")
                if not isinstance(note_path, str) or not isinstance(resource_directory, str):
                    raise StoreError("分组路径设置格式无效")
                group = self.server.create_group(
                    data.get("name", ""),
                    note_path,
                    resource_directory,
                )
                self._json(201, group)
                return
            if parsed.path == "/api/backup/import":
                result = self.server.store.import_data(data)
                self._json(200, result)
                return
        except StoreError as error:
            self._error(error.status, str(error))
            return
        self._error(404, "没有找到这个页面")

    def do_PUT(self) -> None:
        if not self._check_local_request(write=True):
            return
        parsed = urlsplit(self.path)
        try:
            data = self._read_json()
            if parsed.path.startswith("/api/notes/"):
                note_id = self._identifier(parsed.path, "/api/notes/")
                self._json(200, self.server.store.update_note(note_id, data))
                return
            if parsed.path.startswith("/api/groups/"):
                group_id = self._identifier(parsed.path, "/api/groups/")
                self._json(200, self.server.rename_group(group_id, data.get("name", "")))
                return
            if parsed.path == "/api/settings/notes-directory":
                if not isinstance(data, dict):
                    raise StoreError("笔记根文件夹设置格式无效")
                notes_directory = data.get("notes_directory")
                if not isinstance(notes_directory, str):
                    raise StoreError("笔记根文件夹路径无效")
                self.server.set_notes_directory(notes_directory)
                self._json(
                    200,
                    {"notes_directory": str(self.server.store.notes_directory)},
                )
                return
            if parsed.path == "/api/settings/highlight-rules":
                if not isinstance(data, dict):
                    raise StoreError("高光数据路径设置格式无效")
                path = data.get("highlight_rules_path")
                if not isinstance(path, str):
                    raise StoreError("高光数据文件路径无效")
                saved_path = self.server.set_highlight_rules_path(path)
                self._json(200, {"highlight_rules_path": str(saved_path)})
                return
            if parsed.path == "/api/settings/sync-directory":
                if not isinstance(data, dict):
                    raise StoreError("同步文件夹设置格式无效")
                directory = data.get("sync_directory")
                if not isinstance(directory, str):
                    raise StoreError("同步文件夹路径无效")
                configured_path = self.server.set_sync_directory(directory)
                self._json(
                    200,
                    {
                        "sync_directory": (
                            str(configured_path) if configured_path else ""
                        )
                    },
                )
                return
            if parsed.path == "/api/settings/group-resources":
                group = data.get("group")
                directory = data.get("directory")
                if not isinstance(group, str) or not isinstance(directory, str):
                    raise StoreError("分组资源文件夹设置格式无效")
                self.server.set_group_resource_directory(group, directory)
                self._json(
                    200,
                    {
                        "group": group,
                        "directory": self.server.store.group_resource_directories().get(group, ""),
                        "group_resource_directories": self.server.store.group_resource_directories(),
                    },
                )
                return
            if parsed.path == "/api/settings/group-paths":
                group = data.get("group")
                note_path = data.get("note_path")
                resource_directory = data.get("resource_directory")
                if (
                    not isinstance(group, str)
                    or not isinstance(note_path, str)
                    or not isinstance(resource_directory, str)
                ):
                    raise StoreError("分组笔记文件路径设置格式无效")
                self.server.set_group_paths(group, note_path, resource_directory)
                self._json(
                    200,
                    {"group_paths": self.server.store.group_paths()},
                )
                return
            if parsed.path == "/api/highlights":
                with self.server._settings_lock:
                    result = self.server.highlight_manager.update_highlights(data)
                self._json(200, result)
                return
        except StoreError as error:
            self._error(error.status, str(error))
            return
        self._error(404, "没有找到这个页面")

    def do_DELETE(self) -> None:
        if not self._check_local_request(write=True):
            return
        parsed = urlsplit(self.path)
        try:
            if parsed.path.startswith("/api/notes/"):
                note_id = self._identifier(parsed.path, "/api/notes/")
                if not self.server.store.delete_note(note_id):
                    self._error(404, "没有找到这条笔记")
                    return
                self._json(200, {"deleted": True})
                return
            if parsed.path.startswith("/api/groups/"):
                group_id = self._identifier(parsed.path, "/api/groups/")
                self.server.delete_group(group_id)
                self._json(200, {"deleted": True})
                return
        except StoreError as error:
            self._error(error.status, str(error))
            return
        self._error(404, "没有找到这个页面")


def main() -> None:
    parser = argparse.ArgumentParser(description="DictTango 本地笔记词典")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--notes", type=Path)
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="在 HTML 页面显示当前页面和请求调试信息",
    )
    parser.add_argument(
        "--allow-note-scripts",
        action="store_true",
        help="允许查询页运行分组资源目录中的 JavaScript；仅对可信笔记启用",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    notes_directory = args.notes
    try:
        load_group_note_paths(DEFAULT_SETTINGS_PATH)
        load_group_resource_directories(DEFAULT_SETTINGS_PATH)
        if notes_directory is None:
            notes_directory = load_notes_directory_setting(DEFAULT_SETTINGS_PATH)
    except ValueError as error:
        parser.error(str(error))
    if notes_directory is None:
        notes_directory = notes_directory or DEFAULT_NOTES_DIR
    try:
        server = LocalNoteServer(
            (args.host, args.port),
            notes_directory,
            WEB_DIR,
            args.database,
            allow_note_scripts=args.allow_note_scripts,
            test_mode=args.test_mode,
        )
    except ValueError as error:
        parser.error(str(error))
    print(f"笔记网页已启动：http://127.0.0.1:{args.port}/")
    print(f"DictTango 查询地址：http://127.0.0.1:{args.port}/lookup?word=<查询词>")
    print(f"MyNote 笔记目录：{notes_directory.resolve()}")
    if args.test_mode:
        print("测试模式已开启：网页会显示当前页面和请求调试信息。")
    if args.allow_note_scripts:
        print("已允许运行笔记分组目录中的 JavaScript；请仅使用可信笔记。")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务…")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
