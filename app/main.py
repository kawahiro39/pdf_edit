from __future__ import annotations

import base64
import json
import os
from io import BytesIO
from json import JSONDecodeError
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Iterable

import uuid
import zipfile

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


async def _write_upload_to_tempfile(upload: UploadFile) -> str:
    with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
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
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    tmp_path = await _write_upload_to_tempfile(file)
    await file.close()

    if os.path.getsize(tmp_path) == 0:
        os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    temp_images = TemporaryDirectory()
    boundary = f"{BOUNDARY_PREFIX}-{uuid.uuid4().hex}"
    accept_header = request.headers.get("accept")
    wants_zip = _wants_zip(response_format, accept_header)
    wants_json = False if wants_zip else _wants_json(response_format, accept_header)

    def cleanup(extra_path: str | None = None) -> None:
        for path in (tmp_path, extra_path):
            if not path:
                continue
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        temp_images.cleanup()

    zip_path: str | None = None

    try:
        image_paths = _convert_pdf_to_jpeg_paths(tmp_path, temp_images.name)
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
