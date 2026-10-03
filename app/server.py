from __future__ import annotations

import json
import mimetypes
import os
import threading
import urllib.error
import urllib.request
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .annotations import (
    MAX_ANNOTATION_BODY_BYTES,
    AnnotationNotFound,
    RevisionMismatch,
    add_annotation,
    delete_annotation,
    export_annotated,
    get_annotations,
)
from .core import DATA, JOBS, MAX_UPLOAD_BYTES, create_job, create_job_from_path, file_path, get_workspace_preferences, list_jobs, load_job, public_settings, read_settings, render_page, save_settings, save_workspace_preferences, translate_job
from .library import MAX_LIBRARY_BODY_BYTES, apply_action, get_library
from .markdown_export import MarkdownExportError, markdown_directory

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PAPER_TRANSLATOR_PORT", "8765"))
LOCAL_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
FILE_CHUNK_SIZE = 64 * 1024


def _parse_single_range(value: str | None, size: int) -> tuple[int, int] | str | None:
    """Parse one satisfiable byte range; leave unsupported Range headers at 200."""
    if not value or not value.startswith("bytes="):
        return None
    spec = value[6:].strip()
    if not spec or "," in spec or "-" not in spec:
        return None
    first, last = (part.strip() for part in spec.split("-", 1))
    try:
        if not first:
            suffix = int(last)
            if suffix <= 0 or size <= 0:
                return "unsatisfiable"
            return max(0, size - suffix), size - 1
        start = int(first)
        if start < 0 or start >= size:
            return "unsatisfiable"
        if not last:
            return start, size - 1
        end = int(last)
        if end < start:
            return "unsatisfiable"
        return start, min(end, size - 1)
    except ValueError:
        return None


def test_connection() -> dict[str, object]:
    """Make one deliberately tiny completion request to verify auth/model."""
    settings = read_settings()
    if not settings["api_key"]:
        return {"ok": False, "category": "missing_key", "message": "尚未保存 API Key。"}
    url = settings["base_url"].rstrip("/") + "/chat/completions"
    request_body = json_bytes({"model": settings["model"], "messages": [{"role": "user", "content": "Reply with OK only."}], "max_tokens": 1, "temperature": 0})
    request = urllib.request.Request(url, data=request_body, method="POST", headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings['api_key']}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read(4096)
        return {"ok": True, "category": "ok", "message": "连接成功：已用 max_tokens=1 验证认证和模型。", "model": settings["model"]}
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            category, message = "auth", "鉴权失败：请检查 API Key 是否有效及是否有权限调用该服务。"
        elif exc.code == 404:
            category, message = "model_or_url", "模型或 Base URL 不存在：请核对兼容接口路径和模型名。"
        elif exc.code == 429:
            category, message = "quota", "服务返回限流或额度不足，请稍后重试或检查账户额度。"
        else:
            category, message = "provider", f"服务返回 HTTP {exc.code}，请检查服务商状态和设置。"
        return {"ok": False, "category": category, "message": message}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"ok": False, "category": "network", "message": "网络连接失败：请检查 Base URL、网络或代理设置。"}
    except Exception:
        return {"ok": False, "category": "provider", "message": "服务返回格式异常，请检查兼容接口设置。"}


