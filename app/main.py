from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import uuid
import zipfile
from io import BytesIO
from json import JSONDecodeError
from tempfile import NamedTemporaryFile, TemporaryDirectory, mkdtemp
from typing import Iterable

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pdf2image import convert_from_path
from playwright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

app = FastAPI(title="PDF to JPEG service")

BOUNDARY_PREFIX = "pdf-image-boundary"
CHUNK_SIZE = 1024 * 1024
ZIP_FILENAME = "pages.zip"

PDF_EXTENSIONS = {".pdf"}
DOCUMENT_EXTENSIONS = {".doc", ".docx"}
SPREADSHEET_EXTENSIONS = {".xls", ".xlsx"}
PRESENTATION_EXTENSIONS = {".ppt", ".pptx"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi"}

SUPPORTED_EXTENSIONS = (
    PDF_EXTENSIONS
    | DOCUMENT_EXTENSIONS
    | SPREADSHEET_EXTENSIONS
    | PRESENTATION_EXTENSIONS
    | VIDEO_EXTENSIONS
)


def _get_upload_suffix(upload: UploadFile) -> str:
    if upload.filename:
        _, ext = os.path.splitext(upload.filename)
        if ext:
            return ext.lower()

    content_type = (upload.content_type or "").lower()
    content_type_mapping = {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/x-msvideo": ".avi",
    }

    return content_type_mapping.get(content_type, "")


def _categorize_extension(suffix: str) -> str:
    if suffix in PDF_EXTENSIONS:
        return "pdf"
    if suffix in DOCUMENT_EXTENSIONS | SPREADSHEET_EXTENSIONS | PRESENTATION_EXTENSIONS:
        return "office"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return "unknown"


def _convert_office_to_pdf(source_path: str) -> tuple[str, str]:
    output_dir = mkdtemp()
    commands = ("libreoffice", "soffice")
    stdout_data = stderr_data = ""

    for executable in commands:
        try:
            result = subprocess.run(
                [
                    executable,
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    output_dir,
                    source_path,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError as exc:
            stderr_data = exc.stderr.decode("utf-8", errors="ignore")
            stdout_data = exc.stdout.decode("utf-8", errors="ignore")
            shutil.rmtree(output_dir, ignore_errors=True)
            detail = stderr_data.strip() or stdout_data.strip() or "Unknown conversion error"
            raise HTTPException(status_code=500, detail=f"Failed to convert document to PDF: {detail}") from exc
        else:
            stdout_data = result.stdout.decode("utf-8", errors="ignore")
            stderr_data = result.stderr.decode("utf-8", errors="ignore")
            break
    else:  # pragma: no cover - defensive branch
        shutil.rmtree(output_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail="LibreOffice (libreoffice or soffice) is required for document conversion",
        )

    base_name = os.path.splitext(os.path.basename(source_path))[0] + ".pdf"
    pdf_path = os.path.join(output_dir, base_name)

    if not os.path.exists(pdf_path):
        detail = stderr_data.strip() or stdout_data.strip() or "PDF file was not created"
        shutil.rmtree(output_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Failed to locate converted PDF: {detail}")

    return pdf_path, output_dir


def _extract_video_frame(video_path: str, output_dir: str) -> list[str]:
    output_path = os.path.join(output_dir, "frame-1.jpg")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                output_path,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:  # pragma: no cover - external dependency
        raise HTTPException(status_code=500, detail="FFmpeg is required for video conversion") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="ignore").strip()
        if not detail:
            detail = exc.stdout.decode("utf-8", errors="ignore").strip()
        raise HTTPException(status_code=500, detail=f"Failed to extract video frame: {detail}") from exc

    if not os.path.exists(output_path):
        raise HTTPException(status_code=500, detail="Video frame extraction did not produce an image")

    return [output_path]


def _convert_pdf_to_jpeg_paths(pdf_path: str, output_dir: str) -> list[str]:
    try:
        return convert_from_path(
            pdf_path,
            fmt="jpeg",
            output_folder=output_dir,
            output_file="page",
            paths_only=True,
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        raise HTTPException(status_code=500, detail=f"Failed to convert PDF: {exc}") from exc


def _multipart_stream(image_paths: Iterable[str], boundary: str) -> Iterable[bytes]:
    for index, image_path in enumerate(image_paths, start=1):
        filename = f"page-{index}.jpg"
        content_length = os.path.getsize(image_path)
        headers = (
            f"--{boundary}\r\n"
            "Content-Type: image/jpeg\r\n"
            f"Content-Disposition: attachment; filename=\"{filename}\"\r\n"
            f"Content-Length: {content_length}\r\n\r\n"
        )
        yield headers.encode("latin-1")
        with open(image_path, "rb") as image_file:
            while True:
                chunk = image_file.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        yield b"\r\n"
    yield f"--{boundary}--\r\n".encode("latin-1")


def _stream_file(path: str) -> Iterable[bytes]:
    with open(path, "rb") as file_obj:
        while True:
            chunk = file_obj.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk


def _create_zip_archive(image_paths: Iterable[str]) -> str:
    with NamedTemporaryFile(delete=False, suffix=".zip") as tmp_zip:
        zip_path = tmp_zip.name

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for index, image_path in enumerate(image_paths, start=1):
            arcname = f"page-{index}.jpg"
            zip_file.write(image_path, arcname=arcname)

    return zip_path


def _wants_zip(response_format: str | None, accept_header: str | None) -> bool:
    if response_format and response_format.lower() == "zip":
        return True
    if not accept_header:
        return False
    for item in accept_header.split(","):
        media_type = item.split(";")[0].strip().lower()
        if media_type in {"application/zip", "application/x-zip-compressed"}:
            return True
    return False


def _wants_json(response_format: str | None, accept_header: str | None) -> bool:
    if response_format:
        return response_format.lower() == "json"
    if not accept_header:
        return False
    for item in accept_header.split(","):
        media_type = item.split(";")[0].strip().lower()
        if media_type == "application/json":
            return True
    return False


def _require_http_url(value: str | None) -> str:
    if not value:
        raise HTTPException(status_code=400, detail="URL parameter is required")
    if not value.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    return value


async def _capture_url_screenshot(url: str) -> bytes:
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                context = await browser.new_context(viewport={"width": 1920, "height": 1080})
                try:
                    page = await context.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                    return await page.screenshot(type="jpeg", quality=90)
                finally:
                    await context.close()
            finally:
                await browser.close()
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        raise HTTPException(status_code=400, detail=f"Failed to capture screenshot: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected screenshot error: {exc}") from exc


async def _write_upload_to_tempfile(upload: UploadFile, suffix: str) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ""
    with NamedTemporaryFile(delete=False, suffix=suffix or "") as tmp_file:
        while True:
            chunk = await upload.read(CHUNK_SIZE)
            if not chunk:
                break
            tmp_file.write(chunk)
        return tmp_file.name


@app.post("/screenshot")
async def screenshot(request: Request, url: str | None = Query(default=None)) -> StreamingResponse:
    body_url: str | None = None

    if url is None:
        raw_body = await request.body()
        if raw_body:
            try:
                payload = json.loads(raw_body)
            except JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
            if isinstance(payload, dict):
                body_url = payload.get("url")
    target_url = _require_http_url(url or body_url)

    image_bytes = await _capture_url_screenshot(target_url)
    buffer = BytesIO(image_bytes)

    def content() -> Iterable[bytes]:
        try:
            buffer.seek(0)
            while True:
                chunk = buffer.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            buffer.close()

    headers = {"Content-Disposition": 'inline; filename="screenshot.jpg"'}
    return StreamingResponse(content(), media_type="image/jpeg", headers=headers)


@app.post("/convert")
async def convert_pdf(
    request: Request,
    response_format: str | None = Query(default=None, alias="response_format"),
    file: UploadFile = File(...),
) -> StreamingResponse:
    suffix = _get_upload_suffix(file)
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Only PDF, Word, Excel, PowerPoint, and selected video files are supported"
            ),
        )

    tmp_path = await _write_upload_to_tempfile(file, suffix)
    await file.close()

    if os.path.getsize(tmp_path) == 0:
        os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    temp_images = TemporaryDirectory()
    boundary = f"{BOUNDARY_PREFIX}-{uuid.uuid4().hex}"
    accept_header = request.headers.get("accept")
    wants_zip = _wants_zip(response_format, accept_header)
    wants_json = False if wants_zip else _wants_json(response_format, accept_header)

    cleanup_dirs: list[str] = []

    def cleanup(*extra_paths: str | None) -> None:
        for path in (tmp_path, *extra_paths):
            if not path:
                continue
            try:
                os.unlink(path)
            except (FileNotFoundError, IsADirectoryError):
                pass
        for directory in cleanup_dirs:
            shutil.rmtree(directory, ignore_errors=True)
        temp_images.cleanup()

    zip_path: str | None = None

    category = _categorize_extension(suffix)

    if category == "unknown":
        cleanup(None)
        raise HTTPException(status_code=400, detail="Unsupported file extension")

    if category == "video":
        try:
            image_paths = _extract_video_frame(tmp_path, temp_images.name)
        except Exception:
            cleanup(None)
            raise
    else:
        try:
            pdf_path = tmp_path
            if category == "office":
                pdf_path, output_dir = _convert_office_to_pdf(tmp_path)
                cleanup_dirs.append(output_dir)
            image_paths = _convert_pdf_to_jpeg_paths(pdf_path, temp_images.name)
        except Exception:
            cleanup(None)
            raise

    if wants_json:
        try:
            json_payload = []
            for index, image_path in enumerate(image_paths, start=1):
                with open(image_path, "rb") as image_file:
                    encoded = base64.b64encode(image_file.read()).decode("ascii")
                encoded_uri = f"data:image/jpeg;base64,{encoded}"
                json_payload.append(
                    {
                        "page": index,
                        "filename": f"page-{index}.jpg",
                        "data": encoded_uri,
                    }
                )
            return JSONResponse(content=json_payload, media_type="application/json")
        finally:
            cleanup(None)

    def content() -> Iterable[bytes]:
        nonlocal zip_path
        try:
            if wants_zip:
                zip_path = _create_zip_archive(image_paths)
                yield from _stream_file(zip_path)
            else:
                yield from _multipart_stream(image_paths, boundary)
        finally:
            cleanup(zip_path)

    media_type = "application/zip" if wants_zip else f"multipart/mixed; boundary={boundary}"
    headers = {"Content-Disposition": f'attachment; filename="{ZIP_FILENAME}"'} if wants_zip else None
    return StreamingResponse(content(), media_type=media_type, headers=headers)


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