def json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "PaperTranslator/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep request logs useful while never printing request bodies/headers.
        if self.path.startswith("/api/settings"):
            safe_path = "/api/settings"
        else:
            safe_path = self.path.split("?", 1)[0]
        print(f"[{self.log_date_time_string()}] {self.command} {safe_path}")

    def _send(self, status: int, payload: bytes, content_type: str = "application/json; charset=utf-8", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, payload: object) -> None:
        self._send(status, json_bytes(payload))

    def _read_body(self, max_bytes: int | None = None) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        limit = max_bytes if max_bytes is not None else MAX_UPLOAD_BYTES + 5_000_000
        if length < 0 or length > limit:
            raise ValueError("上传请求过大。")
        return self.rfile.read(length)

    def _check_local_write(self, content_type: str) -> None:
        self._check_local_host()
        origin = self.headers.get("Origin")
        if origin and origin not in LOCAL_ORIGINS:
            raise ValueError("拒绝来自其他网站的本地写入请求。")
        if self.headers.get("X-Paper-Translator") != "1":
            raise ValueError("缺少本地应用请求标记。")
        if not content_type.startswith(content_type.split(";", 1)[0]):
            raise ValueError("请求格式无效。")

    def _check_local_host(self) -> None:
        host = self.headers.get("Host", "").lower()
        if host not in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}:
            raise ValueError("仅允许通过本机地址和端口访问。")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            try:
                self._check_local_host()
            except ValueError as exc:
                self._json(403, {"error": str(exc)})
                return
        if path == "/api/health":
            self._json(200, {"ok": True, "service": "paper-translator"})
            return
        if path == "/api/settings":
            self._json(200, public_settings())
            return
        if path == "/api/jobs":
            self._json(200, {"jobs": list_jobs()})
            return
        if path == "/api/library":
            self._json(200, get_library())
            return
        if path == "/api/workspace-preferences":
            self._json(200, get_workspace_preferences())
            return
        parts = [x for x in path.split("/") if x]
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "jobs" and parts[3] == "markdown-location":
            try:
                directory = markdown_directory(JOBS, parts[2])
            except MarkdownExportError as exc:
                status = 400 if "ID" in str(exc) or "不安全" in str(exc) else 404
                self._json(status, {"error": str(exc)})
                return
            self._json(200, {"directory": str(directory)})
            return
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "jobs":
            job = load_job(parts[2])
            if not job:
                self._json(404, {"error": "任务不存在。"})
            else:
                self._json(200, job)
            return
        if len(parts) == 5 and parts[0] == "api" and parts[1] == "jobs" and parts[3] in {"source", "translated"} and parts[4] == "annotations":
            try:
                self._json(200, get_annotations(parts[2], parts[3]))
            except FileNotFoundError as exc:
                self._json(404, {"error": str(exc)})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        if len(parts) == 5 and parts[0] == "api" and parts[1] == "jobs" and parts[3] in {"source", "translated"} and parts[4] == "annotated":
            try:
                payload, _revision = export_annotated(parts[2], parts[3])
                self._send(200, payload, "application/pdf", {"Content-Disposition": f'attachment; filename="annotated-{parts[3]}.pdf"'})
            except FileNotFoundError as exc:
                self._json(404, {"error": str(exc)})
            except (ValueError, RuntimeError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "jobs" and parts[3] in {"source", "translated", "bilingual"}:
            path_obj = file_path(parts[2], parts[3])
            if not path_obj:
                self._json(404, {"error": "文件尚未生成或任务不存在。"})
                return
            self._send_file(path_obj, inline=True)
            return
        if len(parts) == 6 and parts[0] == "api" and parts[1] == "jobs" and parts[3] in {"source", "translated", "bilingual"} and parts[4] == "page":
            try:
                # The browser requests the image as /page/{n}.png.  Strip the
                # representation suffix before validating the page number.
                page_number = int(parts[5].split(".", 1)[0])
                zoom = int(parse_qs(parsed.query).get("zoom", ["100"])[0])
                image = render_page(parts[2], parts[3], page_number, zoom)
                self._send(200, image, "image/png")
            except (ValueError, FileNotFoundError, RuntimeError) as exc:
                self._json(404, {"error": str(exc)})
            return
        if path == "/" or path == "/index.html":
            self._send_file(STATIC / "index.html", inline=True)
            return
        if path.startswith("/static/"):
            candidate = (STATIC / path.removeprefix("/static/")).resolve()
            try:
                candidate.relative_to(STATIC.resolve())
            except ValueError:
                self._json(404, {"error": "资源不存在。"})
                return
            self._send_file(candidate, inline=True)
            return
        self._json(404, {"error": "未找到请求路径。"})

    def _send_file(self, path: Path, inline: bool) -> None:
        if not path.is_file():
            self._json(404, {"error": "文件不存在。"})
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        disposition = "inline" if inline else "attachment"
        try:
            size = path.stat().st_size
        except OSError:
            self._json(404, {"error": "文件不存在。"})
            return
        range_header = self.headers.get("Range")
        accepts_range = content_type == "application/pdf"
        byte_range = _parse_single_range(range_header, size) if accepts_range and not self.headers.get("If-Range") else None
        if byte_range == "unsatisfiable":
            try:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Disposition", f'{disposition}; filename="{path.name}"')
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        start, end = byte_range if byte_range else (0, size - 1)
        status = 206 if byte_range else 200
        length = max(0, end - start + 1)
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Disposition", f'{disposition}; filename="{path.name}"')
            if accepts_range:
                self.send_header("Accept-Ranges", "bytes")
            if byte_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not length:
                return
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(FILE_CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            request_content_type = self.headers.get("Content-Type", "")
            self._check_local_write(request_content_type)
            route_parts = [x for x in path.split("/") if x]
            annotation_route = len(route_parts) == 5 and route_parts[:2] == ["api", "jobs"] and route_parts[3] in {"source", "translated"} and route_parts[4] == "annotations"
            local_import_route = path == "/api/jobs/import-local"
            body_limit = MAX_ANNOTATION_BODY_BYTES if annotation_route else MAX_LIBRARY_BODY_BYTES if path == "/api/library" else 64 * 1024 if local_import_route else None
            body = self._read_body(body_limit)
            if local_import_route:
                if self.headers.get("X-Paper-Translator-Desktop") != "1":
                    raise ValueError("本地路径导入只接受桌面原生文件选择桥。")
                origin = self.headers.get("Origin")
                if origin not in {None, "", "null"}:
                    raise ValueError("本地路径导入不能由网页 Origin 调用。")
                if request_content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("本地路径导入必须使用 application/json。")
                payload = json.loads(body.decode("utf-8")) if body else {}
                if not isinstance(payload, dict):
                    raise ValueError("本地导入参数格式无效。")
                job = create_job_from_path(str(payload.get("path", "")), str(payload.get("pages", "")), str(payload.get("mode", "full")), bool(payload.get("demo_mode", False)), start_translation=False)
                self._json(202, {"job": job})
                return
            if path == "/api/library":
                if request_content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("文件库操作必须使用 application/json。")
                payload = json.loads(body.decode("utf-8")) if body else {}
                self._json(200, apply_action(payload))
                return
            if path == "/api/settings":
                if request_content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("设置保存必须使用 application/json。")
                payload = json.loads(body.decode("utf-8")) if body else {}
                if not isinstance(payload, dict):
                    raise ValueError("设置格式无效。")
                self._json(200, {"settings": save_settings(payload)})
                return
            if path == "/api/workspace-preferences":
                if request_content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("工作区偏好必须使用 application/json。")
                payload = json.loads(body.decode("utf-8")) if body else {}
                self._json(200, save_workspace_preferences(payload))
                return
            if path == "/api/test-connection":
                result = test_connection()
                self._json(200, result)
                return
            if path == "/api/jobs":
                content_type = request_content_type
                if not content_type.startswith("multipart/form-data"):
                    raise ValueError("请使用 PDF 文件上传表单。")
                message = BytesParser(policy=policy.default).parsebytes((f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
                fields: dict[str, str] = {}
                file_name = "paper.pdf"
                file_content = b""
                for part in message.iter_parts():
                    disposition = part.get("Content-Disposition", "")
                    name = part.get_param("name", header="content-disposition")
                    if name == "file":
                        file_name = part.get_filename() or file_name
                        file_content = part.get_payload(decode=True) or b""
                    elif name:
                        value = part.get_payload(decode=True) or b""
                        fields[name] = value.decode("utf-8", "replace")
                import_only = fields.get("mode", "full") == "import"
                job = create_job(file_name, file_content, fields.get("pages", ""), fields.get("mode", "full"), fields.get("demo_mode", "false").lower() == "true", start_translation=not import_only)
                self._json(202, {"job": job})
                return
            parts = route_parts
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "jobs" and parts[3] == "translate":
                if request_content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("翻译启动必须使用 application/json。")
                payload = json.loads(body.decode("utf-8")) if body else {}
                if not isinstance(payload, dict):
                    raise ValueError("翻译参数格式无效。")
                job = translate_job(parts[2], str(payload.get("mode", "full")), str(payload.get("pages", "")), bool(payload.get("demo_mode", False)))
                self._json(202, {"job": job})
                return
            if annotation_route:
                if request_content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("批注保存必须使用 application/json。")
                payload = json.loads(body.decode("utf-8")) if body else {}
                if not isinstance(payload, dict):
                    raise ValueError("批注参数格式无效。")
                action = payload.get("action")
                try:
                    if action == "add":
                        result = add_annotation(parts[2], parts[3], payload)
                    elif action == "delete":
                        result = delete_annotation(parts[2], parts[3], payload)
                    else:
                        raise ValueError("批注 action 必须是 add 或 delete。")
                except RevisionMismatch as exc:
                    self._json(409, {"error": str(exc), "revision": exc.current_revision})
                    return
                except AnnotationNotFound as exc:
                    self._json(404, {"error": str(exc)})
                    return
                self._json(200, result)
                return
            self._json(404, {"error": "未找到请求路径。"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except OSError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": f"服务器处理失败：{exc}"})


def run() -> None:
    DATA.mkdir(exist_ok=True)
    JOBS.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"论文双语阅读器已启动：http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
